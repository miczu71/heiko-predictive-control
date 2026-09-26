"""Silnik doradcy (D2): migawka danych -> analizatory -> propozycje w bazie -> powiadomienie.

WYŁĄCZNIE odczyt z HA i zapis do własnej bazy. W D2 „zatwierdź” zapisuje tylko decyzję (trial=1) — nic nie
trafia do pompy (pierwszy zapis dopiero w D3, za osobną zgodą, z własnym wykonawcą i allowlistą).

Zasady (wywiad 26.09): ta sama propozycja nie dubluje się, tylko odświeża (rolling TTL); zmiana wartości =
poprzednia „zastąpiona”; ustanie warunków = „wygasła”; jeden aktywny eksperyment naraz; powiadomienie raz na
propozycję i tylko dla wyróżnionej soczewki („żaden” = bez powiadomień); przy zatwierdzeniu ponowne liczenie
analizatorów i sprawdzenie, że parametr nadal ma wartość `from`."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Callable
from zoneinfo import ZoneInfo

from . import catalog, comfort, cycle, floor_learn, ha_client, summaries
from . import db as dbm
from . import floor_plan as fp
from .analysis import percentile
from .analyzers import KIND_ALERT, KIND_EXPERIMENT, Draft, Snapshot, anomalies, curve, dhw
from .kpi import outdoor_class
from .profiles import HeikoProfiles
from .tariff import DEFAULT_OFFPEAK_PRICE_PLN, DEFAULT_PEAK_PRICE_PLN, is_peak_hour

logger = logging.getLogger(__name__)

ANALYZERS: tuple[Callable[[Snapshot], list[Draft]], ...] = (curve.analyze, dhw.analyze, anomalies.analyze)
STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED = "oczekuje", "zatwierdzona", "odrzucona"
STATUS_EXPIRED, STATUS_SUPERSEDED = "wygasła", "zastąpiona"
LENS_OFF = "żaden"
EXPERIMENT_ACTIVE_H = 48          # zatwierdzony eksperyment „trwa” tyle godzin (decyzja 7: ±1 na 24–48 h)
WINDOW_H = 24                     # okno statystyk komfortu dla analizatora krzywej
MIN_WINDOW_SAMPLES = 24           # próbek co 15 min (6 h) — poniżej nie oceniamy komfortu
SUMMARY_DAYS = 15
PLAN_MAX_AGE_H = 3                # starszy plan nie służy do oceny skutków
DHW_PEAK_DAYS = 14
REPLAY_DECISION_HOUR = 7          # odtworzenie zimy: „decyzja” raz na dobę o tej godzinie
REPLAY_MIN_HOURS = 20             # godzin z pełnymi danymi w oknie 24 h
REPLAY_SAMPLES = 12
_DEFAULT_WORKDAY: Callable = lambda d: d.weekday() < 5


def _split(raw) -> list[str]:
    return [e.strip() for e in str(raw or "").split(",") if e.strip()]


split_csv = _split


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _friendly(states_by_id: dict, entity: str) -> str:
    return ((states_by_id.get(entity) or {}).get("attributes") or {}).get("friendly_name") or entity


# ── migawka ─────────────────────────────────────────────────────────────────

def room_window_stats(conn, settings: dict, now: datetime, states_by_id: dict | None = None) -> dict:
    """Średnia stref i 5. percentyl najzimniejszego z pokoi wybranych do minimum — z telemetrii, ostatnie 24 h.
    Do średniej liczą się tylko próbki, w których są WSZYSTKIE strefy (zmienny skład dawałby sztuczne skoki)."""
    zones = _split(settings.get("day_zone_temp_entities"))
    selected = set(comfort.min_room_entities(settings))
    if not zones:
        return {}
    since = int((now - timedelta(hours=WINDOW_H)).timestamp())
    marks = ",".join("?" * len(zones))
    rows = conn.execute(
        f"SELECT t.ts AS ts, k.name AS name, t.value AS value FROM telemetry t JOIN telemetry_keys k ON k.id = t.key_id "
        f"WHERE t.ts >= ? AND k.name IN ({marks}) AND t.value IS NOT NULL ORDER BY t.ts", (since, *zones)).fetchall()
    by_ts: dict[int, dict[str, float]] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], {})[r["name"]] = r["value"]
    avgs: list[float] = []
    coldest: list[tuple[float, str]] = []
    for vals in by_ts.values():
        if len(vals) < len(zones):
            continue
        avgs.append(mean(vals.values()))
        coldest.append(min((v, e) for e, v in vals.items() if e in selected))
    if len(avgs) < MIN_WINDOW_SAMPLES:
        return {}
    where = Counter(e for _, e in coldest).most_common(1)[0][0]
    return {"avg24_c": mean(avgs), "cold24_c": percentile(sorted(v for v, _ in coldest), 0.05),
            "coldest_name": _friendly(states_by_id or {}, where), "samples": len(avgs)}


def dhw_peak_share(conn, now: datetime, is_workday: Callable = _DEFAULT_WORKDAY, days: int = DHW_PEAK_DAYS) -> dict | None:
    """Jaka część próbek trybu pracy z CWU (co 15 min) przypada na szczyt G12w."""
    since = int((now - timedelta(days=days)).timestamp())
    rows = conn.execute(
        "SELECT t.ts AS ts, t.value AS value FROM telemetry t JOIN telemetry_keys k ON k.id = t.key_id "
        "WHERE k.name = 'working_mode' AND t.ts >= ? AND t.value IS NOT NULL", (since,)).fetchall()
    total = peak = 0
    for r in rows:
        if int(round(r["value"])) not in summaries.DHW_CODES:
            continue
        local = datetime.fromtimestamp(r["ts"])
        total += 1
        peak += is_peak_hour(local.hour, bool(is_workday(local.date())))
    return {"share": peak / total, "samples": total} if total else None


def _plan_inputs(conn, now: datetime) -> tuple[dict | None, dict | None]:
    """(skutki jednolitego przesunięcia, oszczędność z przesuwania w czasie zł/dobę) z ostatniego planu."""
    plan = dbm.get_setting(conn, "heiko_plan") or {}
    try:
        age_h = (now - datetime.fromisoformat(plan["generated"])).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return None, None
    if age_h > PLAN_MAX_AGE_H:
        return None, None
    day = 24.0 / (plan.get("horizon_h") or cycle.HORIZON_H)
    shift = {lens: round(v["saving_shift_pln"] * day, 3) for lens, v in (plan.get("summary") or {}).items()
             if v.get("saving_shift_pln") is not None}
    return plan.get("uniform_shift"), shift or None


def build_snapshot(conn, settings: dict, now: datetime, states: list[dict],
                   is_workday: Callable = _DEFAULT_WORKDAY) -> Snapshot:
    by_id = {s.get("entity_id"): s for s in states}
    resolved = catalog.resolve_params(states)
    params: dict[str, float] = {}
    for key, eid in resolved.items():
        if catalog.BY_KEY[key].domain == "number":
            value = catalog.to_number((by_id.get(eid) or {}).get("state"))
            if value is not None:
                params[key] = value
    curve_eid = resolved.get("heating_curve")
    curve_on = ({"on": True, "off": False}.get(str((by_id.get(curve_eid) or {}).get("state", "")).lower())
                if curve_eid else None)
    stats = room_window_stats(conn, settings, now, by_id)
    uniform, time_shift = _plan_inputs(conn, now)
    return Snapshot(
        now=now, target_c=float(settings.get("heiko_room_target_c", 20.6)),
        room_min_c=float(settings.get("heiko_room_min_c", 18.5)),
        avg24_c=stats.get("avg24_c"), cold24_c=stats.get("cold24_c"), coldest_name=stats.get("coldest_name"),
        curve_on=curve_on, params=params, managed=frozenset(_split(settings.get("advisor_managed_keys"))),
        model_identified=floor_learn.load_model(conn).identified, uniform=uniform, time_shift=time_shift,
        summaries=summaries.load_summaries(conn, days=SUMMARY_DAYS), dhw_peak=dhw_peak_share(conn, now, is_workday),
        alerts_on=[(e, _friendly(by_id, e)) for e in _split(settings.get("advisor_anomaly_entities"))
                   if str((by_id.get(e) or {}).get("state", "")).lower() == "on"])


def apply_managed(draft: Draft, managed: frozenset[str]) -> Draft:
    """Parametr prowadzony przez automatyzację HA: doradca tylko sugeruje tekstem, bez wartości do zapisu."""
    if draft.param_key in managed and draft.to_value is not None:
        draft.kind, draft.to_value = KIND_ALERT, None
        draft.reason += " (Ten parametr prowadzi automatyzacja HA — doradca tylko sugeruje.)"
    return draft


def run_analyzers(snap: Snapshot) -> list[Draft]:
    drafts: list[Draft] = []
    for analyze in ANALYZERS:
        try:
            drafts += [apply_managed(d, snap.managed) for d in analyze(snap)]
        except Exception:                                   # jeden analizator nie może położyć pozostałych
            logger.exception("Analizator %s nieudany", getattr(analyze, "__module__", analyze))
    return drafts


# ── propozycje w bazie ───────────────────────────────────────────────────────

def _active_experiment(conn, now: datetime) -> bool:
    since = _iso(now - timedelta(hours=EXPERIMENT_ACTIVE_H))
    return conn.execute("SELECT 1 FROM proposals WHERE kind = ? AND status = ? AND decided_at >= ? LIMIT 1",
                        (KIND_EXPERIMENT, STATUS_APPROVED, since)).fetchone() is not None


def expire_old(conn, now: datetime) -> int:
    n = conn.execute("UPDATE proposals SET status = ?, updated = ? WHERE status = ? AND expires < ?",
                     (STATUS_EXPIRED, _iso(now), STATUS_PENDING, _iso(now))).rowcount
    conn.commit()
    return n


def sync(conn, drafts: list[Draft], now: datetime) -> list[int]:
    """Uzgadnia oczekujące propozycje ze szkicami. Zwraca identyfikatory nowych."""
    ts = _iso(now)
    pending = {(r["analyzer"], r["dedupe_key"]): r
               for r in conn.execute("SELECT * FROM proposals WHERE status = ?", (STATUS_PENDING,))}
    seen: set[tuple[str, str]] = set()
    new_ids: list[int] = []
    experiment_running = _active_experiment(conn, now)
    for d in drafts:
        key = (d.analyzer, d.dedupe_key)
        seen.add(key)
        expires = _iso(now + timedelta(hours=d.ttl_h))
        cur = pending.get(key)
        if cur is not None and cur["to_value"] == d.to_value:
            conn.execute("UPDATE proposals SET updated = ?, expires = ?, lens = ?, from_value = ?, reason = ?, evidence = ?, "
                         "effects = ?, confidence = ? WHERE id = ?",
                         (ts, expires, d.lens, d.from_value, d.reason, json.dumps(d.evidence), json.dumps(d.effects),
                          d.confidence, cur["id"]))
            continue
        if cur is not None:
            conn.execute("UPDATE proposals SET status = ?, updated = ? WHERE id = ?", (STATUS_SUPERSEDED, ts, cur["id"]))
        if d.kind == KIND_EXPERIMENT and experiment_running:
            continue                                        # decyzja 7: jeden aktywny eksperyment naraz
        cursor = conn.execute(
            "INSERT INTO proposals (created, updated, analyzer, dedupe_key, lens, kind, param_key, from_value, to_value, "
            "reason, evidence, effects, confidence, expires, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, ts, d.analyzer, d.dedupe_key, d.lens, d.kind, d.param_key, d.from_value, d.to_value, d.reason,
             json.dumps(d.evidence), json.dumps(d.effects), d.confidence, expires, STATUS_PENDING))
        new_ids.append(cursor.lastrowid)
    for key, row in pending.items():
        if key not in seen:                                 # warunki ustały
            conn.execute("UPDATE proposals SET status = ?, updated = ? WHERE id = ?", (STATUS_EXPIRED, ts, row["id"]))
    conn.commit()
    return new_ids


def _title_and_text(row) -> tuple[str, str]:
    label = catalog.BY_KEY[row["param_key"]].label if row["param_key"] in catalog.BY_KEY else None
    if row["kind"] == KIND_ALERT:
        return "Heiko Doradca: alert", row["reason"][:300]
    if label and row["to_value"] is not None:
        kind = " (eksperyment)" if row["kind"] == KIND_EXPERIMENT else ""
        return "Heiko Doradca: nowa propozycja", f"{label}: z {row['from_value']:g} na {row['to_value']:g}{kind}. {row['reason']}"[:300]
    return "Heiko Doradca: nowa propozycja", row["reason"][:300]


def notify_new(conn, settings: dict, now: datetime, notify=ha_client.notify) -> int:
    """Jedno powiadomienie na propozycję, tylko dla wyróżnionej soczewki (albo „obie”). „Żaden” = cisza."""
    lens = settings.get("advisor_lens") or LENS_OFF
    service = settings.get("advisor_notify_service") or ""
    if lens == LENS_OFF or not service:
        return 0
    link = settings.get("advisor_link_path") or ""
    sent = 0
    for row in conn.execute("SELECT * FROM proposals WHERE status = ? AND notified_at IS NULL ORDER BY id",
                            (STATUS_PENDING,)).fetchall():
        if row["lens"] not in (lens, "obie"):
            continue
        title, text = _title_and_text(row)
        if notify(service, title, text, {"url": link, "clickAction": link} if link else None):
            conn.execute("UPDATE proposals SET notified_at = ? WHERE id = ?", (_iso(now), row["id"]))
            sent += 1
    conn.commit()
    return sent


def run(conn, settings: dict, now: datetime, states: list[dict], is_workday: Callable = _DEFAULT_WORKDAY,
        notify=ha_client.notify) -> dict:
    """Jeden przebieg doradcy: wygaś stare, policz analizatory, uzgodnij propozycje, powiadom."""
    expired = expire_old(conn, now)
    drafts = run_analyzers(build_snapshot(conn, settings, now, states, is_workday))
    new_ids = sync(conn, drafts, now)
    sent = notify_new(conn, settings, now, notify)
    return {"expired": expired, "drafts": len(drafts), "new": len(new_ids), "notified": sent}


# ── decyzje (tryb próbny) ────────────────────────────────────────────────────

def make_revalidator(conn, settings: dict, now: datetime, states: list[dict],
                     is_workday: Callable = _DEFAULT_WORKDAY) -> Callable[[dict], tuple[bool, str]]:
    """Ponowne liczenie przy „Zatwierdź”: inny wynik analizatora albo inna wartość parametru = nie wykonujemy."""
    snap = build_snapshot(conn, settings, now, states, is_workday)
    fresh = {(d.analyzer, d.dedupe_key): d for d in run_analyzers(snap)}

    def check(row: dict) -> tuple[bool, str]:
        d = fresh.get((row["analyzer"], row["dedupe_key"]))
        if d is None:
            return False, "warunki się zmieniły — propozycja jest nieaktualna"
        if d.to_value != row["to_value"]:
            return False, "analizator liczy teraz inną wartość — powstanie nowa propozycja"
        if row["param_key"] and row["to_value"] is not None:
            current = snap.params.get(row["param_key"])
            if current is None or current != row["from_value"]:
                return False, f"parametr ma teraz wartość {current}, a propozycja zakładała {row['from_value']}"
        return True, ""
    return check


def decide(conn, proposal_id: int, decision: str, now: datetime,
           revalidate: Callable[[dict], tuple[bool, str]] | None = None) -> dict:
    """Zapisuje decyzję usera. W D2 to wyłącznie zmiana statusu (trial=1) — bez zapisu do pompy."""
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        return {"ok": False, "error": "decyzja: zatwierdzona albo odrzucona"}
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "nie ma takiej propozycji"}
    if row["status"] != STATUS_PENDING:
        return {"ok": False, "error": f"propozycja ma już status „{row['status']}”"}
    if row["expires"] < _iso(now):
        conn.execute("UPDATE proposals SET status = ?, updated = ? WHERE id = ?", (STATUS_EXPIRED, _iso(now), proposal_id))
        conn.commit()
        return {"ok": False, "error": "propozycja wygasła"}
    if decision == STATUS_APPROVED and revalidate is not None:
        ok, why = revalidate(dict(row))
        if not ok:
            conn.execute("UPDATE proposals SET status = ?, updated = ? WHERE id = ?", (STATUS_SUPERSEDED, _iso(now), proposal_id))
            conn.commit()
            return {"ok": False, "error": why}
    conn.execute("UPDATE proposals SET status = ?, decided_at = ?, updated = ?, trial = 1 WHERE id = ?",
                 (decision, _iso(now), _iso(now), proposal_id))
    conn.commit()
    return {"ok": True, "status": decision, "trial": True}


def _as_dict(row, now: datetime) -> dict:
    out = dict(row)
    out["evidence"], out["effects"] = json.loads(row["evidence"]), json.loads(row["effects"])
    out["label"] = catalog.BY_KEY[row["param_key"]].label if row["param_key"] in catalog.BY_KEY else None
    out["unit"] = catalog.BY_KEY[row["param_key"]].unit if row["param_key"] in catalog.BY_KEY else ""
    try:
        out["remaining_h"] = round((datetime.fromisoformat(row["expires"]) - now).total_seconds() / 3600, 1)
    except ValueError:
        out["remaining_h"] = None
    return out


def list_proposals(conn, now: datetime, history_limit: int = 50) -> dict:
    pending = conn.execute("SELECT * FROM proposals WHERE status = ? ORDER BY id DESC", (STATUS_PENDING,)).fetchall()
    history = conn.execute("SELECT * FROM proposals WHERE status != ? ORDER BY id DESC LIMIT ?",
                           (STATUS_PENDING, history_limit)).fetchall()
    return {"pending": [_as_dict(r, now) for r in pending], "history": [_as_dict(r, now) for r in history]}


# ── odtworzenie zimy (analizator krzywej na LTS sezonu 2025/26) ──────────────

def _curve_values(settings: dict, get_numeric) -> tuple[list, list]:
    amb = [get_numeric(e) for e in _split(settings.get("heiko_curve_ambient_entities") or cycle._DEFAULT_CURVE_AMBIENT)]
    wat = [get_numeric(e) for e in _split(settings.get("heiko_curve_water_entities") or cycle._DEFAULT_CURVE_WATER)]
    return amb, wat


def _replay_effects(settings: dict, model, amb, wat, t0: datetime, hourly: dict, tz: ZoneInfo, avg24: float,
                    out_now: float) -> dict | None:
    """Skutki ±1 z planera dla doby odtwarzanej: prognoza = faktyczne temperatury zewnętrzne z LTS, stan początkowy
    domu w równowadze (Qf = c·(Tr − To)/g), krzywa = obecne punkty krzywej pompy."""
    n = int(cycle.HORIZON_H / fp.STEP_H)
    points = []
    for h in range(cycle.HORIZON_H + 1):
        moment = t0 + timedelta(hours=h)
        key = int(moment.replace(tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)
        if key in hourly:
            points.append((moment, hourly[key][2]))
    t_out = fp.outdoor_steps(points, t0, n, out_now)
    base_steps = [fp.curve_setpoint(amb, wat, t) for t in t_out]
    if any(b is None for b in base_steps):
        return None
    day_start, day_end = int(settings.get("heiko_day_start_hour", 6)), int(settings.get("heiko_day_end_hour", 22))
    prof = HeikoProfiles.from_settings(settings).komfort
    target = float(settings.get("heiko_room_target_c", 20.6))
    blocks = fp.build_blocks(t0, n, _DEFAULT_WORKDAY, day_start, day_end, DEFAULT_PEAK_PRICE_PLN, DEFAULT_OFFPEAK_PRICE_PLN)
    inp = fp.PlanInputs(
        model=model, tr0_c=avg24, qf0_kw=max(0.0, model.c * (avg24 - out_now) / max(model.g, 1e-3)), t_out_c=t_out,
        base_c=base_steps, blocks=blocks, bands=fp.step_bands(t0, n, target, prof.day_c, prof.night_c, day_start, day_end),
        target_c=target, water_min_c=float(settings.get("heiko_water_min_c", 20.0)),
        water_max_c=float(settings.get("heiko_water_max_c", 32.0)), offpeak_price=DEFAULT_OFFPEAK_PRICE_PLN)
    return curve.uniform_effects(inp)


def replay_winter(settings: dict, now: datetime, model, get_statistics=ha_client.get_statistics,
                  get_numeric=ha_client.get_numeric_state) -> dict:
    """Co analizator krzywej zaproponowałby w zeszłym sezonie grzewczym, gdyby krzywa była wtedy włączona:
    raz na dobę o 7:00, na statystykach z poprzednich 24 h. Skutki (zł, kWh) z planera przy modelu bez
    zidentyfikowanej bezwładności — rząd wielkości, nie prognoza."""
    tz = ZoneInfo(settings.get("timezone") or "Europe/Warsaw")
    zones = _split(settings.get("day_zone_temp_entities"))
    selected = set(comfort.min_room_entities(settings))
    outdoor = settings.get("heiko_bootstrap_outdoor_entity") or settings.get("outdoor_temp_entity", "")
    start, end = floor_learn.season_window(now)
    result: dict = {"created": now.isoformat(timespec="minutes"), "ok": False,
                    "window": {"start": start.date().isoformat(), "end": end.date().isoformat()}}
    stats = get_statistics(zones + [outdoor], start.isoformat(), end.isoformat()) if zones and outdoor else None
    if not stats:
        result["error"] = "brak statystyk LTS (WebSocket HA niedostępny albo brak encji)"
        return result

    def by_start(entity: str) -> dict[int, float]:
        return {r["start"]: r["mean"] for r in stats.get(entity, []) if r.get("mean") is not None}
    rooms = {z: by_start(z) for z in zones}
    outdoor_by = by_start(outdoor)
    hourly: dict[int, tuple[float, float, float]] = {}              # start_ms -> (średnia stref, najzimniejszy wybrany, zewn.)
    for key, to in outdoor_by.items():
        vals = {z: rooms[z].get(key) for z in zones}
        if all(v is not None for v in vals.values()):
            hourly[key] = (mean(vals.values()), min(vals[z] for z in zones if z in selected), to)

    amb, wat = _curve_values(settings, get_numeric)
    target = float(settings.get("heiko_room_target_c", 20.6))
    room_min = float(settings.get("heiko_room_min_c", 18.5))
    by_class: dict[int, dict[str, int]] = {}
    totals = {"down": [0, 0.0, 0.0, 0], "up": [0, 0.0, 0.0, 0]}     # dni, Σ Δ zł/dobę, Σ Δ kWh/dobę, dni ze skutkiem
    samples: list[dict] = []
    evaluated = 0
    day = start.astimezone(tz).date() + timedelta(days=1)
    while day <= end.astimezone(tz).date():
        t0 = datetime(day.year, day.month, day.day, REPLAY_DECISION_HOUR)
        window = [hourly[k] for h in range(WINDOW_H, 0, -1)
                  if (k := int((t0 - timedelta(hours=h)).replace(tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)) in hourly]
        day += timedelta(days=1)
        if len(window) < REPLAY_MIN_HOURS:
            continue
        out_mean = mean(r[2] for r in window)
        if out_mean >= floor_learn.HEATING_MAX_OUTDOOR_C:
            continue                                                # dzień bez grzania — reguła zapasu nie ma sensu
        evaluated += 1
        avg24, cold24 = mean(r[0] for r in window), percentile(sorted(r[1] for r in window), 0.05)
        uniform = _replay_effects(settings, model, amb, wat, t0, hourly, tz, avg24, window[-1][2])
        snap = Snapshot(now=t0, target_c=target, room_min_c=room_min, avg24_c=avg24, cold24_c=cold24, curve_on=True,
                        params={curve.KEY: 0.0}, model_identified=model.identified, uniform=uniform)
        for d in curve.analyze(snap):
            side = "down" if d.to_value < d.from_value else "up"
            cls = by_class.setdefault(outdoor_class(out_mean), {"days": 0, "down": 0, "up": 0})
            cls[side] += 1
            totals[side][0] += 1
            eff = d.effects
            if eff.get("cost_day_delta_pln") is not None:
                totals[side][1] += eff["cost_day_delta_pln"]
                totals[side][2] += eff.get("energy_day_delta_kwh") or 0.0
                totals[side][3] += 1
            samples.append({"day": t0.date().isoformat(), "side": side, "avg24_c": round(avg24, 2),
                            "cold24_c": round(cold24, 2), "outdoor_c": round(out_mean, 1), "effects": eff})
        by_class.setdefault(outdoor_class(out_mean), {"days": 0, "down": 0, "up": 0})["days"] += 1

    def side(key: str) -> dict:
        n, cost, kwh, with_effects = totals[key]                    # brak skutku (np. brak punktów krzywej) = None, nie 0
        return {"days": n, "share": round(n / evaluated, 3) if evaluated else None, "days_with_effects": with_effects,
                "cost_delta_pln_season": round(cost, 1) if with_effects else None,
                "energy_delta_kwh_season": round(kwh, 1) if with_effects else None,
                "cost_delta_pln_day": round(cost / with_effects, 3) if with_effects else None,
                "energy_delta_kwh_day": round(kwh / with_effects, 3) if with_effects else None}
    result.update({
        "ok": True, "days_evaluated": evaluated, "decision_hour": REPLAY_DECISION_HOUR,
        "down": side("down"), "up": side("up"),
        "by_class": {str(k): v for k, v in sorted(by_class.items())}, "samples": samples[-REPLAY_SAMPLES:],
        "model": {"identified": model.identified, "source": model.source},
        "caveat": ("Skutki z planera przy modelu bez zidentyfikowanej bezwładności to rząd wielkości, nie prognoza. "
                 "Reguła decyzyjna (zapas komfortu z 24 h) jest sprawdzana na faktycznych danych, skutków nie da się "
                 "zweryfikować bez eksperymentu w sezonie."),
        "thresholds": {"down_avg_margin_c": curve.DOWN_AVG_MARGIN_C, "down_min_margin_c": curve.DOWN_MIN_MARGIN_C,
                       "up_avg_margin_c": curve.UP_AVG_MARGIN_C, "up_min_margin_c": curve.UP_MIN_MARGIN_C,
                       "target_c": target, "room_min_c": room_min},
    })
    return result


def save_replay(conn, replay: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO report_cache (name, created, data) VALUES ('replay', ?, ?)",
                 (replay["created"], json.dumps(replay)))
    conn.commit()


def load_replay(conn) -> dict | None:
    row = conn.execute("SELECT data FROM report_cache WHERE name = 'replay'").fetchone()
    return json.loads(row["data"]) if row else None
