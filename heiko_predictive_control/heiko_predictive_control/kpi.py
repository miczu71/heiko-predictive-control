"""KPI zmierzone (nie modelowane): udział energii pompy zużytej w szczycie G12w.

To jest właściwa miara przesunięcia grzania — nie zależy od modelu domu. Porównujemy
ostatnie dni z zimą sprzed sterowania (statystyki LTS), **przy tym samym rozkładzie
temperatur zewnętrznych** (klasy co 3°C), żeby chłodniejsza pogoda nie udawała zmiany
zachowania pompy. Energia obejmuje CWU po obu stronach porównania (LTS nie rozróżnia).

Funkcje czyste + `ensure_peak_baseline` (jedyne I/O: statystyki HA)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db as dbm
from . import ha_client
from .floor_learn import HEATING_MAX_OUTDOOR_C, HEATING_MONTHS, season_window
from .tariff import is_peak_hour

logger = logging.getLogger(__name__)

CLASS_STEP_C = 3
MIN_WINDOW_KWH = 5.0          # poniżej: lato / pompa prawie nie grzeje — KPI nie ma sensu
MIN_CLASS_HOURS = 24          # klasa temperatury w bazie z mniejszą liczbą godzin -> udział ogólny
DEFAULT_TZ = "Europe/Warsaw"


def outdoor_class(temp_c: float) -> int:
    return int(round(temp_c / CLASS_STEP_C)) * CLASS_STEP_C


def baseline_from_stats(stats: dict[str, list[dict]], outdoor: str, energy: str,
                        tz_name: str = DEFAULT_TZ) -> dict | None:
    """Udział energii w szczycie w zimie, ogółem i per klasa temp. zewn. Szczyt = dzień
    roboczy (pn–pt, święta pomijamy) w oknach G12w, wg czasu lokalnego."""
    tz = ZoneInfo(tz_name)
    temps = {r["start"]: r["mean"] for r in stats.get(outdoor, []) if r.get("mean") is not None}
    kwh = {r["start"]: r["change"] for r in stats.get(energy, []) if r.get("change") is not None}
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
        is_peak = is_peak_hour(local.hour, local.weekday() < 5)
        cls = by_class.setdefault(outdoor_class(to), [0.0, 0.0, 0])
        cls[0] += e
        cls[1] += e if is_peak else 0.0
        cls[2] += 1
        total += e
        peak += e if is_peak else 0.0
        hours += 1
    if hours == 0 or total <= 0:
        return None
    return {
        "overall": round(peak / total, 4), "kwh": round(total, 1), "hours": hours,
        "by_class": {str(c): {"share": round(v[1] / v[0], 4) if v[0] > 0 else None,
                              "kwh": round(v[0], 1), "hours": v[2]} for c, v in sorted(by_class.items())},
    }


def peak_share_kpi(rows, baseline: dict | None, min_kwh: float = MIN_WINDOW_KWH) -> dict | None:
    """`rows`: wiersze cykli z `energy_kwh`, `tariff_peak`, `outdoor_temp_c`.
    Zwraca udział szczytu w oknie i oczekiwany udział z zimy przy tym samym rozkładzie temperatur."""
    total = peak = 0.0
    per_class: dict[int, float] = {}
    for r in rows:
        e, is_peak, to = r["energy_kwh"], r["tariff_peak"], r["outdoor_temp_c"]
        if e is None or e < 0 or is_peak is None:
            continue
        total += e
        peak += e if is_peak else 0.0
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


def recent_kpi(conn, now: datetime, baseline: dict | None, days: int = 7) -> dict | None:
    cutoff = (now.replace(microsecond=0) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT energy_kwh, tariff_peak, outdoor_temp_c FROM cycles "
        "WHERE loop = 'heiko' AND ts >= ? AND energy_kwh IS NOT NULL", (cutoff,)).fetchall()
    return peak_share_kpi(rows, baseline)


def ensure_peak_baseline(conn, settings: dict, now: datetime,
                         get_statistics=ha_client.get_statistics) -> dict | None:
    """Raz (do skutku) policz bazę z LTS ostatniego pełnego sezonu i zapisz w `peak_baseline`."""
    existing = dbm.get_setting(conn, "peak_baseline")
    if existing:
        return existing
    outdoor = settings.get("heiko_bootstrap_outdoor_entity") or settings.get("outdoor_temp_entity", "")
    energy = settings.get("pump_energy_entity", "")
    start, end = season_window(now)
    stats = get_statistics([outdoor, energy], start.isoformat(), end.isoformat())
    if stats is None:
        logger.info("Baza udziału szczytu: statystyki niedostępne — spróbuję po restarcie")
        return None
    baseline = baseline_from_stats(stats, outdoor, energy, settings.get("timezone") or DEFAULT_TZ)
    if baseline is None:
        logger.warning("Baza udziału szczytu: brak danych sezonu w statystykach")
        return None
    baseline["at"] = now.isoformat(timespec="minutes")
    dbm.set_setting(conn, "peak_baseline", baseline)
    logger.info("Baza udziału szczytu (zima): %s", {k: baseline[k] for k in ("overall", "kwh", "hours")})
    return baseline
