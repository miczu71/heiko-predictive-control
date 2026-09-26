"""Rdzeń cyklu decyzyjnego — funkcje czyste (testowalne bez HA) + cienkie
orkiestratory I/O. Pętla A (Heiko, od 0.6.0 plan blokowy na modelu podłogówki)
jest nadal WYŁĄCZNIE obserwacją: `run_heiko_cycle` nigdy nie woła `call_service`.
Pętla B (AC poddasza, od 0.4.0) zapisuje do klimatyzatora tylko przez
`run_attic_cycle`, i tylko gdy `attic_enabled`."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import attic
from . import db as dbm
from . import floor_learn
from . import floor_model as fm
from . import floor_plan as fp
from . import ha_client
from .profiles import AtticProfiles, HeikoProfiles
from .tariff import (DEFAULT_OFFPEAK_PRICE_PLN, DEFAULT_PEAK_PRICE_PLN,
                     is_peak_hour, price_for)

logger = logging.getLogger(__name__)

HORIZON_H = 36
_MIN_STEP_MIN, _MAX_STEP_MIN = 5, 30        # ważny odstęp między cyklami do uczenia
_DEFAULT_CURVE_AMBIENT = ",".join(f"number.heiko_heat_pump_curve_ambient_temp_{i}" for i in range(1, 6))
_DEFAULT_CURVE_WATER = ",".join(f"number.heiko_heat_pump_curve_water_temp_{i}" for i in range(1, 6))
_DEFAULT_WATER_TEMP = "sensor.heiko_heat_pump_condenser_temperature"
_DEFAULT_MODE = "sensor.heiko_heat_pump_working_mode_2"
DEFAULT_WATER_TARGET_ENTITY = "sensor.heiko_heat_pump_water_temperature_setpoint"
_DEFAULT_CURVE_SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
REDUCED_MIN_DROP_C = 0.5          # o tyle cel wody poniżej krzywej = ograniczona nastawa aktywna


# ── Pętla A (Heiko / podłogówka) — funkcje czyste ───────────────────────────

def average_temp(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


def mode_flags(state_text) -> tuple[bool, bool]:
    """(grzanie, CWU) z tekstu trybu pracy pompy („Heating”, „Sanitary Hot Water”, 2, 1…)."""
    s = str(state_text or "").strip().lower()
    heating = s in ("2", "heating") or s.startswith("heating")
    dhw = s in ("1",) or "sanitary" in s or "dhw" in s
    return heating, dhw


def infer_reduced(water_target_c: float | None, curve_c: float | None, shift_c: float | None,
                  curve_on: bool | None) -> bool | None:
    """Czy działa natywna „ograniczona nastawa”? Zegar 5.3 nie jest czytelny z HA, więc
    wnioskujemy: przy WŁĄCZONEJ krzywej cel wody wyraźnie poniżej krzywej (+ przesunięcie).
    Przy wyłączonej krzywej cel to stała nastawa — porównanie z krzywą nic nie mówi (None)."""
    if curve_on is not True or water_target_c is None or curve_c is None:
        return None
    expected = curve_c + (shift_c or 0.0)
    return water_target_c <= expected - REDUCED_MIN_DROP_C


def parse_forecast(forecast) -> list[tuple[datetime, float]]:
    """Prognoza godzinowa z HA -> [(czas lokalny bez strefy, temperatura)]."""
    points = []
    for item in forecast or []:
        ts = _parse_ts(item.get("datetime"))
        try:
            temp = float(item.get("temperature"))
        except (TypeError, ValueError):
            continue
        if ts is not None:
            points.append((ts, temp))
    return sorted(points)


def workday_resolver(today: date, today_workday: bool | None, check):
    """`fn(date) -> bool`: dziś ze stanu sensora, inne dni przez `check`
    (workday.check_date, uwzględnia święta); brak odpowiedzi = pn–pt."""
    cache: dict[date, bool] = {}

    def resolve(d: date) -> bool:
        if d not in cache:
            value = today_workday if (d == today and today_workday is not None) else check(d.isoformat())
            cache[d] = (d.weekday() < 5) if value is None else bool(value)
        return cache[d]
    return resolve


def _r(value, digits=2):
    return None if value is None else round(value, digits)


def summarize_plan(now: datetime, t0: datetime, inp: fp.PlanInputs, base: fp.PlanResult,
                   plans: dict[str, fp.PlanResult], shifts: dict[str, fp.PlanResult],
                   indoor_c: float, outdoor_c: float, min_room: tuple[float, str] | None) -> dict:
    """Dokument planu do bazy (settings["heiko_plan"]) — czyta go pulpit."""
    n = len(inp.t_out_c)
    steps_per_hour = int(round(1 / inp.step_h))
    hours = []
    for i in range(steps_per_hour - 1, n, steps_per_hour):
        hours.append({
            "t": (t0 + timedelta(hours=(i + 1) * inp.step_h)).isoformat(timespec="minutes"),
            "outdoor": _r(inp.t_out_c[i], 1), "band_lo": _r(inp.bands[i][0], 1),
            "band_hi": _r(inp.bands[i][1], 1), "base_set": _r(base.setpoints_c[i], 1),
            "base_temp": _r(base.temps_c[i]),
            **{f"{name}_temp": _r(plan.temps_c[i]) for name, plan in plans.items()},
            **{f"{name}_set": _r(plan.setpoints_c[i], 1) for name, plan in plans.items()},
        })
    blocks = [{
        "start": b.start.isoformat(timespec="minutes"), "end": b.end.isoformat(timespec="minutes"),
        "peak": b.is_peak, "price": b.price_pln_kwh, "base_c": _r(base.setpoints_c[b.i0], 1),
        **{f"{name}_c": _r(plan.block_setpoint(b), 1) for name, plan in plans.items()},
        **{f"{name}_delta": plan.deltas[k] for name, plan in plans.items()},
    } for k, b in enumerate(inp.blocks)]
    summary = {}
    for name, plan in plans.items():
        summary[name] = {
            "cost_pln": _r(plan.cost_pln), "saving_pln": _r(base.cost_pln - plan.cost_pln),
            "saving_shift_pln": _r(base.cost_pln - shifts[name].cost_pln),
            "mean_temp_c": _r(fp.mean_temp(plan)), "min_temp_c": _r(min(plan.temps_c)),
        }
    return {
        "generated": now.isoformat(timespec="seconds"), "t0": t0.isoformat(timespec="minutes"),
        "horizon_h": HORIZON_H, "indoor_c": _r(indoor_c, 1), "outdoor_c": _r(outdoor_c, 1),
        "fuse_active": inp.fuse_active,
        "min_room_c": None if min_room is None else _r(min_room[0], 1),
        "min_room_name": None if min_room is None else min_room[1],
        "model": inp.model.as_dict(), "target_c": inp.target_c,
        "baseline": {"cost_pln": _r(base.cost_pln), "mean_temp_c": _r(fp.mean_temp(base)),
                     "min_temp_c": _r(min(base.temps_c))},
        "summary": summary, "blocks": blocks, "hours": hours,
    }


# ── Orkiestrator pętli A ──────────────────────────────────────────────────

def run_heiko_cycle(conn, settings: dict, now: datetime,
                     get_state=ha_client.get_state,
                     get_numeric=ha_client.get_numeric_state,
                     get_bool=ha_client.get_bool_state,
                     get_forecast=ha_client.get_forecast,
                     check_workday=ha_client.check_workday) -> dict:
    """Odczyt -> (uczenie modelu) -> plan blokowy dla obu profili -> baza.
    Nic nie zapisuje do pompy (faza cienia)."""
    rooms: list[tuple[float, str]] = []
    for entity in _entity_list(settings.get("day_zone_temp_entities")):
        data = get_state(entity) or {}
        try:
            rooms.append((float(data.get("state")),
                          (data.get("attributes") or {}).get("friendly_name") or entity))
        except (TypeError, ValueError):
            continue                                   # niedostępny czujnik nie wchodzi do średniej
    indoor_c = average_temp([t for t, _ in rooms])
    min_room = min(rooms) if rooms else None
    outdoor_c = get_numeric(settings.get("outdoor_temp_entity", ""))
    is_peak = get_bool(settings.get("tariff_state_entity", ""))
    price = get_numeric(settings.get("tariff_price_entity", ""))
    if price is None:
        price = price_for(bool(is_peak), DEFAULT_PEAK_PRICE_PLN, DEFAULT_OFFPEAK_PRICE_PLN)
    energy_now = get_numeric(settings.get("pump_energy_entity", ""))
    water_temp = get_numeric(settings.get("heiko_water_temp_entity") or _DEFAULT_WATER_TEMP)
    mode = (get_state(settings.get("heiko_working_mode_entity") or _DEFAULT_MODE) or {}).get("state")
    heating_now, dhw_now = mode_flags(mode)
    water_target = get_numeric(settings.get("heiko_water_setpoint_entity") or DEFAULT_WATER_TARGET_ENTITY)
    curve_raw = str((get_state(settings.get("heiko_curve_switch_entity", "")) or {}).get("state", "")).lower()
    curve_on = {"on": True, "off": False}.get(curve_raw)
    shift = get_numeric(settings.get("heiko_curve_shift_entity") or _DEFAULT_CURVE_SHIFT)

    result = {
        "ts": now.isoformat(), "loop": "heiko",
        "active_profile": settings.get("heiko_active_profile", "ekonomia"),
        "tariff_peak": int(bool(is_peak)) if is_peak is not None else None,
        "price_pln_kwh": price, "outdoor_temp_c": outdoor_c, "indoor_temp_c": indoor_c,
        "write_enabled": int(bool(settings.get("heiko_enabled"))),
        "water_temp_c": water_temp, "heating_active": int(heating_now), "dhw_active": int(dhw_now),
        "water_setpoint_c": water_target, "curve_on": None if curve_on is None else int(curve_on),
        "min_room_c": None if min_room is None else min_room[0],
        "min_room_name": None if min_room is None else min_room[1],
    }
    if indoor_c is None or outdoor_c is None or is_peak is None:
        logger.warning("Heiko: brakujące dane wejściowe, pomijam cykl (indoor=%s outdoor=%s peak=%s)",
                        indoor_c, outdoor_c, is_peak)
        dbm.insert_cycle(conn, result)
        return result

    model = floor_learn.load_model(conn)
    n = int(HORIZON_H / fp.STEP_H)
    t0 = fp.floor_to_step(now)

    # Krzywa grzewcza: równoważnik nastawy wody przy prognozowanej temp. zewnętrznej.
    points = parse_forecast(get_forecast(settings.get("weather_entity", "")))
    t_out = fp.outdoor_steps(points, t0, n, outdoor_c)
    amb = [get_numeric(e) for e in _entity_list(settings.get("heiko_curve_ambient_entities") or _DEFAULT_CURVE_AMBIENT)]
    wat = [get_numeric(e) for e in _entity_list(settings.get("heiko_curve_water_entities") or _DEFAULT_CURVE_WATER)]
    base_steps = [fp.curve_setpoint(amb, wat, t) for t in t_out]
    curve_now = fp.curve_setpoint(amb, wat, outdoor_c)
    result["base_curve_c"] = curve_now
    reduced = infer_reduced(water_target, curve_now, shift, curve_on)
    result["reduced_active"] = None if reduced is None else int(reduced)

    # Pomiar mocy cieplnej w minionym kroku i aktualizacja stanu magazynu wylewki.
    prev = dbm.latest_cycle(conn, "heiko")
    prev_ts = _parse_ts(prev["ts"]) if prev else None
    dt_h = None
    if prev_ts is not None:
        minutes = (now - prev_ts).total_seconds() / 60.0
        if _MIN_STEP_MIN <= minutes <= _MAX_STEP_MIN:
            dt_h = minutes / 60.0
    prev_energy = _get_last_energy(conn)
    energy_kwh = heat_kw = None
    if dt_h and energy_now is not None and prev_energy is not None and energy_now >= prev_energy:
        energy_kwh = energy_now - prev_energy
        prev_dhw = bool(prev["dhw_active"]) if prev["dhw_active"] is not None else dhw_now
        prev_heating = bool(prev["heating_active"]) if prev["heating_active"] is not None else heating_now
        heating_step = (heating_now or prev_heating) and not (dhw_now or prev_dhw)
        tw = water_temp if water_temp is not None else (curve_now if curve_now is not None else outdoor_c + 10)
        heat_kw = fm.measured_heat_kw(energy_kwh, dt_h, outdoor_c, tw) if heating_step else 0.0
    result["energy_kwh"], result["heat_kw"] = energy_kwh, heat_kw

    fstate = dbm.get_setting(conn, "floor_state") or {}
    qf_now, model_err = (heat_kw or 0.0), None
    fts = _parse_ts(fstate.get("ts"))
    if fts is not None and all(isinstance(fstate.get(k), (int, float)) for k in ("tr", "qf", "to")):
        f_min = (now - fts).total_seconds() / 60.0
        if _MIN_STEP_MIN <= f_min <= _MAX_STEP_MIN:
            f_dt = f_min / 60.0
            pred = fstate["tr"] + f_dt * (model.g * fstate["qf"] - model.c * (fstate["tr"] - fstate["to"]))
            model_err = indoor_c - pred
            qf_now = fstate["qf"] if heat_kw is None else \
                fstate["qf"] + fm.alpha(f_dt, model.tau_h) * (heat_kw - fstate["qf"])
    result["model_err_c"] = model_err
    dbm.set_setting(conn, "floor_state", {"ts": now.isoformat(), "tr": indoor_c, "qf": qf_now,
                                            "to": outdoor_c})

    if energy_now is not None:
        dbm.set_setting(conn, "last_pump_energy_kwh", energy_now)
    result["coefficient"] = model.c

    if any(b is None for b in base_steps):
        logger.warning("Heiko: brak punktów krzywej grzewczej — plan pominięty")
        dbm.insert_cycle(conn, result)
        return result

    # Plan: oba profile na tym samym modelu i tej samej osi czasu.
    workday = workday_resolver(now.date(), get_bool("binary_sensor.workday"), check_workday)
    day_start = int(settings.get("heiko_day_start_hour", 6))
    day_end = int(settings.get("heiko_day_end_hour", 22))
    blocks = fp.build_blocks(t0, n, workday, day_start, day_end,
                              DEFAULT_PEAK_PRICE_PLN, DEFAULT_OFFPEAK_PRICE_PLN)
    target = float(settings.get("heiko_room_target_c", 20.6))
    room_min = float(settings.get("heiko_room_min_c", 18.5))
    fuse = min_room is not None and min_room[0] < room_min
    profiles = HeikoProfiles.from_settings(settings)
    bands = {name: fp.step_bands(t0, n, target, prof.day_c, prof.night_c, day_start, day_end)
             for name, prof in (("komfort", profiles.komfort), ("ekonomia", profiles.ekonomia))}
    common = dict(model=model, tr0_c=indoor_c, qf0_kw=qf_now, t_out_c=t_out, base_c=base_steps,
                  blocks=blocks, target_c=target, fuse_active=fuse,
                  water_min_c=float(settings.get("heiko_water_min_c", 20.0)),
                  water_max_c=float(settings.get("heiko_water_max_c", 32.0)),
                  offpeak_price=DEFAULT_OFFPEAK_PRICE_PLN)
    inputs = {name: fp.PlanInputs(bands=b, **common) for name, b in bands.items()}
    base = fp.baseline(inputs["komfort"])
    plans = {name: fp.optimize(inp) for name, inp in inputs.items()}
    shifts = {name: fp.plan_with_shift_only(inp, base) for name, inp in inputs.items()}

    active = result["active_profile"] if result["active_profile"] in plans else "ekonomia"
    price0 = fp.step_prices(blocks, n)[0]
    for name, plan in plans.items():
        result[f"setpoint_{name}"] = _r(plan.block_setpoint(blocks[0]), 1)
        result[f"sim_cost_today_{name}_pln"] = plan.energy_kwh[0] * price0
    result["plan_setpoint_c"] = result[f"setpoint_{active}"]
    result["baseline_cost_today_pln"] = base.energy_kwh[0] * price0
    dbm.set_setting(conn, "heiko_plan", summarize_plan(
        now, t0, inputs["komfort"], base, plans, shifts, indoor_c, outdoor_c, min_room))
    dbm.insert_cycle(conn, result)
    return result


def _get_last_energy(conn) -> float | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'last_pump_energy_kwh'").fetchone()
    return float(row["value"]) if row else None


def _parse_ts(value) -> datetime | None:
    """ISO z HA (ze strefą) -> naiwny czas lokalny, spójny z datetime.now()."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _entity_list(raw) -> list[str]:
    return [e.strip() for e in str(raw or "").split(",") if e.strip()]


def _open_since(get_state, entities: list[str]) -> datetime | None:
    """Najstarsza zmiana na „otwarte” spośród otwartych czujników; None gdy
    wszystkie zamknięte/niedostępne."""
    since = []
    for entity in entities:
        data = get_state(entity) or {}
        if str(data.get("state", "")).lower() == "on":
            ts = _parse_ts(data.get("last_changed"))
            if ts is not None:
                since.append(ts)
    return min(since) if since else None


_AC_UNAVAILABLE = ("", "unavailable", "unknown", "none")


def run_attic_cycle(conn, settings: dict, now: datetime,
                     is_workday: bool | None, on_vacation: bool | None = None,
                     get_state=ha_client.get_state,
                     get_numeric=ha_client.get_numeric_state,
                     call_service=ha_client.call_service,
                     notify=ha_client.notify) -> dict:
    """Pętla B: odczyt -> attic.decide -> (opcjonalny) zapis do AC -> baza.
    Stan sterowania utrwalany dopiero po udanym zapisie: nieudane wywołanie
    usługi = ponowna próba w następnym cyklu, bez fałszywej „ręcznej zmiany”."""
    ac_entity = settings.get("attic_ac_entity", "")
    ac = get_state(ac_entity) or {}
    ac_state = str(ac.get("state", "")).strip().lower()
    ac_state = None if ac_state in _AC_UNAVAILABLE else ac_state
    try:
        ac_setpoint = float((ac.get("attributes") or {}).get("temperature"))
    except (TypeError, ValueError):
        ac_setpoint = None

    indoor_c = get_numeric(settings.get("attic_temp_entity", ""))
    window_since = _open_since(get_state, _entity_list(settings.get("attic_window_entities")))
    door_raw = str((get_state(settings.get("attic_door_entity", "")) or {}).get("state", "")).lower()
    door_open = {"on": 1, "off": 0}.get(door_raw)
    power_w = get_numeric(settings.get("attic_power_entity", ""))

    # Ręczny przełącznik pauzy (np. input_boolean): brak encji/niedostępny = brak pauzy.
    pause_raw = str((get_state(settings.get("attic_pause_entity", "")) or {}).get("state", "")).lower()
    paused = {"on": True, "off": False}.get(pause_raw)

    # Czujnik obecności: brak encji/niedostępny = brak reguły „pusto" (bezpieczniej grzać).
    presence_data = get_state(settings.get("attic_presence_entity", "")) or {}
    presence = {"on": True, "off": False}.get(str(presence_data.get("state", "")).lower())
    vacant_since = _parse_ts(presence_data.get("last_changed")) if presence is False else None

    enabled = bool(settings.get("attic_enabled"))
    state = attic.AtticState.from_dict(dbm.get_setting(conn, "attic_ctrl_state"))
    inputs = attic.AtticInputs(
        now=now, is_workday=is_workday, on_vacation=on_vacation, temp_c=indoor_c,
        ac_state=ac_state, ac_setpoint_c=ac_setpoint, window_open_since=window_since,
        door_open=None if door_open is None else bool(door_open), paused=paused,
        presence=presence, vacant_since=vacant_since)
    decision = attic.decide(inputs, state, settings, enabled)

    wrote, failed = 0, False
    for cmd in decision.commands:
        if decision.dry_run:
            logger.info("Poddasze [dry-run]: zapisałbym %s %s", cmd["service"], cmd["data"])
            continue
        ok = call_service("climate", cmd["service"], {"entity_id": ac_entity, **cmd["data"]})
        if ok:
            wrote = 1
            logger.info("Poddasze: %s %s -> %s", cmd["service"], cmd["data"], ac_entity)
        else:
            failed = True
            logger.warning("Poddasze: zapis %s nieudany, ponowię w następnym cyklu",
                           cmd["service"])
    notify_to = settings.get("notify_service", "")
    if failed:
        notify(notify_to, "Heiko Predictive: błąd AC poddasza",
               "Nie udało się sterować klimatyzacją poddasza — ponowię w następnym cyklu.")
    elif not decision.dry_run:
        if "takeover" in decision.events:
            now_txt = "—" if indoor_c is None else f"{indoor_c:.1f}"
            notify(notify_to, "Heiko Predictive: AC poddasza",
                   f"Włączam ogrzewanie poddasza (cel {decision.target_c:.1f}°C, "
                   f"teraz {now_txt}°C).")
        if "manual_override" in decision.events:
            notify(notify_to, "Heiko Predictive: AC poddasza",
                   "Wykryto ręczną zmianę klimatyzacji — do końca dnia nie steruję.")
    if "vacant" in decision.events and not decision.dry_run and not failed:
        logger.info("Poddasze: pusto od %.0f min — klimatyzator wyłączony", decision.vacant_min or 0)
    if not decision.dry_run and not failed:
        dbm.set_setting(conn, "attic_ctrl_state", decision.state.as_dict())

    # Energia i koszt AC — całkowanie mocy chwilowej od poprzedniego cyklu.
    prev = dbm.latest_cycle(conn, "attic")
    prev_ts = _parse_ts(prev["ts"]) if prev else None
    dt_h = 0.0
    if prev_ts is not None and now > prev_ts:
        dt_h = min((now - prev_ts).total_seconds() / 3600.0, 0.5)
    energy_kwh = (power_w or 0.0) / 1000.0 * dt_h
    price = get_numeric(settings.get("tariff_price_entity", ""))
    if price is None:
        price = price_for(is_peak_hour(now.hour, bool(is_workday)),
                          DEFAULT_PEAK_PRICE_PLN, DEFAULT_OFFPEAK_PRICE_PLN)

    profiles = AtticProfiles.from_settings(settings)
    show_targets = decision.phase not in ("poza_oknem", "czeka", "przekazanie")
    st = decision.state
    result = {
        "ts": now.isoformat(), "loop": "attic",
        "active_profile": settings.get("attic_active_profile", "ekonomia"),
        "tariff_peak": None, "price_pln_kwh": price, "outdoor_temp_c": None,
        "indoor_temp_c": indoor_c, "write_enabled": int(enabled),
        "setpoint_komfort": profiles.komfort_target_c if show_targets else None,
        "setpoint_ekonomia": profiles.ekonomia_target_c if show_targets else None,
        "coefficient": st.heat_rate_c_h, "baseline_cost_today_pln": None,
        "sim_cost_today_komfort_pln": None, "sim_cost_today_ekonomia_pln": None,
        "phase": "dry_run" if decision.dry_run and decision.commands else decision.phase,
        "ac_cmd_setpoint": (st.last_cmd or {}).get("temp"),
        "offset_c": st.offset_c, "ac_power_w": power_w,
        "window_open": int(window_since is not None), "door_open": door_open,
        "wrote": wrote, "attic_energy_kwh": energy_kwh,
        "attic_cost_pln": energy_kwh * price,
        "planned_start": decision.planned_start.isoformat() if decision.planned_start else None,
        "presence": None if presence is None else int(presence),
        "vacant_min": decision.vacant_min,
    }
    dbm.insert_cycle(conn, result)
    return result
