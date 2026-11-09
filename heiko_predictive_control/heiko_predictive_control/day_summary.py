"""Podsumowanie doby (E3, `docs/PLAN_heiko_ogrzewanie_E3.md`): kWh / zł / udział PV z podziałem na podłogówkę,
CWU i klimatyzację. Wczoraj z zapisanych rekordów (heating_runs, dhw_cycles, attic_runs), dziś na żywo z recordera
HA (jedno zapytanie, cache 300 s). Rekord liczy się do doby lokalnej daty startu. **Tylko odczyt**; zero zapisów
do pompy i HA."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import attic_runs, catalog, dhw_cycles, ha_client, heating_runs, tariff_feed
from .dhw_cycles import battery_counter, source_roles
from .summaries import day_bounds

logger = logging.getLogger(__name__)

CACHE_TTL_S = 300
CACHE: dict = {}                       # współdzielony cache „dziś”: klucz = data lokalna -> (czas, wynik)
ERROR_NO_HISTORY = "brak historii z HA"


def local_date(iso: str, tz) -> date:
    return datetime.fromisoformat(iso).astimezone(tz).date()


def _merge_sources(acc: dict[str, dict], src: dict | None) -> None:
    """Dosumowuje `sources` (origin -> kwh/pln/battery_kwh) do akumulatora."""
    for origin, x in (src or {}).items():
        a = acc.setdefault(origin, {"kwh": 0.0, "pln": 0.0, "battery_kwh": 0.0})
        for f in ("kwh", "pln", "battery_kwh"):
            a[f] += x.get(f) or 0.0


def aggregate(records: list[dict], kwh_key: str) -> dict:
    """Suma rekordów jednego składnika. Pola `_pv_kwh`/`_known_kwh` (tylko rekordy `split_complete`) służą
    `combine`; `public` je usuwa."""
    kwh = pln = pv_all = known = 0.0
    zones: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    approx = False
    for r in records:
        k, cost, zn = r.get(kwh_key), r.get("cost_pln"), r.get("zones")
        _merge_sources(sources, r.get("sources"))
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
    return {"kwh": kwh, "pln": pln, "zones": zones, "sources": sources, "runs": len(records), "approx": approx,
            "_pv_kwh": pv_all, "_known_kwh": known}


def combine(parts: list[dict]) -> dict:
    """`total` z wyników `aggregate`: sumy, zones scalone po strefie, `approx` = OR."""
    zones: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    for p in parts:
        _merge_sources(sources, p.get("sources"))
        for name, z in p["zones"].items():
            acc = zones.setdefault(name, {"kwh": 0.0, "pln": 0.0, "pv_kwh": 0.0})
            for f in ("kwh", "pln", "pv_kwh"):
                acc[f] += z[f]
    return {"kwh": sum(p["kwh"] for p in parts), "pln": sum(p["pln"] for p in parts), "zones": zones,
            "sources": sources, "runs": sum(p["runs"] for p in parts), "approx": any(p["approx"] for p in parts),
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
    out["sources"] = {n: {"kwh": round(x["kwh"], 3), "pln": round(x["pln"], 2),
                          "battery_kwh": round(x["battery_kwh"], 3)}
                      for n, x in d.get("sources", {}).items()}
    return out


def _configured(settings: dict) -> dict[str, bool]:
    return {"floor": bool(settings.get("pump_energy_entity")),
            "dhw": bool(settings.get("pump_energy_entity") or settings.get("ah_energy_entity")),
            "ac": bool(settings.get("attic_power_entity"))}


def _result(day: date, parts: dict[str, dict], configured: dict[str, bool], now: datetime,
            in_progress: bool = False, error: str | None = None, standby: dict | None = None) -> dict:
    out: dict = {"day": day.isoformat()}
    for name in ("floor", "dhw", "ac"):
        out[name] = {**public(parts[name]), "configured": configured[name]}
    extra = [standby] if standby is not None else []
    out["total"] = public(combine([parts["floor"], parts["dhw"], parts["ac"], *extra]))
    # Pompa = podłoga + CWU (+ postój, gdy liczony; bez klimatyzacji) — źródło kafelki „Pompa dziś” na pulpicie.
    out["pump"] = {**public(combine([parts["floor"], parts["dhw"], *extra])),
                   "configured": configured["floor"] or configured["dhw"]}
    if standby is not None:
        out["standby"] = {**public(standby), "configured": True}
    out["in_progress"] = in_progress
    out["generated_at"] = now.isoformat()
    if error:
        out["error"] = error
    return out


def _tz(settings: dict) -> tuple[str, ZoneInfo]:
    name = settings.get("timezone") or "Europe/Warsaw"
    return name, ZoneInfo(name)


def _roles(settings: dict, states: list[dict], configured: dict[str, bool]):
    """Role encji dla `extract_*` (podłoga, CWU, AC), role źródeł energii i lista wszystkich encji do historii."""
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
    entities = sorted({e for roles in (roles_floor, roles_dhw, roles_ac, sources) for e in roles.values() if e})
    return roles_floor, roles_dhw, roles_ac, sources, entities


def build_yesterday(conn, settings: dict, now: datetime) -> dict:
    """Wczoraj z zapisanych rekordów (bez cache — tanie, z SQLite)."""
    _, tz = _tz(settings)
    day = now.astimezone(tz).date() - timedelta(days=1)

    def pick(records):
        return [r for r in records if local_date(r["start"], tz) == day]

    parts = {"floor": aggregate(pick(heating_runs.load_runs(conn, 3)), "energy_kwh"),
             "dhw": aggregate(pick(dhw_cycles.load_cycles(conn, 3)), "kwh_total"),
             "ac": aggregate(pick(attic_runs.load_runs(conn, 3)), "energy_kwh")}
    error = None
    if not any(p["runs"] for p in parts.values()) and any(_configured(settings).values()):
        error = "Brak zapisanych runów tej doby (zapis nocą o 00:30)"
    return _result(day, parts, _configured(settings), now, error=error)


def build_today(conn, settings: dict, states: list[dict], now: datetime,
                get_history=ha_client.get_history,
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

    roles_floor, roles_dhw, roles_ac, sources, entities = _roles(settings, states, configured)
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
    mixes = dhw_cycles.mix_at(conn, tz, fetch_start, now)
    feed = tariff_feed.for_settings(conn, settings)
    kw = dict(offpeak_price=feed.offpeak_price(day_start.astimezone(tz).replace(tzinfo=None)),
              battery_cost_at=lambda t: battery.at(t.astimezone(tz).replace(tzinfo=None)),
              mix_at=lambda t: mixes(t.astimezone(tz).replace(tzinfo=None)))

    def mine(records):
        return [r for r in records if local_date(r["start"], tz) == today]

    floor = mine(heating_runs.extract_runs(series(roles_floor), fetch_start, now, tz, feed, **kw)) \
        if configured["floor"] else []
    dhw = mine(dhw_cycles.extract_cycles(series(roles_dhw), fetch_start, now, tz, feed,
                                         bath_entries=None, **kw)) if configured["dhw"] else []
    ac = mine(attic_runs.extract_runs(series(roles_ac), fetch_start, now, tz, feed, **kw)) \
        if configured["ac"] else []
    parts = {"floor": aggregate(floor, "energy_kwh"), "dhw": aggregate(dhw, "kwh_total"),
             "ac": aggregate(ac, "energy_kwh")}
    in_progress = any(r.get("complete") is False for r in floor + dhw + ac)
    result = _result(today, parts, configured, now, in_progress=in_progress)
    if cache is not None:
        cache.clear()
        cache[key] = (now, result)
    return result



# ── Doba od północy do północy (0.23.0): dziś i wczoraj z historii HA, runy przycięte o 00:00, „postój” ──

DAY_CACHE: dict = {}                   # klucz = data -> (czas, wynik); osobny od `CACHE` (ten wymaga kształtu dla costs)
TODAY_TTL_S = CACHE_TTL_S
PAST_TTL_S = 3600
FALLBACK_NOTE = "Historia z HA niedostępna — wartości z zapisanych runów (przybliżone)"


def _settle(records: list[dict], live_from: datetime | None) -> list[dict]:
    """Cięcie o północy nie jest „w toku”: `complete: False` zostaje tylko dla runu sięgającego `live_from`
    (chwila „teraz” w bieżącej dobie); w dobie zamkniętej `live_from` = None, więc wszystko jest kompletne."""
    out = []
    for r in records:
        live = live_from is not None and datetime.fromisoformat(r["end"]) >= live_from
        out.append({**r, "complete": not live})
    return out


def _standby(pump: dict | None, parts: dict[str, dict]) -> dict:
    """Postój = licznik pompy − podłoga − CWU, składnik po składniku, nie mniej niż 0. Bez licznika (None) albo
    z resetem licznika: zero i `approx`."""
    zero = aggregate([], "kwh_total")
    if pump is None or pump["kwh"] is None:
        zero["approx"] = True
        return zero
    runs = combine([parts["floor"], parts["dhw"]])
    out = aggregate([], "kwh_total")
    out["kwh"] = max(0.0, pump["kwh"] - runs["kwh"])
    out["pln"] = max(0.0, pump["pln"] - runs["pln"])
    for name, z in pump["zones"].items():
        r = runs["zones"].get(name, {})
        out["zones"][name] = {f: max(0.0, z[f] - (r.get(f) or 0.0)) for f in ("kwh", "pln", "pv_kwh")}
    for origin, x in pump["sources"].items():
        r = runs["sources"].get(origin, {})
        out["sources"][origin] = {f: max(0.0, (x.get(f) or 0.0) - (r.get(f) or 0.0))
                                  for f in ("kwh", "pln", "battery_kwh")}
    if pump["split_complete"]:
        out["_pv_kwh"] = max(0.0, pump["pv_kwh"] - runs["_pv_kwh"])
        out["_known_kwh"] = out["kwh"]
    out["approx"] = not pump["split_complete"] or runs["approx"]
    return out


def build_day(conn, settings: dict, states: list[dict], day: date, now: datetime,
              get_history=ha_client.get_history, cache: dict | None = None) -> dict:
    """Doba lokalna `day` (dziś albo wczoraj) z historii HA: runy przycięte do [00:00, 24:00) (run przez północ
    dzieli się między doby), licznik pompy na tym samym przedziale, a różnica do podłogi i CWU to `standby`
    (postój). Wczoraj bez historii → zapisane rekordy z `approx` i notatką. Dziś bez historii → zera z błędem."""
    tz_name, tz = _tz(settings)
    now_l = now.astimezone(tz)
    is_today = day == now_l.date()
    key = day.isoformat()
    ttl = TODAY_TTL_S if is_today else PAST_TTL_S
    if cache is not None:
        hit = cache.get(key)
        if hit and 0 <= (now - hit[0]).total_seconds() < ttl:
            return hit[1]
    configured = _configured(settings)
    empty = {n: aggregate([], "energy_kwh") for n in ("floor", "dhw", "ac")}
    if not any(configured.values()):
        return _result(day, empty, configured, now)

    roles_floor, roles_dhw, roles_ac, sources, entities = _roles(settings, states, configured)
    day_start, day_end = day_bounds(day, tz_name)
    window_end = min(day_end, now)
    fetch_start = day_start - timedelta(hours=3)
    try:
        hist = get_history(entities, fetch_start.isoformat(), window_end.isoformat())
    except Exception:
        logger.warning("day_summary: historia z HA niedostępna", exc_info=True)
        hist = None
    if not hist:
        if not is_today:
            data = build_yesterday(conn, settings, now)
            for p in ("floor", "dhw", "ac", "total", "pump"):
                if data[p].get("kwh"):
                    data[p]["approx"] = True
            data["error"] = data.get("error") or FALLBACK_NOTE
            return data
        zero = {n: aggregate([], "energy_kwh") for n in empty}
        for n in zero:
            zero[n]["approx"] = configured[n]
        return _result(day, zero, configured, now, error=ERROR_NO_HISTORY)

    def series(roles):
        s = {role: hist.get(eid, []) for role, eid in roles.items() if eid}
        s.update({role: hist.get(eid, []) for role, eid in sources.items() if eid})
        return s

    battery = battery_counter(conn, tz, fetch_start, window_end)
    mixes = dhw_cycles.mix_at(conn, tz, fetch_start, window_end)
    feed = tariff_feed.for_settings(conn, settings)
    kw = dict(offpeak_price=feed.offpeak_price(day_start.astimezone(tz).replace(tzinfo=None)),
              battery_cost_at=lambda t: battery.at(t.astimezone(tz).replace(tzinfo=None)),
              mix_at=lambda t: mixes(t.astimezone(tz).replace(tzinfo=None)))
    live_from = window_end - timedelta(minutes=heating_runs.MERGE_GAP_MIN) if is_today else None

    floor = _settle(heating_runs.extract_runs(series(roles_floor), day_start, window_end, tz, feed, **kw), live_from) \
        if configured["floor"] else []
    dhw = _settle(dhw_cycles.extract_cycles(series(roles_dhw), day_start, window_end, tz, feed,
                                            bath_entries=None, **kw), live_from) if configured["dhw"] else []
    ac = _settle(attic_runs.extract_runs(series(roles_ac), day_start, window_end, tz, feed, **kw), live_from) \
        if configured["ac"] else []
    parts = {"floor": aggregate(floor, "energy_kwh"), "dhw": aggregate(dhw, "kwh_total"),
             "ac": aggregate(ac, "energy_kwh")}

    standby = None
    pump_entity = settings.get("pump_energy_entity")
    if pump_entity:
        e_times, e_values = dhw_cycles._points(hist.get(pump_entity, []))
        cost = dhw_cycles._cost(e_times, e_values, day_start, window_end, tz, feed, kw["offpeak_price"],
                                battery_cost_at=kw["battery_cost_at"], mix_at=kw["mix_at"],
                                **dhw_cycles.source_counters(series({}))) if e_times else None
        standby = _standby(None if cost is None else {
            "kwh": cost["kwh"], "pln": cost["pln"], "pv_kwh": cost["pv_kwh"], "zones": cost["zones"],
            "sources": cost["sources"], "split_complete": cost["split_complete"]}, parts)
    in_progress = any(r.get("complete") is False for r in floor + dhw + ac)
    result = _result(day, parts, configured, now, in_progress=in_progress, standby=standby)
    if cache is not None:
        cache[key] = (now, result)
    return result
