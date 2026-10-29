"""KPI zmierzone (nie modelowane): udział energii pompy zużytej w szczycie G12w.

To jest właściwa miara przesunięcia grzania — nie zależy od modelu domu. Porównujemy
ostatnie dni z zimą sprzed sterowania (statystyki LTS), **przy tym samym rozkładzie
temperatur zewnętrznych** (klasy co 3°C), żeby chłodniejsza pogoda nie udawała zmiany
zachowania pompy. Energia obejmuje CWU po obu stronach porównania (LTS nie rozróżnia).
Od 0.15.0 do szczytu liczy się tylko energia z sieci (`energy_source`): pompa na PV albo
baterii w godzinach szczytu nie płaci ceny szczytowej. Baza z LTS — ten sam podział, godzinowo.

Funkcje czyste + `ensure_peak_baseline` (jedyne I/O: statystyki HA)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db as dbm
from . import ha_client
from . import energy_source as es
from .floor_learn import HEATING_MAX_OUTDOOR_C, HEATING_MONTHS, season_window
from .tariff import default_is_workday, fallback_hour

logger = logging.getLogger(__name__)

CLASS_STEP_C = 3
MIN_WINDOW_KWH = 5.0          # poniżej: lato / pompa prawie nie grzeje — KPI nie ma sensu
MIN_CLASS_HOURS = 24          # klasa temperatury w bazie z mniejszą liczbą godzin -> udział ogólny
DEFAULT_TZ = "Europe/Warsaw"
BASELINE_VERSION = 2          # 2 = szczyt tylko z poboru z sieci; starsza baza (zegarowa) liczona od nowa


def outdoor_class(temp_c: float) -> int:
    return int(round(temp_c / CLASS_STEP_C)) * CLASS_STEP_C


def baseline_from_stats(stats: dict[str, list[dict]], outdoor: str, energy: str,
                        tz_name: str = DEFAULT_TZ, grid: str | None = None) -> dict | None:
    """Udział energii w szczycie w zimie, ogółem i per klasa temp. zewn. Szczyt = dzień
    roboczy (pn–pt, święta pomijamy) w oknach G12w, wg czasu lokalnego. Z `grid` (licznik
    poboru z sieci) w szczycie liczy się tylko min(energia pompy, pobór) w tej godzinie."""
    tz = ZoneInfo(tz_name)
    temps = {r["start"]: r["mean"] for r in stats.get(outdoor, []) if r.get("mean") is not None}
    kwh = {r["start"]: r["change"] for r in stats.get(energy, []) if r.get("change") is not None}
    imported = {r["start"]: r["change"] for r in stats.get(grid, []) if r.get("change") is not None} if grid else {}
    total = peak = 0.0
    hours = 0
    by_class: dict[int, list[float]] = {}                 # klasa -> [kwh, peak_kwh, godziny]
    for start, to in temps.items():
        e = kwh.get(start)
        if e is None or e < 0:
            continue
        utc = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
        if utc.month not in HEATING_MONTHS or to >= HEATING_MAX_OUTDOOR_C:
            continue
        local = utc.astimezone(tz)
        # Baza historyczna zawsze wg G12w (zima sprzed feedu z pv_roi) — porównanie ma stałą miarę.
        is_peak = fallback_hour(local, default_is_workday(local.date())).is_peak
        from_grid = es.split(e, imported[start])[0] if start in imported else e
        cls = by_class.setdefault(outdoor_class(to), [0.0, 0.0, 0])
        cls[0] += e
        cls[1] += from_grid if is_peak else 0.0
        cls[2] += 1
        total += e
        peak += from_grid if is_peak else 0.0
        hours += 1
    if hours == 0 or total <= 0:
        return None
    return {
        "overall": round(peak / total, 4), "kwh": round(total, 1), "hours": hours,
        "by_class": {str(c): {"share": round(v[1] / v[0], 4) if v[0] > 0 else None,
                              "kwh": round(v[0], 1), "hours": v[2]} for c, v in sorted(by_class.items())},
    }


def peak_share_kpi(rows, baseline: dict | None, min_kwh: float = MIN_WINDOW_KWH) -> dict | None:
    """`rows`: wiersze cykli z `energy_kwh`, `tariff_peak`, `outdoor_temp_c` i opcjonalnie `grid_kwh`
    (brak = cykl sprzed podziału na źródła, cała energia jak z sieci).
    Zwraca udział szczytu w oknie i oczekiwany udział z zimy przy tym samym rozkładzie temperatur."""
    total = peak = 0.0
    per_class: dict[int, float] = {}
    for r in rows:
        e, is_peak, to = r["energy_kwh"], r["tariff_peak"], r["outdoor_temp_c"]
        if e is None or e < 0 or is_peak is None:
            continue
        grid = r["grid_kwh"] if "grid_kwh" in r.keys() and r["grid_kwh"] is not None else e
        total += e
        peak += grid if is_peak else 0.0
        if to is not None:
            per_class[outdoor_class(to)] = per_class.get(outdoor_class(to), 0.0) + e
    if total < min_kwh:
        return None
    share = peak / total
    expected = None
    if baseline:
        weights = classified = 0.0
        for cls, e in per_class.items():
            b = (baseline.get("by_class") or {}).get(str(cls)) or {}
            use = b["share"] if b.get("share") is not None and b.get("hours", 0) >= MIN_CLASS_HOURS \
                else baseline["overall"]
            weights += e * use
            classified += e
        rest = total - classified                          # energia bez znanej temp. zewn.
        expected = (weights + rest * baseline["overall"]) / total
    return {
        "share": round(share, 4), "kwh": round(total, 2),
        "baseline_share": None if expected is None else round(expected, 4),
        "delta_pp": None if expected is None else round((share - expected) * 100, 1),
    }


def hourly_rows(rows) -> list[dict]:
    """Cykle zsumowane do godzin, żeby udział z sieci liczyć tak jak baza z godzinowych LTS:
    min(energia pompy w godzinie, pobór z sieci w godzinie). Godzina z cyklem bez poboru
    (sprzed 0.15.0 albo bez liczników) — suma `grid_kwh` per cykl, a bez niego cała energia."""
    hours: dict[tuple, list] = {}
    for r in rows:
        if r["tariff_peak"] is not None:
            hours.setdefault((r["ts"][:13], r["tariff_peak"]), []).append(r)
    out = []
    for members in hours.values():
        e = sum(m["energy_kwh"] for m in members)
        imports = [m["grid_import_kwh"] for m in members]
        grid = es.split(e, sum(imports))[0] if None not in imports else \
            sum(m["energy_kwh"] if m["grid_kwh"] is None else m["grid_kwh"] for m in members)
        temps = [m["outdoor_temp_c"] for m in members if m["outdoor_temp_c"] is not None]
        out.append({"energy_kwh": e, "tariff_peak": members[0]["tariff_peak"], "grid_kwh": grid,
                    "outdoor_temp_c": sum(temps) / len(temps) if temps else None})
    return out


def recent_kpi(conn, now: datetime, baseline: dict | None, days: int = 7) -> dict | None:
    cutoff = (now.replace(microsecond=0) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT ts, energy_kwh, tariff_peak, outdoor_temp_c, grid_kwh, grid_import_kwh FROM cycles "
        "WHERE loop = 'heiko' AND ts >= ? AND energy_kwh IS NOT NULL", (cutoff,)).fetchall()
    return peak_share_kpi(hourly_rows(rows), baseline)


def ensure_peak_baseline(conn, settings: dict, now: datetime,
                         get_statistics=ha_client.get_statistics) -> dict | None:
    """Raz (do skutku) policz bazę z LTS ostatniego pełnego sezonu i zapisz w `peak_baseline`."""
    existing = dbm.get_setting(conn, "peak_baseline")
    if existing and existing.get("v") == BASELINE_VERSION:
        return existing
    if existing:                                       # baza zegarowa nie pasuje do szczytu z sieci — bez porównania,
        dbm.set_setting(conn, "peak_baseline", None)   # dopóki nowa się nie policzy

    outdoor = settings.get("heiko_bootstrap_outdoor_entity") or settings.get("outdoor_temp_entity", "")
    energy = settings.get("pump_energy_entity", "")
    grid = es.entities(settings)["grid_import"]
    start, end = season_window(now)
    stats = get_statistics([outdoor, energy, grid], start.isoformat(), end.isoformat())
    if stats is None:
        logger.info("Baza udziału szczytu: statystyki niedostępne — spróbuję po restarcie")
        return None
    baseline = baseline_from_stats(stats, outdoor, energy, settings.get("timezone") or DEFAULT_TZ, grid=grid)
    if baseline is None:
        logger.warning("Baza udziału szczytu: brak danych sezonu w statystykach")
        return None
    baseline["at"], baseline["v"] = now.isoformat(timespec="minutes"), BASELINE_VERSION
    dbm.set_setting(conn, "peak_baseline", baseline)
    logger.info("Baza udziału szczytu (zima): %s", {k: baseline[k] for k in ("overall", "kwh", "hours")})
    return baseline
