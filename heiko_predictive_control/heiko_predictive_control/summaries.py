"""Dobowe streszczenia pracy pompy z historii stanów HA (recorder trzyma ~7 dni, a cykle
sprężarki i CWU są potrzebne dłużej) — funkcje czyste + jedna funkcja I/O (`store_day`).

Tryb pracy to KOD liczbowy z `sensor…working_mode`: 0 Standby, 1 CWU, 2 grzanie, 3 chłodzenie,
4 CWU+grzanie, 5 CWU+chłodzenie (integracja `heiko_heatpump`, protocol.py)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Callable
from zoneinfo import ZoneInfo

from . import catalog
from . import energy_source as es
from . import ha_client
from . import tariff_feed
from .tariff import default_is_workday, fallback_hour

logger = logging.getLogger(__name__)

DHW_CODES = (1, 4, 5)
HEAT_CODES = (2, 4)
SHORT_CYCLE_MIN = 10.0
SPREAD_MIN_SAMPLES = 8               # próbek telemetrii co 15 min w standby (2 h), poniżej nie liczymy rozrzutu

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
    if not pts:
        return None
    if len(pts) == 1:                      # licznik nie zmienił się w dobie (HA zwraca tylko stan początkowy) → przyrost 0
        return 0.0
    inside = [v for t, v in pts if t >= start]
    before = [v for t, v in pts if t < start]
    first = before[-1] if before else (inside[0] if inside else None)
    last = inside[-1] if inside else None
    if first is None or last is None or last < first:
        return None
    return last - first


def _energy(series: Series, start: datetime, end: datetime, tz: ZoneInfo, is_workday: Callable[[date], bool],
            grid: Series | None = None, feed: tariff_feed.TariffFeed | None = None) -> dict | None:
    """Energia z licznika: przyrost między kolejnymi próbkami przypisany do godziny lokalnej próbki końcowej.
    Z historią licznika poboru z sieci (`grid`) do szczytu liczy się tylko część pokryta z sieci."""
    pts = sorted((t, _num(s)) for t, s in series if _num(s) is not None and t < end)
    imported = es.Counter((t, _num(s)) for t, s in grid or [])
    total = peak = 0.0
    prev = prev_t = None
    for t, v in pts:
        if prev is not None and t > start and v >= prev:
            inc = v - prev
            local = t.astimezone(tz)
            total += inc
            hour_peak = (feed.is_peak(local.replace(tzinfo=None)) if feed is not None
                         else fallback_hour(local, is_workday(local.date())).is_peak)
            if hour_peak:
                d = imported.delta(prev_t, t)
                peak += inc if d is None else es.split(inc, d)[0]
        prev, prev_t = v, t
    if not pts:
        return None
    return {"kwh": round(total, 3), "kwh_peak": round(peak, 3),
            "peak_share": round(peak / total, 4) if total > 0 else None}


def summarize_day(day: date, series: dict[str, Series], tz_name: str,
                  is_workday: Callable[[date], bool] = default_is_workday,
                  feed: tariff_feed.TariffFeed | None = None) -> dict[str, dict]:
    """Streszczenia doby per temat. `series`: role → historia: mode, freq, hbh, hwtbh, p1, energy.
    Klucz `backup`: ah_min/hbh_min/hwtbh_min (minuty). Zwraca {} gdy nie ma danych trybu ani sprężarki (doba poza zasięgiem recordera)."""
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
    # Liczniki czasu pracy grzałek w integracji są w MINUTACH (AH = grzałka pomocnicza jednostki wewnętrznej,
    # HBH = rezerwowa w buforze, HWTBH = rezerwowa zbiornika CWU).
    backup = {"ah_min": _delta(series.get("ah", []), start, end), "hbh_min": _delta(series.get("hbh", []), start, end),
              "hwtbh_min": _delta(series.get("hwtbh", []), start, end)}
    if any(v is not None for v in backup.values()):
        out["backup"] = {k: None if v is None else round(v, 1) for k, v in backup.items()}
    p1 = _segments(series.get("p1", []), start, end)
    if p1:
        runs = _runs(p1, lambda v: v > 0)
        out["p0"] = {"pulses": len(runs), "on_min": round(sum(runs), 1),
                     "mean_pulse_min": round(sum(runs) / len(runs), 1) if runs else None}
    energy = _energy(series.get("energy", []), start, end, tz, is_workday, series.get("grid"), feed)
    if energy:
        out["energy"] = energy
    return out


def standby_spread(mode: dict[int, float], zones: dict[int, dict[str, float]], n_zones: int) -> dict | None:
    """Średni rozrzut (max − min) temperatur stref w próbkach, gdy pompa stoi w standby (tryb 0).
    Liczą się tylko próbki ze WSZYSTKIMI strefami (zmienny skład dawałby sztuczne skoki). Miara testu P0."""
    spreads, avgs = [], []
    for ts, code in mode.items():
        vals = zones.get(ts) or {}
        if int(round(code)) != 0 or len(vals) < n_zones:
            continue
        spreads.append(max(vals.values()) - min(vals.values()))
        avgs.append(mean(vals.values()))
    if len(spreads) < SPREAD_MIN_SAMPLES:
        return None
    return {"spread_c": round(mean(spreads), 2), "zones_avg_c": round(mean(avgs), 2), "samples": len(spreads)}


def _telemetry_day(conn, names: list[str], start_ts: int, end_ts: int) -> dict[str, dict[int, float]]:
    rows = conn.execute(
        f"SELECT t.ts AS ts, k.name AS name, t.value AS value FROM telemetry t JOIN telemetry_keys k ON k.id = t.key_id "
        f"WHERE t.ts >= ? AND t.ts < ? AND t.value IS NOT NULL AND k.name IN ({','.join('?' * len(names))})",
        (start_ts, end_ts, *names)).fetchall()
    out: dict[str, dict[int, float]] = {n: {} for n in names}
    for r in rows:
        out[r["name"]][r["ts"]] = r["value"]
    return out


def zone_samples(conn, settings: dict, start_ts: int, end_ts: int):
    """(tryb pracy, temperatury stref wg czasu, liczba stref) z telemetrii w oknie — wejście `standby_spread`.
    None, gdy nie wskazano stref dnia."""
    zones = [e.strip() for e in str(settings.get("day_zone_temp_entities") or "").split(",") if e.strip()]
    if not zones:
        return None
    tel = _telemetry_day(conn, ["working_mode", *zones], start_ts, end_ts)
    by_ts: dict[int, dict[str, float]] = {}
    for name in zones:
        for ts, value in tel[name].items():
            by_ts.setdefault(ts, {})[name] = value
    return tel["working_mode"], by_ts, len(zones)


P0_KEYS = (("run_min", "p0_run_time"), ("stop_min", "p0_stop_time"))


def p0_setting(conn, end: datetime, states: list[dict]) -> dict | None:
    """Nastawa P0 (praca/postój, min) na koniec doby. Encje `number` nie są w recorderze, więc: pierwsza zmiana
    po końcu doby w `param_changes` zwraca swoje `old`; bez późniejszej zmiany obowiązuje bieżący stan."""
    entities = catalog.resolve_params(states)
    state_of = {st.get("entity_id"): st.get("state") for st in states}
    out = {}
    for field, key in P0_KEYS:
        value = _num(state_of.get(entities.get(key)))
        for row in conn.execute("SELECT ts, old FROM param_changes WHERE key = ? ORDER BY ts", (key,)):
            when = datetime.fromisoformat(row["ts"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= end:
                value = _num(row["old"])
                break
        if value is None:
            return None
        out[field] = value
    return out


def store_day(conn, settings: dict, day: date, states: list[dict], now: datetime,
              get_history=ha_client.get_history, is_workday: Callable[[date], bool] | None = None) -> dict[str, dict]:
    """Pobiera historię doby z HA i zapisuje streszczenia (INSERT OR REPLACE). Pusty wynik = nic nie zapisujemy."""
    tel = catalog.resolve_telemetry(states)
    roles = {"mode": tel.get("working_mode"), "freq": tel.get("compressor_frequency"),
             "ah": tel.get("ah_working_time"), "hbh": tel.get("hbh_working_time"),
             "hwtbh": tel.get("hwtbh_working_time"),
             "p1": tel.get("water_pump_p1"), "energy": settings.get("pump_energy_entity") or None,
             "grid": es.entities(settings)["grid_import"]
                     if settings.get("pump_energy_entity") else None}
    entities = [e for e in roles.values() if e]
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    start, end = day_bounds(day, tz_name)
    hist = get_history(entities, start.isoformat(), end.isoformat())
    if not hist:
        return {}
    series = {role: hist.get(eid, []) for role, eid in roles.items() if eid}
    is_workday = is_workday or default_is_workday
    summary = summarize_day(day, series, tz_name, is_workday, tariff_feed.for_settings(conn, settings, is_workday))
    if summary:
        extra = {"setting": p0_setting(conn, end, states)}
        samples = zone_samples(conn, settings, int(start.timestamp()), int(end.timestamp()))
        if samples:
            extra["standby_spread"] = standby_spread(*samples)
        if any(extra.values()):
            summary.setdefault("p0", {}).update({k: v for k, v in extra.items() if v})
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
