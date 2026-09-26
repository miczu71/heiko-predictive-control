"""Analiza opisowa danych pompy i domu (D1) — funkcje czyste na statystykach LTS i streszczeniach dobowych.

Nic tu nie zapisuje do pompy. `compute_report` (jedyne I/O: statystyki HA przez WebSocket) składa raport,
z którego na checkpoincie D1 wybieramy analizatory do D2. Raport zawiera też `data_gaps` (czego brakuje)
i `ideas` (hipotezy propozycji — jeszcze nie propozycje, tylko materiał do decyzji)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from . import catalog, ha_client, summaries, telemetry
from .comfort import min_room_entities
from .floor_learn import HEATING_MAX_OUTDOOR_C, HEATING_MONTHS, season_window
from .kpi import baseline_from_stats, outdoor_class
from .tariff import PEAK_WINDOWS

logger = logging.getLogger(__name__)

HEIKO_SINCE = datetime(2026, 4, 12, tzinfo=timezone.utc)      # integracja `heiko_heatpump` działa od tej daty
DEFAULT_TZ = "Europe/Warsaw"


def _entities(raw) -> list[str]:
    return [e.strip() for e in str(raw or "").split(",") if e.strip()]


def _by_start(rows, field) -> dict[int, float]:
    return {r["start"]: r[field] for r in rows or [] if r.get(field) is not None}


def _local(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)


def percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _r(v, digits=2):
    return None if v is None else round(v, digits)


def comfort(stats: dict, zones: list[str], outdoor: str, s: dict, tz: ZoneInfo) -> dict | None:
    """Rozkład temperatur pokoi w sezonie grzewczym (godziny z temp. zewn. poniżej progu grzania)
    i czas w paśmie komfortu (pasmo dzień/noc jak w ustawieniach)."""
    target = float(s.get("heiko_room_target_c", 20.6))
    band_day, band_night = float(s.get("heiko_comfort_band_day_c", 1.0)), float(s.get("heiko_comfort_band_night_c", 1.5))
    d0, d1 = int(s.get("heiko_day_start_hour", 6)), int(s.get("heiko_day_end_hour", 22))
    room_min = float(s.get("heiko_room_min_c", 18.5))
    selected = set(min_room_entities(s, zones))          # minimum liczy się tylko z pokoi wybranych w Opcjach
    temps = _by_start(stats.get(outdoor, []), "mean")
    rooms = {z: _by_start(stats.get(z, []), "mean") for z in zones}
    per: dict[str, list[tuple[float, bool]]] = {z: [] for z in zones}     # (temp, w paśmie)
    coldest, cold_hours, averages = [], 0, []
    for start, to in temps.items():
        local = _local(start, tz)
        if local.month not in HEATING_MONTHS or to >= HEATING_MAX_OUTDOOR_C:
            continue
        band = band_day if d0 <= local.hour < d1 else band_night
        vals = []
        for z in zones:
            t = rooms[z].get(start)
            if t is None:
                continue
            per[z].append((t, abs(t - target) <= band))
            vals.append(t)
        if len(vals) == len(zones):
            cold = min(rooms[z][start] for z in zones if z in selected)
            coldest.append(cold)
            cold_hours += cold < room_min
            avg = sum(vals) / len(vals)
            averages.append((avg, abs(avg - target) <= band))
    if not coldest:
        return None
    out_rooms = {}
    for z, rows in per.items():
        if not rows:
            continue
        t = sorted(v for v, _ in rows)
        mean = sum(t) / len(t)
        out_rooms[z] = {"hours": len(t), "mean": _r(mean), "offset": _r(mean - target), "sd": _r((sum((x - mean) ** 2 for x in t) / len(t)) ** 0.5),
                        "p5": _r(percentile(t, 0.05)), "p50": _r(percentile(t, 0.5)), "p95": _r(percentile(t, 0.95)),
                        "min": _r(t[0]), "in_band": _r(sum(1 for _, ok in rows if ok) / len(rows), 3)}
    cs = sorted(coldest)
    av = [a for a, _ in averages]
    av_mean = sum(av) / len(av)
    return {"target": target, "band_day": band_day, "band_night": band_night, "room_min": room_min,
            "hours": len(coldest), "rooms": out_rooms, "min_rooms": [z for z in zones if z in selected],
            # średnia stref = wielkość, którą reguluje add-on (cel liczony na średniej, nie na pojedynczym pokoju)
            "average": {"mean": _r(av_mean), "offset": _r(av_mean - target),
                        "sd": _r((sum((x - av_mean) ** 2 for x in av) / len(av)) ** 0.5),
                        "in_band": _r(sum(1 for _, ok in averages if ok) / len(averages), 3)},
            "coldest": {"p5": _r(percentile(cs, 0.05)), "p50": _r(percentile(cs, 0.5)), "min": _r(cs[0]),
                        "below_room_min_h": int(cold_hours)}}


def energy(stats: dict, outdoor: str, energy_entity: str, tz: ZoneInfo) -> dict | None:
    """Energia pompy w sezonie: miesięcznie (kWh, stopniodni, kWh/stopniodzień), udział szczytu ogółem i wg klas temp."""
    temps = _by_start(stats.get(outdoor, []), "mean")
    kwh = _by_start(stats.get(energy_entity, []), "change")
    months: dict[str, list[float]] = {}                      # 'RRRR-MM' -> [kWh, stopniogodziny, godziny]
    for start, to in temps.items():
        e = kwh.get(start)
        if e is None or e < 0:
            continue
        local = _local(start, tz)
        if local.month not in HEATING_MONTHS:
            continue
        m = months.setdefault(local.strftime("%Y-%m"), [0.0, 0.0, 0])
        m[0] += e
        m[1] += max(0.0, HEATING_MAX_OUTDOOR_C - to)
        m[2] += 1
    if not months:
        return None
    base = baseline_from_stats(stats, outdoor, energy_entity, tz.key)
    return {"months": {k: {"kwh": _r(v[0], 1), "hdd": _r(v[1] / 24, 1), "hours": v[2],
                           "kwh_per_hdd": _r(v[0] / (v[1] / 24), 2) if v[1] > 0 else None}
                       for k, v in sorted(months.items())},
            "peak_share": base and {"overall": base["overall"], "kwh": base["kwh"], "hours": base["hours"],
                                    "by_class": base["by_class"]}}


def pump_hours(daily: dict, outdoor_hourly: list[dict], tz: ZoneInfo) -> dict | None:
    """Zimowy podział czasu pompy z dobowych liczników godzin trybu (`pompa_heating/hot_water/off`,
    zerowane o północy — dobowe maksimum = godziny w dobie). Klasy wg średniej dobowej temp. zewn."""
    def per_day(rows):
        return {_local(r["start"], tz).date(): r["max"] for r in rows or [] if r.get("max") is not None}
    heat, dhw, off = (per_day(daily.get(k)) for k in ("heating", "hot_water", "off"))
    by_day: dict[date, list[float]] = {}
    for r in outdoor_hourly:
        if r.get("mean") is not None:
            by_day.setdefault(_local(r["start"], tz).date(), []).append(r["mean"])
    classes: dict[int, list[float]] = {}                      # klasa -> [dni, grzanie h, CWU h, postój h]
    months: dict[str, list[float]] = {}
    for day, h in heat.items():
        temps = by_day.get(day)
        if not temps or len(temps) < 20 or day not in dhw or day not in off or not (0 <= h + dhw[day] + off[day] <= 26):
            continue
        cls = classes.setdefault(outdoor_class(sum(temps) / len(temps)), [0, 0.0, 0.0, 0.0])
        mon = months.setdefault(day.strftime("%Y-%m"), [0, 0.0, 0.0, 0.0])
        for acc in (cls, mon):
            acc[0] += 1
            acc[1] += h
            acc[2] += dhw[day]
            acc[3] += off[day]
    if not classes:
        return None

    def fmt(acc):
        n = acc[0]
        return {"days": n, "heating_h": _r(acc[1] / n, 1), "dhw_h": _r(acc[2] / n, 1), "standby_h": _r(acc[3] / n, 1)}
    return {"by_class": {str(c): fmt(v) for c, v in sorted(classes.items())},
            "by_month": {k: fmt(v) for k, v in sorted(months.items())}}


def cop_by_class(stats: dict, cop: str, outdoor: str) -> dict | None:
    """COP (estymowany przez integrację, tylko tryb grzania) wg klas temp. zewn., od uruchomienia integracji."""
    temps = _by_start(stats.get(outdoor, []), "mean")
    classes: dict[int, list[float]] = {}
    for start, c in _by_start(stats.get(cop, []), "mean").items():
        to = temps.get(start)
        if to is None or c <= 0 or c > 12:
            continue
        classes.setdefault(outdoor_class(to), []).append(c)
    if not classes:
        return None
    return {str(k): {"hours": len(v), "mean": _r(sum(v) / len(v)), "min": _r(min(v)), "max": _r(max(v))}
            for k, v in sorted(classes.items())}


def own_data(sums: dict[str, dict[str, dict]], changes: dict) -> dict:
    """Dane zebrane przez add-on: dobowe streszczenia (sprężarka, CWU, grzałka, P0) i licznik zmian parametrów."""
    def avg(topic, field):
        vals = [d[topic][field] for d in sums.values() if topic in d and d[topic].get(field) is not None]
        return _r(sum(vals) / len(vals), 1) if vals else None
    return {"days": len(sums), "changes": changes,
            "avg_per_day": {"compressor_starts": avg("compressor", "starts"),
                            "compressor_run_min": avg("compressor", "run_min"),
                            "short_cycles": avg("compressor", "short_cycles"),
                            "mean_run_min": avg("compressor", "mean_run_min"),
                            "dhw_cycles": avg("modes", "dhw_cycles"), "dhw_min": avg("modes", "dhw_min"),
                            "heating_min": avg("modes", "heating_min"),
                            "ah_min": avg("backup", "ah_min"), "hbh_min": avg("backup", "hbh_min"),
                            "hwtbh_min": avg("backup", "hwtbh_min"), "p0_pulses": avg("p0", "pulses"),
                            "kwh": avg("energy", "kwh"), "peak_share": avg("energy", "peak_share")}}


def ideas(report: dict) -> list[str]:
    """Hipotezy do sprawdzenia w D2 — reguły z liczb raportu, bez udawania pewności."""
    out: list[str] = []
    c = report.get("comfort")
    if c:
        avg = c.get("average") or {}
        if avg.get("in_band") is not None and avg["in_band"] < 0.85:
            way = "za ciepło" if avg["offset"] > 0 else "za zimno"
            out.append(f"Komfort: średnia stref jest w paśmie {avg['in_band'] * 100:.0f}% czasu (średnio {avg['offset']:+.1f}°C od celu — {way}).")
        warm = sorted(((v["offset"], k) for k, v in c["rooms"].items() if v.get("offset") is not None and v["offset"] > 1.0),
                      reverse=True)
        if warm:
            names = ", ".join(f"{k.split('.')[-1]} ({o:+.1f}°C)" for o, k in warm[:3])
            out.append(f"Oszczędność: pokoje średnio powyżej celu: {names} — obniżenie przesunięcia krzywej może dać oszczędność bez utraty komfortu w tych pokojach (do sprawdzenia eksperymentem, ±1).")
        below = c["coldest"]["below_room_min_h"]
        if below:
            out.append(f"Komfort: najzimniejszy pokój schodził poniżej {c['room_min']}°C przez {below} godz. z {c['hours']} — ustal, który pokój i czemu (okno? drzwi?), zanim propozycje ruszą krzywą w dół.")
    e = report.get("energy") or {}
    ps = (e.get("peak_share") or {}).get("overall")
    if ps is not None and ps > 0.35:
        out.append(f"Koszt: {ps * 100:.0f}% energii pompy zimą szło w szczycie G12w — przesunięcie grzania ma potencjał, ale zysk zależy od bezwładności (niezmierzonej).")
    p = report.get("pump_hours") or {}
    offpeak_h = 24 - sum(b - a for a, b in PEAK_WINDOWS)
    heavy = [(int(k), v) for k, v in (p.get("by_class") or {}).items() if v["days"] >= 3 and v["heating_h"] is not None
             and v["heating_h"] > offpeak_h]
    if heavy:
        cold = max(k for k, _ in heavy)
        out.append(f"Sufit przesuwania: przy temp. zewn. ≤ {cold}°C grzanie zajmuje {min(v['heating_h'] for _, v in heavy):.0f}–"
                   f"{max(v['heating_h'] for _, v in heavy):.0f} h/dobę, a poza szczytem G12w jest tylko {offpeak_h} h w dzień roboczy — "
                   "w największe mrozy nie da się grzać wyłącznie poza szczytem (zysk z przesuwania maleje).")
    own = report.get("own") or {}
    a = own.get("avg_per_day") or {}
    if a.get("short_cycles") is not None and a["short_cycles"] >= 3:
        out.append(f"Histereza: {a['short_cycles']:.0f} krótkich cykli sprężarki na dobę — kandydat na analizator histerezy (stop/restart).")
    if a.get("dhw_cycles") is not None and a["dhw_cycles"] >= 3:
        out.append(f"CWU: {a['dhw_cycles']:.0f} cykli CWU na dobę — sprawdź histerezę CWU i porę względem taryfy.")
    heaters = {k: a.get(f"{k}_min") for k in ("ah", "hbh", "hwtbh")}
    if any(heaters.values()):
        txt = ", ".join(f"{k.upper()} {v:g} min/dobę" for k, v in heaters.items() if v)
        out.append(f"Grzałki (liczniki czasu pracy, średnio na dobę): {txt} — sprawdź, czy to anti-legionella, CWU 58°C, czy mróz.")
    return out


def data_gaps(report: dict, now: datetime) -> list[str]:
    gaps = [f"Czujniki Heiko (COP, sprężarka, HBH, P0) mają dane dopiero od {HEIKO_SINCE.date().isoformat()} — bez pełnej zimy.",
            "Bezwładność domu nie jest zidentyfikowana (jedna zima, prawie jedna nastawa) — skutków zmian nastaw nie da się jeszcze przewidzieć."]
    cop = report.get("cop")
    if not cop:
        gaps.append("Brak COP w LTS dla trybu grzania (latem pompa robi głównie CWU).")
    elif any(v["mean"] > 7 for v in cop.values()):
        gaps.append("COP estymowany przez integrację ma klasy o średniej > 7 (nierealne dla pompy powietrznej: szacunek z nominalnego "
                    "przepływu) — używać wyłącznie jako trendu; wiarygodny COP wymaga miernika ciepła albo bilansu energii.")
    own_days = (report.get("own") or {}).get("days", 0)
    if own_days < 14:
        gaps.append(f"Własne streszczenia dobowe add-onu: {own_days} dni — statystyki cykli będą wiarygodne po ~2 tygodniach.")
    if not report.get("energy"):
        gaps.append("Brak statystyk energii pompy / temperatury zewnętrznej w oknie sezonu.")
    return gaps


def compute_report(conn, settings: dict, now: datetime, get_statistics=ha_client.get_statistics,
                   get_states=ha_client.get_all_states) -> dict:
    """Składa raport. Statystyki LTS pobierane po jednej encji (WebSocket) — kilka minut; wywoływać w tle."""
    tz = ZoneInfo(settings.get("timezone") or DEFAULT_TZ)
    zones = _entities(settings.get("day_zone_temp_entities"))
    outdoor = settings.get("heiko_bootstrap_outdoor_entity") or settings.get("outdoor_temp_entity", "")
    energy_entity = settings.get("pump_energy_entity", "")
    start, end = season_window(now)
    report: dict = {"created": now.isoformat(timespec="minutes"),
                    "window": {"start": start.date().isoformat(), "end": end.date().isoformat()}}

    season = get_statistics(list({*zones, outdoor, energy_entity} - {""}), start.isoformat(), end.isoformat())
    if season:
        report["comfort"] = comfort(season, zones, outdoor, settings, tz)
        report["energy"] = energy(season, outdoor, energy_entity, tz)
    daily = {}
    for key, eid in (("heating", "sensor.pompa_heating"), ("hot_water", "sensor.pompa_hot_water"),
                     ("off", "sensor.pompa_off")):
        got = get_statistics([eid], start.isoformat(), end.isoformat(), period="day", types=("max",))
        daily[key] = (got or {}).get(eid)
    if season and any(daily.values()):
        report["pump_hours"] = pump_hours(daily, season.get(outdoor, []), tz)
    cop_id = catalog.resolve_telemetry(get_states() or []).get("cop_estimated")
    if cop_id and outdoor:
        got = get_statistics([cop_id, outdoor], HEIKO_SINCE.isoformat(), now.astimezone(timezone.utc).isoformat())
        if got:
            report["cop"] = cop_by_class(got, cop_id, outdoor)
    report["own"] = own_data(summaries.load_summaries(conn), telemetry.changes_summary(conn, now))
    report["ideas"] = ideas(report)
    report["data_gaps"] = data_gaps(report, now)
    return report


def save_report(conn, report: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO report_cache (name, created, data) VALUES ('d1', ?, ?)",
                 (report["created"], json.dumps(report)))
    conn.commit()


def load_report(conn) -> dict | None:
    row = conn.execute("SELECT data FROM report_cache WHERE name = 'd1'").fetchone()
    return json.loads(row["data"]) if row else None
