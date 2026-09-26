"""Dobowe streszczenia pracy pompy z historii stanów HA (recorder trzyma ~7 dni, a cykle
sprężarki i CWU są potrzebne dłużej) — funkcje czyste + jedna funkcja I/O (`store_day`).

Tryb pracy to KOD liczbowy z `sensor…working_mode`: 0 Standby, 1 CWU, 2 grzanie, 3 chłodzenie,
4 CWU+grzanie, 5 CWU+chłodzenie (integracja `heiko_heatpump`, protocol.py)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import catalog
from . import ha_client
from .tariff import is_peak_hour

logger = logging.getLogger(__name__)

DHW_CODES = (1, 4, 5)
HEAT_CODES = (2, 4)
SHORT_CYCLE_MIN = 10.0

Series = list[tuple[datetime, str]]


def _num(state) -> float | None:
    return catalog.to_number(state)


def day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    return start.astimezone(timezone.utc), datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(timezone.utc)


def _segments(series: Series, start: datetime, end: datetime) -> list[tuple[datetime, datetime, float | None]]:
    """Odcinki stałej wartości numerycznej w [start, end). Wartość sprzed `start` nosi się do przodu;
    brak danych przed pierwszą próbką → odcinek zaczyna się w pierwszej próbce."""
    pts = sorted((t, _num(s)) for t, s in series)
    out = []
    cur_t, cur_v = None, None
    for t, v in pts:
        if t <= start:
            cur_t, cur_v = start, v
            continue
        if t >= end:
            break
        if cur_t is not None:
            out.append((cur_t, t, cur_v))
        cur_t, cur_v = t, v
    if cur_t is not None and cur_t < end:
        out.append((cur_t, end, cur_v))
    return out


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def _runs(segments, predicate) -> list[float]:
    """Długości (min) ciągłych okresów, w których predicate(wartość) jest prawdziwe."""
    runs, cur = [], 0.0
    for a, b, v in segments:
        if v is not None and predicate(v):
            cur += _minutes(a, b)
        elif cur:
            runs.append(cur)
            cur = 0.0
    if cur:
        runs.append(cur)
    return runs


def _delta(series: Series, start: datetime, end: datetime) -> float | None:
    """Przyrost licznika w dobie (ostatnia − pierwsza wartość w oknie/przed nim); reset licznika → None."""
    pts = sorted((t, _num(s)) for t, s in series if _num(s) is not None and t < end)
    if len(pts) < 2:
        return None
    inside = [v for t, v in pts if t >= start]
    before = [v for t, v in pts if t < start]
    first = before[-1] if before else (inside[0] if inside else None)
    last = inside[-1] if inside else None
    if first is None or last is None or last < first:
        return None
    return last - first


def _energy(series: Series, start: datetime, end: datetime, tz: ZoneInfo, is_workday: Callable[[date], bool]
            ) -> dict | None:
    """Energia z licznika: przyrost między kolejnymi próbkami przypisany do godziny lokalnej próbki końcowej."""
    pts = sorted((t, _num(s)) for t, s in series if _num(s) is not None and t < end)
    total = peak = 0.0
    prev = None
    for t, v in pts:
        if prev is not None and t > start and v >= prev:
            inc = v - prev
            local = t.astimezone(tz)
            total += inc
            if is_peak_hour(local.hour, is_workday(local.date())):
                peak += inc
        prev = v
    if not pts:
        return None
    return {"kwh": round(total, 3), "kwh_peak": round(peak, 3),
            "peak_share": round(peak / total, 4) if total > 0 else None}


def summarize_day(day: date, series: dict[str, Series], tz_name: str,
                  is_workday: Callable[[date], bool] = lambda d: d.weekday() < 5) -> dict[str, dict]:
    """Streszczenia doby per temat. `series`: role → historia: mode, freq, hbh, hwtbh, p1, energy.
    Zwraca {} gdy nie ma danych trybu ani sprężarki (doba poza zasięgiem recordera)."""
    start, end = day_bounds(day, tz_name)
    tz = ZoneInfo(tz_name)
    out: dict[str, dict] = {}
    mode = _segments(series.get("mode", []), start, end)
    freq = _segments(series.get("freq", []), start, end)
    if not mode and not freq:
        return out

    if mode:
        dhw = _runs(mode, lambda v: int(round(v)) in DHW_CODES)
        heat = _runs(mode, lambda v: int(round(v)) in HEAT_CODES)
        out["modes"] = {"dhw_min": round(sum(dhw), 1), "dhw_cycles": len(dhw),
                        "heating_min": round(sum(heat), 1), "heating_runs": len(heat),
                        "standby_min": round(sum(_minutes(a, b) for a, b, v in mode
                                                 if v is not None and int(round(v)) == 0), 1)}
    if freq:
        runs = _runs(freq, lambda v: v > 0)
        weighted = sum(_minutes(a, b) * v for a, b, v in freq if v is not None and v > 0)
        run_min = sum(runs)
        out["compressor"] = {"starts": len(runs), "run_min": round(run_min, 1),
                             "short_cycles": sum(1 for r in runs if r < SHORT_CYCLE_MIN),
                             "mean_run_min": round(run_min / len(runs), 1) if runs else None,
                             "mean_hz": round(weighted / run_min, 1) if run_min else None}
    backup = {"hbh_h": _delta(series.get("hbh", []), start, end), "hwtbh_h": _delta(series.get("hwtbh", []), start, end)}
    if any(v is not None for v in backup.values()):
        out["backup"] = {k: None if v is None else round(v, 2) for k, v in backup.items()}
    p1 = _segments(series.get("p1", []), start, end)
    if p1:
        runs = _runs(p1, lambda v: v > 0)
        out["p0"] = {"pulses": len(runs), "on_min": round(sum(runs), 1),
                     "mean_pulse_min": round(sum(runs) / len(runs), 1) if runs else None}
    energy = _energy(series.get("energy", []), start, end, tz, is_workday)
    if energy:
        out["energy"] = energy
    return out


def store_day(conn, settings: dict, day: date, states: list[dict], now: datetime,
              get_history=ha_client.get_history, is_workday: Callable[[date], bool] | None = None) -> dict[str, dict]:
    """Pobiera historię doby z HA i zapisuje streszczenia (INSERT OR REPLACE). Pusty wynik = nic nie zapisujemy."""
    tel = catalog.resolve_telemetry(states)
    roles = {"mode": tel.get("working_mode"), "freq": tel.get("compressor_frequency"),
             "hbh": tel.get("hbh_working_time"), "hwtbh": tel.get("hwtbh_working_time"),
             "p1": tel.get("water_pump_p1"), "energy": settings.get("pump_energy_entity") or None}
    entities = [e for e in roles.values() if e]
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    start, end = day_bounds(day, tz_name)
    hist = get_history(entities, start.isoformat(), end.isoformat())
    if not hist:
        return {}
    series = {role: hist.get(eid, []) for role, eid in roles.items() if eid}
    summary = summarize_day(day, series, tz_name, is_workday or (lambda d: d.weekday() < 5))
    for topic, data in summary.items():
        conn.execute("INSERT OR REPLACE INTO daily_summary (day, topic, data) VALUES (?, ?, ?)",
                     (day.isoformat(), topic, json.dumps(data)))
    conn.commit()
    return summary


def missing_days(conn, now: datetime, back_days: int = 7) -> list[date]:
    """Ostatnie pełne doby (bez dzisiejszej), dla których nie ma jeszcze streszczeń — od najstarszej."""
    have = {r["day"] for r in conn.execute("SELECT DISTINCT day FROM daily_summary")}
    today = now.date()
    days = [today - timedelta(days=i) for i in range(1, back_days + 1)]
    return [d for d in sorted(days) if d.isoformat() not in have]


def load_summaries(conn, days: int = 30) -> dict[str, dict[str, dict]]:
    """{dzień: {temat: dane}} z ostatnich `days` dni (do raportu)."""
    rows = conn.execute("SELECT day, topic, data FROM daily_summary ORDER BY day DESC LIMIT ?",
                        (days * 8,)).fetchall()
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["day"], {})[r["topic"]] = json.loads(r["data"])
    return dict(sorted(out.items()))
