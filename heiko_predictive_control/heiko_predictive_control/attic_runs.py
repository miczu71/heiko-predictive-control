"""Dziennik runów klimatyzacji na poddaszu (A2, `docs/PLAN_fixes_2026_11_A.md`) — funkcje czyste
+ jedna funkcja I/O (`store_runs`), wzorzec `dhw_cycles.py`. Run = ciągły okres `ac_power_w > AC_ON_MIN_W`
(przerwy ≤ `MERGE_GAP_MIN` scalane). **Tylko odczyt** z recordera HA; zero zapisów do klimatyzacji."""
from __future__ import annotations

import bisect
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import catalog
from . import ha_client
from . import tariff_feed
from .stats import AC_ON_MIN_W
from .summaries import day_bounds
from .tariff import default_is_workday, fallback_hour, fallback_prices, price_for

logger = logging.getLogger(__name__)

DATA_VERSION = 1
BACKFILL_DAYS = 7                  # zasięg recordera HA
MERGE_GAP_MIN = 10.0               # przerwa krótsza/równa = ta sama praca sprężarki (postoje)
ENERGY_CHECK_TOLERANCE = 0.25      # rozjazd licznik vs całka mocy powyżej tego = energy_check false

Series = list[tuple[datetime, str]]


def _points(series: Series) -> tuple[list[datetime], list[float | None]]:
    pts = sorted((t, catalog.to_number(s)) for t, s in series)
    return [t for t, _ in pts], [v for _, v in pts]


def _value_at(times: list[datetime], values: list[float | None], t: datetime) -> float | None:
    """Ostatnia znana wartość w chwili `t` (krok schodkowy)."""
    i = bisect.bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def _on_intervals(times, values, window_start, window_end) -> list[tuple[datetime, datetime]]:
    """Odcinki [od, do) z mocą > AC_ON_MIN_W; wartość sprzed okna nosi się do przodu, ostatnia do końca okna."""
    marks = [(window_start, _value_at(times, values, window_start))]
    marks += [(t, v) for t, v in zip(times, values) if window_start < t < window_end]
    out: list[tuple[datetime, datetime]] = []
    cur = None
    for t, v in marks:
        on = v is not None and v > AC_ON_MIN_W
        if on and cur is None:
            cur = t
        elif not on and cur is not None:
            out.append((cur, t))
            cur = None
    if cur is not None:
        out.append((cur, window_end))
    return out


def _power_at(times, values, t: datetime) -> float:
    """Moc liniowo interpolowana między próbkami (poza zakresem: ostatnia znana, brak = 0)."""
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return 0.0
    v0 = values[i] or 0.0
    if i + 1 >= len(times) or times[i] == t:
        return v0
    v1 = values[i + 1] or 0.0
    frac = _minutes(times[i], t) / _minutes(times[i], times[i + 1])
    return v0 + (v1 - v0) * frac


def _integral(times, values, start: datetime, end: datetime, tz: ZoneInfo,
              price_at: Callable[[datetime], float]) -> tuple[float, float]:
    """(kWh, zł) z całki trapezowej mocy na [start, end]; odcinki tną próbki i granice godzin lokalnych,
    cena z godziny lokalnej początku odcinka."""
    cuts = {start, end} | {t for t in times if start < t < end}
    h = start.astimezone(tz).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while h.astimezone(timezone.utc) < end:
        cuts.add(h.astimezone(timezone.utc))
        h += timedelta(hours=1)
    ordered = sorted(cuts)
    kwh = pln = 0.0
    for a, b in zip(ordered, ordered[1:]):
        seg = (_power_at(times, values, a) + _power_at(times, values, b)) / 2.0 * (_minutes(a, b) / 60.0) / 1000.0
        kwh += seg
        pln += seg * price_at(a.astimezone(tz).replace(tzinfo=None))
    return kwh, pln


def extract_runs(series: dict[str, Series], window_start: datetime, window_end: datetime,
                 tz: ZoneInfo, is_workday: Callable[[date], bool] = default_is_workday,
                 peak_price: float | None = None, offpeak_price: float | None = None,
                 feed: tariff_feed.TariffFeed | None = None) -> list[dict]:
    """Runy AC w [window_start, window_end). `series`: power (W), energy (licznik kWh), temp (poddasze),
    outdoor. Run dotykający granic okna (start = początek okna z mocą przeniesioną z poprzedzającej doby albo
    koniec w odległości ≤ `MERGE_GAP_MIN` od końca okna) dostaje `complete: False` — wołający go pomija,
    okno kolejnej doby złapie go w całości. Cena godziny z `feed`, bez niego zapas G12w."""
    default_peak, default_offpeak = fallback_prices()
    peak_price = default_peak if peak_price is None else peak_price
    offpeak_price = default_offpeak if offpeak_price is None else offpeak_price
    p_times, p_values = _points(series.get("power", []))
    e_times, e_values = _points(series.get("energy", []))
    t_times, t_values = _points(series.get("temp", []))
    o_times, o_values = _points(series.get("outdoor", []))
    if not p_times:
        return []

    def price_at(local: datetime) -> float:
        if feed is not None:
            return feed.price(local)
        return price_for(fallback_hour(local, is_workday(local.date())).is_peak, peak_price, offpeak_price)

    merged: list[list[datetime]] = []
    for a, b in _on_intervals(p_times, p_values, window_start, window_end):
        if merged and _minutes(merged[-1][1], a) <= MERGE_GAP_MIN:
            merged[-1][1] = b
        else:
            merged.append([a, b])

    runs = []
    for start, end in merged:
        edge = end >= window_end - timedelta(minutes=MERGE_GAP_MIN)
        complete = not edge and not (start == window_start and (_value_at(p_times, p_values, window_start) or 0) > AC_ON_MIN_W)
        hours = _minutes(start, end) / 60.0
        int_kwh, pln = _integral(p_times, p_values, start, end, tz, price_at)
        meter = None
        a, b = _value_at(e_times, e_values, start), _value_at(e_times, e_values, end)
        if a is not None and b is not None and b >= a:
            meter = b - a
        energy = int_kwh if meter is None else meter
        check = None if meter is None else bool(int_kwh <= 0 and meter <= 0
                                                 or int_kwh > 0 and abs(meter - int_kwh) / int_kwh <= ENERGY_CHECK_TOLERANCE)
        in_run = [v for t, v in zip(p_times, p_values) if start <= t < end and v is not None]
        t0, t1 = _value_at(t_times, t_values, start), _value_at(t_times, t_values, end)
        delta = None if t0 is None or t1 is None else t1 - t0
        hour = None if feed is None else feed.at(start.astimezone(tz).replace(tzinfo=None))
        runs.append({
            "v": DATA_VERSION, "start": start.isoformat(), "end": end.isoformat(), "complete": complete,
            "duration_min": round(_minutes(start, end), 1),
            "energy_kwh": round(energy, 3), "energy_check": check,
            "peak_w": max(in_run) if in_run else None,
            "avg_w": round(int_kwh * 1000.0 / hours, 0) if hours > 0 else None,
            "cost_pln": round(pln, 2), "zone": None if hour is None else hour.zone,
            "temp_start_c": t0, "temp_end_c": t1,
            "delta_c": None if delta is None else round(delta, 2),
            "rate_c_h": None if delta is None or hours <= 0 else round(delta / hours, 2),
            "kwh_per_c": None if delta is None or delta <= 0 else round(energy / delta, 3),
            "outdoor_c": _value_at(o_times, o_values, start),
        })
    return runs


def store_runs(conn, settings: dict, day: date, states: list[dict], now: datetime,
               get_history=ha_client.get_history,
               is_workday: Callable[[date], bool] | None = None) -> int:
    """Pobiera historię doby (z zapasem 3 h wstecz, by złapać runy zaczęte tuż przed północą) i zapisuje
    kompletne runy (INSERT OR REPLACE po `start`). Bez `attic_power_entity` nic nie robi."""
    if not settings.get("attic_power_entity"):
        return 0
    tel = catalog.resolve_telemetry(states)
    roles = {"power": settings.get("attic_power_entity"),
             "energy": settings.get("attic_energy_entity") or None,
             "temp": settings.get("attic_temp_entity") or None,
             "outdoor": tel.get("ambient_air_temperature")}
    entities = [e for e in roles.values() if e]
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    tz = ZoneInfo(tz_name)
    day_start, day_end = day_bounds(day, tz_name)
    fetch_start = day_start - timedelta(hours=3)
    hist = get_history(entities, fetch_start.isoformat(), day_end.isoformat())
    if not hist:
        return 0
    series = {role: hist.get(eid, []) for role, eid in roles.items() if eid}
    is_workday = is_workday or default_is_workday
    feed = tariff_feed.for_settings(conn, settings, is_workday)
    stored = 0
    for r in extract_runs(series, fetch_start, day_end, tz, is_workday, feed=feed):
        if not r["complete"]:
            continue
        conn.execute("INSERT OR REPLACE INTO attic_runs (start, end, data) VALUES (?, ?, ?)",
                     (r["start"], r["end"], json.dumps(r)))
        stored += 1
    if stored:
        conn.commit()
    return stored


def missing_days(conn, now: datetime, back_days: int = BACKFILL_DAYS) -> list[date]:
    """Ostatnie pełne doby (bez dzisiejszej) bez zapisanych runów w bieżącej wersji rekordu, od najstarszej.
    Doba bez żadnego runu (klimatyzacja nie pracowała) jest ponawiana — tanio, bo recorder odpowiada raz na dobę."""
    have = {r["d"] for r in conn.execute(
        "SELECT substr(start, 1, 10) AS d FROM attic_runs GROUP BY d "
        "HAVING MIN(COALESCE(json_extract(data, '$.v'), 1)) >= ?", (DATA_VERSION,))}
    today = now.date()
    days = [today - timedelta(days=i) for i in range(1, back_days + 1)]
    return [d for d in sorted(days) if d.isoformat() not in have]


def load_runs(conn, days: int = 60) -> list[dict]:
    """Runy zapisane w ostatnich `days` dniach, najnowsze pierwsze."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT data FROM attic_runs WHERE start >= ? ORDER BY start DESC", (since,)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def summarize(runs: list[dict], tz: ZoneInfo) -> list[dict]:
    """Podsumowanie doby (data lokalna startu runu): suma kWh i zł, liczba runów, godziny pracy. Najnowsze pierwsze;
    pole bez wartości (None) nie wchodzi do sumy."""
    days: dict[str, dict] = {}
    for r in runs:
        start = datetime.fromisoformat(r["start"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        d = days.setdefault(start.astimezone(tz).date().isoformat(), {"kwh": 0.0, "pln": 0.0, "runs": 0, "hours": 0.0})
        d["kwh"] += r.get("energy_kwh") or 0.0
        d["pln"] += r.get("cost_pln") or 0.0
        d["runs"] += 1
        d["hours"] += (r.get("duration_min") or 0.0) / 60.0
    return [{"day": k, "kwh": round(v["kwh"], 3), "pln": round(v["pln"], 2), "runs": v["runs"],
             "hours": round(v["hours"], 2)} for k, v in sorted(days.items(), reverse=True)]
