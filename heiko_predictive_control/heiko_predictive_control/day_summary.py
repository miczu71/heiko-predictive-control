"""Podsumowanie doby (E3, `docs/PLAN_heiko_ogrzewanie_E3.md`): kWh / zł / udział PV z podziałem na podłogówkę,
CWU i klimatyzację. Wczoraj z zapisanych rekordów (heating_runs, dhw_cycles, attic_runs), dziś na żywo z recordera
HA (jedno zapytanie, cache 300 s). Rekord liczy się do doby lokalnej daty startu. **Tylko odczyt**; zero zapisów
do pompy i HA."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from . import attic_runs, catalog, dhw_cycles, ha_client, heating_runs, tariff_feed
from .dhw_cycles import battery_counter, source_roles
from .summaries import day_bounds
from .tariff import default_is_workday

logger = logging.getLogger(__name__)

CACHE_TTL_S = 300
CACHE: dict = {}                       # współdzielony cache „dziś”: klucz = data lokalna -> (czas, wynik)
ERROR_NO_HISTORY = "brak historii z HA"


def local_date(iso: str, tz) -> date:
    return datetime.fromisoformat(iso).astimezone(tz).date()


def aggregate(records: list[dict], kwh_key: str) -> dict:
    """Suma rekordów jednego składnika. Pola `_pv_kwh`/`_known_kwh` (tylko rekordy `split_complete`) służą
    `combine`; `public` je usuwa."""
    kwh = pln = pv_all = known = 0.0
    zones: dict[str, dict] = {}
    approx = False
    for r in records:
        k, cost, zn = r.get(kwh_key), r.get("cost_pln"), r.get("zones")
        # cykle CWU nie mają `split_complete`: pv_share ustawione (albo zerowa energia) = podział pełny
        split = r.get("split_complete")
        if split is None:
            split = r.get("pv_share") is not None or (k is not None and k <= 0 and cost is not None)
        if k is not None:
            kwh += k
        if cost is not None:
            pln += cost
        if zn:
            for name, z in zn.items():
                acc = zones.setdefault(name, {"kwh": 0.0, "pln": 0.0, "pv_kwh": 0.0})
                for f in ("kwh", "pln", "pv_kwh"):
                    acc[f] += z.get(f) or 0.0
        if split and k is not None and zn is not None:
            known += k
            pv_all += sum(z.get("pv_kwh") or 0.0 for z in zn.values())
        if not split or cost is None or zn is None or r.get("complete") is False:
            approx = True
    return {"kwh": kwh, "pln": pln, "zones": zones, "runs": len(records), "approx": approx,
            "_pv_kwh": pv_all, "_known_kwh": known}


def combine(parts: list[dict]) -> dict:
    """`total` z wyników `aggregate`: sumy, zones scalone po strefie, `approx` = OR."""
    zones: dict[str, dict] = {}
    for p in parts:
        for name, z in p["zones"].items():
            acc = zones.setdefault(name, {"kwh": 0.0, "pln": 0.0, "pv_kwh": 0.0})
            for f in ("kwh", "pln", "pv_kwh"):
                acc[f] += z[f]
    return {"kwh": sum(p["kwh"] for p in parts), "pln": sum(p["pln"] for p in parts), "zones": zones,
            "runs": sum(p["runs"] for p in parts), "approx": any(p["approx"] for p in parts),
            "_pv_kwh": sum(p["_pv_kwh"] for p in parts), "_known_kwh": sum(p["_known_kwh"] for p in parts)}


def public(d: dict) -> dict:
    """Wynik do JSON: zaokrąglenia (kWh 3, zł 2, pv_share 3), `pv_share`, bez kluczy pomocniczych `_*`."""
    known = d.get("_known_kwh", 0.0)
    out = {k: v for k, v in d.items() if not k.startswith("_")}
    out["kwh"] = round(d["kwh"], 3)
    out["pln"] = round(d["pln"], 2)
    out["pv_share"] = round(d["_pv_kwh"] / known, 3) if known > 0 else None
    out["zones"] = {n: {"kwh": round(z["kwh"], 3), "pln": round(z["pln"], 2), "pv_kwh": round(z["pv_kwh"], 3)}
                    for n, z in d["zones"].items()}
    return out


def _configured(settings: dict) -> dict[str, bool]:
    return {"floor": bool(settings.get("pump_energy_entity")),
            "dhw": bool(settings.get("pump_energy_entity") or settings.get("ah_energy_entity")),
            "ac": bool(settings.get("attic_power_entity"))}


def _result(day: date, parts: dict[str, dict], configured: dict[str, bool], now: datetime,
            in_progress: bool = False, error: str | None = None) -> dict:
    out: dict = {"day": day.isoformat()}
    for name in ("floor", "dhw", "ac"):
        out[name] = {**public(parts[name]), "configured": configured[name]}
    out["total"] = public(combine([parts["floor"], parts["dhw"], parts["ac"]]))
    out["in_progress"] = in_progress
    out["generated_at"] = now.isoformat()
    if error:
        out["error"] = error
    return out


def _tz(settings: dict) -> tuple[str, ZoneInfo]:
    name = settings.get("timezone") or "Europe/Warsaw"
    return name, ZoneInfo(name)


def build_yesterday(conn, settings: dict, now: datetime) -> dict:
    """Wczoraj z zapisanych rekordów (bez cache — tanie, z SQLite)."""
    _, tz = _tz(settings)
    day = now.astimezone(tz).date() - timedelta(days=1)

    def pick(records):
        return [r for r in records if local_date(r["start"], tz) == day]

    parts = {"floor": aggregate(pick(heating_runs.load_runs(conn, 3)), "energy_kwh"),
             "dhw": aggregate(pick(dhw_cycles.load_cycles(conn, 3)), "kwh_total"),
             "ac": aggregate(pick(attic_runs.load_runs(conn, 3)), "energy_kwh")}
    return _result(day, parts, _configured(settings), now)


def build_today(conn, settings: dict, states: list[dict], now: datetime,
                get_history=ha_client.get_history,
                is_workday: Callable[[date], bool] = default_is_workday,
                cache: dict | None = None) -> dict:
    """Dziś na żywo: jedno zapytanie do recordera od północ − 3 h do `now`. Rekordy w toku (`complete: False`)
    wchodzą do sumy i ustawiają `in_progress`. Brak historii → składniki zerowe, `approx`, `error`."""
    tz_name, tz = _tz(settings)
    now_l = now.astimezone(tz)
    today = now_l.date()
    key = today.isoformat()
    if cache is not None:
        hit = cache.get(key)
        if hit and 0 <= (now - hit[0]).total_seconds() < CACHE_TTL_S:
            return hit[1]
    configured = _configured(settings)
    empty = {n: aggregate([], "energy_kwh") for n in ("floor", "dhw", "ac")}
    if not any(configured.values()):
        return _result(today, empty, configured, now)

    tel = catalog.resolve_telemetry(states)
    params = catalog.resolve_params(states)
    common = {"water": tel.get("water_temperature"), "outdoor": tel.get("ambient_air_temperature")}
    pump = settings.get("pump_energy_entity") or None
    roles_floor = {"mode": tel.get("working_mode"), "energy": pump, **common} if configured["floor"] else {}
    roles_dhw = ({"mode": tel.get("working_mode"), "target": params.get("dhw_setpoint"),
                  "ah": tel.get("ah_working_time"), "energy_c": settings.get("ah_energy_entity") or None,
                  "energy_total": pump, **common} if configured["dhw"] else {})
    roles_ac = ({"power": settings.get("attic_power_entity"), "energy": settings.get("attic_energy_entity") or None,
                 "temp": settings.get("attic_temp_entity") or None, "outdoor": common["outdoor"]}
                if configured["ac"] else {})
    sources = source_roles(settings)
    all_roles = [roles_floor, roles_dhw, roles_ac, sources]
    entities = sorted({e for roles in all_roles for e in roles.values() if e})

    day_start, _ = day_bounds(today, tz_name)
    fetch_start = day_start - timedelta(hours=3)
    try:
        hist = get_history(entities, fetch_start.isoformat(), now.isoformat())
    except Exception:
        logger.warning("day_summary: historia z HA niedostępna", exc_info=True)
        hist = None
    if not hist:
        zero = {n: aggregate([], "energy_kwh") for n in empty}
        for n in zero:
            zero[n]["approx"] = configured[n]
        return _result(today, zero, configured, now, error=ERROR_NO_HISTORY)

    def series(roles):
        s = {role: hist.get(eid, []) for role, eid in roles.items() if eid}
        s.update({role: hist.get(eid, []) for role, eid in sources.items() if eid})
        return s

    battery = battery_counter(conn, tz, fetch_start, now)
    feed = tariff_feed.for_settings(conn, settings, is_workday)
    kw = dict(feed=feed, offpeak_price=feed.offpeak_price(day_start.astimezone(tz).replace(tzinfo=None)),
              battery_cost_at=lambda t: battery.at(t.astimezone(tz).replace(tzinfo=None)))

    def mine(records):
        return [r for r in records if local_date(r["start"], tz) == today]

    floor = mine(heating_runs.extract_runs(series(roles_floor), fetch_start, now, tz, is_workday, **kw)) \
        if configured["floor"] else []
    dhw = mine(dhw_cycles.extract_cycles(series(roles_dhw), fetch_start, now, tz, is_workday,
                                         bath_entries=None, **kw)) if configured["dhw"] else []
    ac = mine(attic_runs.extract_runs(series(roles_ac), fetch_start, now, tz, is_workday, **kw)) \
        if configured["ac"] else []
    parts = {"floor": aggregate(floor, "energy_kwh"), "dhw": aggregate(dhw, "kwh_total"),
             "ac": aggregate(ac, "energy_kwh")}
    in_progress = any(r.get("complete") is False for r in floor + dhw + ac)
    result = _result(today, parts, configured, now, in_progress=in_progress)
    if cache is not None:
        cache.clear()
        cache[key] = (now, result)
    return result

