"""Koszty (K2, `docs/PLAN_heiko_koszty_K2.md`): kWh / zł / udział PV z zapisanych rekordów (heating_runs, dhw_cycles,
attic_runs) zagregowane w koszyki doba / tydzień ISO / miesiąc, z porównaniem do poprzedniego okresu tej samej długości.
Rekord należy do doby lokalnej swojego startu. **Tylko odczyt**: zero zapisów do pompy, HA i bazy."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from . import attic_runs, dhw_cycles, heating_runs
from .day_summary import _tz, aggregate, combine, local_date, public
from .summaries import day_bounds

GROUPS = ("auto", "day", "week", "month")
AUTO_DAY_MAX = 62                      # ≤ 62 dni -> doby
AUTO_WEEK_MAX = 182                    # ≤ 182 dni -> tygodnie, dalej miesiące
FETCH_MARGIN = timedelta(hours=12)
ZONES_MIN_VERSION = 7                  # `v` cyklu CWU od wersji ze strefami taryfy (`dhw_cycles.DATA_VERSION` = 7)
OUTDOOR_KEY = "ambient_air_temperature"
COMPONENTS = (("floor", heating_runs, "energy_kwh"),
              ("dhw", dhw_cycles, "kwh_total"),
              ("ac", attic_runs, "energy_kwh"))
NAMES = tuple(c[0] for c in COMPONENTS)


def _resolve_group(group: str, days: int) -> str:
    if group not in GROUPS:
        raise ValueError(f"group musi być jednym z: {', '.join(GROUPS)}")
    if group != "auto":
        return group
    return "day" if days <= AUTO_DAY_MAX else "week" if days <= AUTO_WEEK_MAX else "month"


def _bucket(day: date, group: str) -> tuple[str, str]:
    """(klucz, etykieta) koszyka: doba = data, tydzień ISO = poniedziałek, miesiąc = 1. dzień."""
    if group == "day":
        return day.isoformat(), day.isoformat()
    if group == "week":
        monday = day - timedelta(days=day.weekday())
        iso = monday.isocalendar()
        return monday.isoformat(), f"{iso[0]}-W{iso[1]:02d}"
    first = day.replace(day=1)
    return first.isoformat(), f"{first.year}-{first.month:02d}"


def _with_unsplit(agg: dict) -> dict:
    out = public(agg)
    out["unsplit_kwh"] = round(max(0.0, agg["kwh"] - sum(z["kwh"] for z in agg["zones"].values())), 3)
    return out


def _publish(parts: dict[str, dict]) -> dict:
    """floor/dhw/ac + total (public + unsplit_kwh) z agregatów składników."""
    out = {name: _with_unsplit(parts[name]) for name in NAMES}
    out["total"] = _with_unsplit(combine([parts[name] for name in NAMES]))
    return out


def _from_public(part: dict) -> dict:
    """Wynik `public` (doba „dziś” z `build_today`) z powrotem do postaci `aggregate`, by wszedł do sum.
    Podział PV przybliżony: `pv_share` liczone od całej energii (bez `_known_kwh` w wyniku publicznym)."""
    share = part.get("pv_share")
    kwh = part.get("kwh") or 0.0
    zones = {n: {"kwh": z.get("kwh") or 0.0, "pln": z.get("pln") or 0.0, "pv_kwh": z.get("pv_kwh") or 0.0}
             for n, z in (part.get("zones") or {}).items()}
    return {"kwh": kwh, "pln": part.get("pln") or 0.0, "zones": zones, "runs": part.get("runs") or 0,
            "approx": bool(part.get("approx")), "_pv_kwh": share * kwh if share is not None else 0.0,
            "_known_kwh": kwh if share is not None else 0.0}


def _load_records(conn, tz_name: str, tz, start: date, end: date) -> dict[str, dict[date, list[dict]]]:
    """{składnik: {doba lokalna: [rekordy]}} dla dób [start, end]; rekord liczy się do doby lokalnej startu."""
    lo = day_bounds(start, tz_name)[0] - FETCH_MARGIN
    hi = day_bounds(end, tz_name)[1]
    out: dict[str, dict[date, list[dict]]] = {}
    for name, module, _ in COMPONENTS:
        by_day: dict[date, list[dict]] = {}
        for r in module.load_range(conn, lo, hi):
            d = local_date(r["start"], tz)
            if start <= d <= end:
                by_day.setdefault(d, []).append(r)
        out[name] = by_day
    return out


def _outdoor_telemetry(conn, tz_name: str, tz, start: date, end: date, group: str) -> dict[str, float]:
    """Średnia `ambient_air_temperature` per koszyk: próbki surowe (waga 1) + agregaty godzinowe (waga `n`)."""
    lo, hi = int(day_bounds(start, tz_name)[0].timestamp()), int(day_bounds(end, tz_name)[1].timestamp())
    sums: dict[str, list[float]] = {}

    def add(ts: int, total: float, weight: float):
        key = _bucket(datetime.fromtimestamp(ts, tz).date(), group)[0]
        acc = sums.setdefault(key, [0.0, 0.0])
        acc[0] += total
        acc[1] += weight

    for r in conn.execute("SELECT t.ts AS ts, t.value AS value FROM telemetry t JOIN telemetry_keys k ON k.id = t.key_id "
                          "WHERE k.name = ? AND t.ts >= ? AND t.ts < ? AND t.value IS NOT NULL", (OUTDOOR_KEY, lo, hi)):
        add(r["ts"], r["value"], 1)
    for r in conn.execute("SELECT h.hour_ts AS ts, h.mean AS mean, h.n AS n FROM telemetry_hourly h "
                          "JOIN telemetry_keys k ON k.id = h.key_id WHERE k.name = ? AND h.hour_ts >= ? AND h.hour_ts < ? "
                          "AND h.mean IS NOT NULL AND h.n > 0", (OUTDOOR_KEY, lo, hi)):
        add(r["ts"], r["mean"] * r["n"], r["n"])
    return {k: v[0] / v[1] for k, v in sums.items() if v[1] > 0}


def _coverage(conn, tz) -> dict:
    def oldest(table: str) -> str | None:
        row = conn.execute(f"SELECT MIN(start) AS s FROM {table}").fetchone()
        return local_date(row["s"], tz).isoformat() if row and row["s"] else None

    row = conn.execute("SELECT start FROM dhw_cycles WHERE json_extract(data, '$.v') >= ? ORDER BY start LIMIT 1",
                       (ZONES_MIN_VERSION,)).fetchone()
    zones_from = local_date(row["start"], tz).isoformat() if row else None
    return {"floor": {"oldest": oldest("heating_runs")},
            "dhw": {"oldest": oldest("dhw_cycles"), "zones_from": zones_from},
            "ac": {"oldest": oldest("attic_runs")}}


def _totals(conn, tz_name: str, tz, start: date, end: date) -> dict:
    """Sumy zapisanych rekordów okresu (bez „dziś”) — do okresu poprzedniego."""
    records = _load_records(conn, tz_name, tz, start, end)
    parts = {name: aggregate([r for day in sorted(records[name]) for r in records[name][day]], kwh_key)
             for name, _, kwh_key in COMPONENTS}
    return _publish(parts)


def _delta(cur: dict, prev: dict) -> dict:
    out = {}
    for name in (*NAMES, "total"):
        c, p = cur[name], prev[name]
        pp = None if c["pv_share"] is None or p["pv_share"] is None else round((c["pv_share"] - p["pv_share"]) * 100, 1)
        out[name] = {"pln": round(c["pln"] - p["pln"], 2), "kwh": round(c["kwh"] - p["kwh"], 3), "pv_share_pp": pp}
    return out


def build(conn, settings: dict, start: date, end: date, now: datetime, group: str = "auto",
          today_fn: Callable[[], dict] | None = None) -> dict:
    """Koszty za [start, end] (doby lokalne, włącznie). `today_fn` (wynik jak `day_summary.build_today`) dostarcza
    dzisiejszą dobę, gdy `end` to dziś; bez niego dziś liczy się z zapisanych rekordów. `ValueError` dla złych parametrów."""
    if start > end:
        raise ValueError("from musi być <= to")
    days = (end - start).days + 1
    group = _resolve_group(group, days)
    tz_name, tz = _tz(settings)
    today = now.astimezone(tz).date()
    use_today = today_fn is not None and start <= today <= end
    today_parts = None
    if use_today:
        res = today_fn()
        today_parts = {name: _from_public(res.get(name) or {}) for name in NAMES}

    records = _load_records(conn, tz_name, tz, start, end)
    outdoor = _outdoor_telemetry(conn, tz_name, tz, start, end, group)
    grouped: dict[str, dict] = {}          # klucz koszyka -> {label, in_progress, recs}; kolejność wstawiania = chronologia
    for i in range(days):
        day = start + timedelta(days=i)
        key, label = _bucket(day, group)
        b = grouped.setdefault(key, {"label": label, "in_progress": False, "recs": {name: [] for name in NAMES}})
        if use_today and day == today:
            b["in_progress"] = True
            continue                       # dzisiejsze rekordy ignorujemy na rzecz `today_fn`
        for name in NAMES:
            b["recs"][name].extend(records[name].get(day, []))

    buckets, bucket_parts = [], []
    for key, b in grouped.items():
        recs = b["recs"]
        parts = {name: aggregate(recs[name], kwh_key) for name, _, kwh_key in COMPONENTS}
        if use_today and _bucket(today, group)[0] == key:
            parts = {name: combine([parts[name], today_parts[name]]) for name in parts}
        bucket_parts.append(parts)
        fallback = [r["outdoor_c"] for name in NAMES for r in recs[name] if r.get("outdoor_c") is not None]
        temp = outdoor.get(key)
        if temp is None and fallback:
            temp = sum(fallback) / len(fallback)
        buckets.append({"key": key, "label": b["label"], "in_progress": b["in_progress"],
                        "outdoor_c": None if temp is None else round(temp, 1), **_publish(parts)})

    totals = _publish({name: combine([p[name] for p in bucket_parts]) for name in NAMES})
    prev_start = start - timedelta(days=days)
    prev_end = start - timedelta(days=1)
    prev_totals = _totals(conn, tz_name, tz, prev_start, prev_end)
    has_prev = prev_totals["total"]["runs"] > 0
    return {"from": start.isoformat(), "to": end.isoformat(), "group": group, "days": days,
            "buckets": buckets, "totals": totals,
            "previous": {"from": prev_start.isoformat(), "to": prev_end.isoformat(), "totals": prev_totals}
            if has_prev else None,
            "delta": _delta(totals, prev_totals) if has_prev else None,
            "coverage": _coverage(conn, tz), "generated_at": now.isoformat()}
