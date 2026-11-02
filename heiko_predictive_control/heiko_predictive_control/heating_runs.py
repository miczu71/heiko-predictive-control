"""Dziennik runów podłogówki (E2, `docs/PLAN_heiko_ogrzewanie_E2.md`) — wzorzec `attic_runs.py`. Run = ciągły
okres `working_mode == 2` (kod 4 = CWU+grzanie nie jest runem podłogówki); przerwy ≤ `MERGE_GAP_MIN` scalane.
Energia i koszt z licznika `pump_energy_entity` na `[start, end]` bez marginesu po końcu runu. Licznik obejmuje
wszystkie tryby, ale okno runu to wyłącznie tryb 2 — scalone przerwy mogą zawierać krótki odszron (akceptowane).
**Tylko odczyt** z recordera HA; zero zapisów do pompy."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import catalog
from . import db
from . import ha_client
from . import tariff_feed
from .dhw_cycles import (_cost, _delta, _minutes, _points, _value_at, battery_counter, pv_share_of,
                         source_counters, source_roles)
from .summaries import day_bounds
from .tariff import default_is_workday, fallback_prices

logger = logging.getLogger(__name__)

HEATING_CODE = 2                   # working_mode: grzanie (4 = CWU + grzanie, to nie run podłogówki)
DATA_VERSION = 1
BACKFILL_DAYS = 7                  # zasięg recordera HA
MERGE_GAP_MIN = 10.0               # przerwa krótsza/równa = ten sam run

Series = list[tuple[datetime, str]]


def _on_intervals(times, values, window_start, window_end) -> list[tuple[datetime, datetime]]:
    """Odcinki [od, do) w trybie grzania; wartość sprzed okna nosi się do przodu, ostatnia do końca okna."""
    marks = [(window_start, _value_at(times, values, window_start))]
    marks += [(t, v) for t, v in zip(times, values) if window_start < t < window_end]
    out: list[tuple[datetime, datetime]] = []
    cur = None
    for t, v in marks:
        on = v is not None and int(round(v)) == HEATING_CODE
        if on and cur is None:
            cur = t
        elif not on and cur is not None:
            out.append((cur, t))
            cur = None
    if cur is not None:
        out.append((cur, window_end))
    return out


def extract_runs(series: dict[str, Series], window_start: datetime, window_end: datetime,
                 tz: ZoneInfo, is_workday: Callable[[date], bool] = default_is_workday,
                 peak_price: float | None = None, offpeak_price: float | None = None,
                 feed: tariff_feed.TariffFeed | None = None,
                 battery_cost_at: Callable[[datetime], float | None] = lambda t: None) -> list[dict]:
    """Runy podłogówki w [window_start, window_end). `series`: mode (kod trybu), energy (licznik pompy), water,
    outdoor oraz do kosztu wg źródła: grid, discharge, rce. Run dotykający granic okna (start = początek okna
    z trybem przeniesionym z poprzedzającej doby albo koniec w odległości ≤ `MERGE_GAP_MIN` od końca okna) dostaje
    `complete: False`. Koszt z licznika dzielony wg źródła jak w cyklach CWU (`pv_share`); gdy brakuje serii źródeł,
    podział jest niepełny (`split_complete: False`). Bez licznika: energia 0, koszt/strefy/pv_share None."""
    default_peak, default_offpeak = fallback_prices()
    peak_price = default_peak if peak_price is None else peak_price
    offpeak_price = default_offpeak if offpeak_price is None else offpeak_price
    m_times, m_values = _points(series.get("mode", []))
    e_times, e_values = _points(series.get("energy", []))
    w_times, w_values = _points(series.get("water", []))
    o_times, o_values = _points(series.get("outdoor", []))
    if not m_times:
        return []
    sources = source_counters(series)

    merged: list[list[datetime]] = []
    for a, b in _on_intervals(m_times, m_values, window_start, window_end):
        if merged and _minutes(merged[-1][1], a) <= MERGE_GAP_MIN:
            merged[-1][1] = b
        else:
            merged.append([a, b])

    runs = []
    for start, end in merged:
        edge = end >= window_end - timedelta(minutes=MERGE_GAP_MIN)
        mode0 = _value_at(m_times, m_values, window_start)
        carried = start == window_start and mode0 is not None and int(round(mode0)) == HEATING_CODE
        meter = _delta(e_times, e_values, start, end)
        split = _cost(e_times, e_values, start, end, tz, is_workday, peak_price, offpeak_price,
                      battery_cost_at=battery_cost_at, feed=feed, **sources) if e_times else None
        hour = None if feed is None else feed.at(start.astimezone(tz).replace(tzinfo=None))
        runs.append({
            "v": DATA_VERSION, "start": start.isoformat(), "end": end.isoformat(),
            "complete": not edge and not carried,
            "duration_min": round(_minutes(start, end), 1),
            "energy_kwh": round(meter or 0.0, 3),
            "cost_pln": split["pln"] if split else None,
            "pv_share": pv_share_of(split),
            "split_complete": bool(split and meter is not None and split["split_complete"]),
            "zones": split["zones"] if split else None,
            "zone": None if hour is None else hour.zone,
            "outdoor_c": _value_at(o_times, o_values, start),
            "water_start_c": _value_at(w_times, w_values, start),
            "water_end_c": _value_at(w_times, w_values, end),
        })
    return runs


def store_runs(conn, settings: dict, day: date, states: list[dict],
               get_history=ha_client.get_history,
               is_workday: Callable[[date], bool] | None = None) -> int:
    """Pobiera historię doby (z zapasem 12 h wstecz, by runy przez północ były kompletne) i zapisuje kompletne runy (INSERT OR REPLACE po `start`).
    Bez `pump_energy_entity` nic nie robi (przed jakimkolwiek zapytaniem)."""
    if not settings.get("pump_energy_entity"):
        return 0
    tel = catalog.resolve_telemetry(states)
    roles = {"mode": tel.get("working_mode"), "water": tel.get("water_temperature"),
             "outdoor": tel.get("ambient_air_temperature"), "energy": settings.get("pump_energy_entity")}
    roles.update(source_roles(settings))
    entities = [e for e in roles.values() if e]
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    tz = ZoneInfo(tz_name)
    day_start, day_end = day_bounds(day, tz_name)
    fetch_start = day_start - timedelta(hours=12)
    hist = get_history(entities, fetch_start.isoformat(), day_end.isoformat())
    if not hist:
        return 0
    series = {role: hist.get(eid, []) for role, eid in roles.items() if eid}
    battery = battery_counter(conn, tz, fetch_start, day_end)
    is_workday = is_workday or default_is_workday
    feed = tariff_feed.for_settings(conn, settings, is_workday)
    stored = 0
    for r in extract_runs(series, fetch_start, day_end, tz, is_workday, feed=feed,
                          offpeak_price=feed.offpeak_price(day_start.astimezone(tz).replace(tzinfo=None)),
                          battery_cost_at=lambda t: battery.at(t.astimezone(tz).replace(tzinfo=None))):
        if not r["complete"]:
            continue
        conn.execute("INSERT OR REPLACE INTO heating_runs (start, end, data) VALUES (?, ?, ?)",
                     (r["start"], r["end"], json.dumps(r)))
        stored += 1
    if stored:
        conn.commit()
    return stored


def missing_days(conn, now: datetime, back_days: int = BACKFILL_DAYS) -> list[date]:
    """Ostatnie pełne doby (bez dzisiejszej) bez zapisanych runów w bieżącej wersji rekordu, od najstarszej."""
    have = {r["d"] for r in conn.execute(
        "SELECT substr(start, 1, 10) AS d FROM heating_runs GROUP BY d "
        "HAVING MIN(COALESCE(json_extract(data, '$.v'), 1)) >= ?", (DATA_VERSION,))}
    today = now.date()
    days = [today - timedelta(days=i) for i in range(1, back_days + 1)]
    return [d for d in sorted(days) if d.isoformat() not in have]


def load_runs(conn, days: int = 60) -> list[dict]:
    """Runy zapisane w ostatnich `days` dniach, najnowsze pierwsze."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT data FROM heating_runs WHERE start >= ? ORDER BY start DESC", (since,)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def load_range(conn, start_utc: datetime, end_utc: datetime) -> list[dict]:
    """Zapisy ze `start` w [start_utc, end_utc) (UTC, `start` zapisany jako UTC ISO), rosnąco. Tylko odczyt."""
    return db.load_json_range(conn, "heating_runs", start_utc, end_utc)
