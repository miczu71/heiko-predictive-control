"""Rdzeń cyklu decyzyjnego — funkcje czyste (testowalne bez HA) + cienki
orkiestrator I/O. Pętla A (Heiko) jest nadal WYŁĄCZNIE dry-run: `run_heiko_cycle`
nigdy nie woła `call_service`. Pętla B (AC poddasza, od 0.4.0) zapisuje do
klimatyzatora tylko przez `run_attic_cycle`, i tylko gdy `attic_enabled`."""
from __future__ import annotations

import logging
from datetime import datetime

from . import attic
from . import db as dbm
from . import ha_client
from .model import ThermalModel
from .profiles import AtticProfiles, HeikoProfiles
from .tariff import (DEFAULT_OFFPEAK_PRICE_PLN, DEFAULT_PEAK_PRICE_PLN,
                     is_peak_hour, price_for)

logger = logging.getLogger(__name__)


# ── Pętla A (Heiko / podłogówka) — funkcje czyste ───────────────────────────

def simulated_setpoint_c(baseline_c: float, band_c: float, is_peak: bool) -> float:
    """W szczycie zbij o pełne pasmo, poza szczytem podbij o pełne pasmo —
    najprostsza reguła, która wykorzystuje całe dostępne pasmo komfortu w obu
    kierunkach (nie tylko połowę). Model termiczny (predict_delta_c) mówi,
    ile to naprawdę zmieni temperaturę — to tu tylko wybór punktu w paśmie."""
    return baseline_c - band_c if is_peak else baseline_c + band_c


def simulate_cost_increment_pln(observed_energy_kwh: float, price_pln_kwh: float,
                                 outdoor_c: float, baseline_setpoint_c: float,
                                 sim_setpoint_c: float) -> float:
    """Pierwszy-rzut proxy: energia pompy ciepła w przybliżeniu proporcjonalna
    do różnicy stopni-grzania (setpoint - outdoor), skalujemy zaobserwowane
    zużycie tego cyklu tym stosunkiem. Świadome uproszczenie na Etap 1 —
    dokładniejsze dopiero gdy Etap 3 zacznie realnie sterować (obserwacja
    rzeczywistej reakcji zamiast ekstrapolacji). Chronione przed dzieleniem
    przez ~0 (baseline blisko outdoor = pompa i tak prawie nie grzeje)."""
    baseline_drive = baseline_setpoint_c - outdoor_c
    if baseline_drive <= 0.5 or observed_energy_kwh <= 0:
        return 0.0
    sim_drive = max(sim_setpoint_c - outdoor_c, 0.0)
    scale = sim_drive / baseline_drive
    return observed_energy_kwh * scale * price_pln_kwh


def average_temp(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


# ── Orkiestrator I/O ─────────────────────────────────────────────────────────

def run_heiko_cycle(conn, settings: dict, model: ThermalModel,
                     now: datetime,
                     get_state=ha_client.get_state,
                     get_numeric=ha_client.get_numeric_state,
                     get_bool=ha_client.get_bool_state) -> dict:
    day_entities = [e.strip() for e in
                    str(settings.get("day_zone_temp_entities", "")).split(",") if e.strip()]
    indoor_c = average_temp([get_numeric(e) for e in day_entities])
    outdoor_c = get_numeric(settings.get("outdoor_temp_entity", ""))
    baseline_setpoint_c = get_numeric(settings.get("heiko_setpoint_entity", ""))
    is_peak = get_bool(settings.get("tariff_state_entity", ""))
    price = get_numeric(settings.get("tariff_price_entity", ""))
    if price is None:
        price = price_for(bool(is_peak), DEFAULT_PEAK_PRICE_PLN, DEFAULT_OFFPEAK_PRICE_PLN)

    energy_now = get_numeric(settings.get("pump_energy_entity", ""))

    result = {
        "ts": now.isoformat(), "loop": "heiko",
        "active_profile": settings.get("heiko_active_profile", "ekonomia"),
        "tariff_peak": int(bool(is_peak)) if is_peak is not None else None,
        "price_pln_kwh": price, "outdoor_temp_c": outdoor_c,
        "indoor_temp_c": indoor_c, "write_enabled": int(bool(settings.get("heiko_enabled"))),
    }

    if indoor_c is None or outdoor_c is None or baseline_setpoint_c is None or is_peak is None:
        logger.warning("Heiko: brakujące dane wejściowe, pomijam cykl (indoor=%s outdoor=%s "
                        "setpoint=%s peak=%s)", indoor_c, outdoor_c, baseline_setpoint_c, is_peak)
        dbm.insert_cycle(conn, result)
        return result

    profiles = HeikoProfiles.from_settings(settings)
    hour = now.hour
    day_start = int(settings.get("heiko_day_start_hour", 6))
    day_end = int(settings.get("heiko_day_end_hour", 22))

    prev_row = dbm.latest_cycle(conn, "heiko")
    prev_energy = _get_last_energy(conn)

    cycle_interval_min = float(settings.get("cycle_interval_min", 15))
    dt_hours = cycle_interval_min / 60.0

    for profile_name, band in (("komfort", profiles.komfort), ("ekonomia", profiles.ekonomia)):
        band_c = band.for_hour(hour, day_start, day_end)
        sim_setpoint = simulated_setpoint_c(baseline_setpoint_c, band_c, bool(is_peak))
        result[f"setpoint_{profile_name}"] = round(sim_setpoint, 2)

    observed_energy_kwh = 0.0
    if energy_now is not None and prev_energy is not None and energy_now >= prev_energy:
        observed_energy_kwh = energy_now - prev_energy
    baseline_cost = observed_energy_kwh * price
    result["baseline_cost_today_pln"] = baseline_cost

    for profile_name in ("komfort", "ekonomia"):
        sim_setpoint = result[f"setpoint_{profile_name}"]
        result[f"sim_cost_today_{profile_name}_pln"] = simulate_cost_increment_pln(
            observed_energy_kwh, price, outdoor_c, baseline_setpoint_c, sim_setpoint)

    # Aktualizacja modelu z NATURALNEJ reakcji domu na krzywą natywną
    # (heating_active z working_mode, nie z naszego sterowania — Etap 1
    # niczego nie zapisuje, patrz docstring modułu).
    heating_active = _is_heating_active(get_state)
    if prev_row is not None and prev_row["indoor_temp_c"] is not None:
        model.update(
            indoor_before_c=prev_row["indoor_temp_c"], indoor_after_c=indoor_c,
            outdoor_c=outdoor_c, setpoint_c=baseline_setpoint_c,
            dt_hours=dt_hours, heating_active=heating_active,
        )
    result["coefficient"] = model.k_loss if not heating_active else model.k_gain

    dbm.insert_cycle(conn, result)
    if energy_now is not None:
        dbm.set_setting(conn, "last_pump_energy_kwh", energy_now)
    return result


def _get_last_energy(conn) -> float | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'last_pump_energy_kwh'").fetchone()
    return float(row["value"]) if row else None


def _is_heating_active(get_state) -> bool:
    data = get_state("sensor.heiko_heat_pump_working_mode_2")
    if not data:
        return False
    return str(data.get("state", "")).strip().lower() in ("2", "heating")


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
