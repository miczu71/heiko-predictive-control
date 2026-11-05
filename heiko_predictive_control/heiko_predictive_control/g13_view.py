"""Tryb „Szczyt G13” — widok dla pulpitu (E4), tylko odczyt: stan trybu, następna zmiana, energia i koszt
podłogówki w strefie `evening_peak` (dziś i 7 dni, z dziennika runów) oraz zadziałania bezpiecznika."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db as dbm
from . import g13_exec, g13_peak, heating_runs, tariff_feed
from .summaries import day_bounds

PEAK_ZONE = "evening_peak"
WEEK_DAYS = 7
STATUS_LABELS = {"off": "wyłączony", "suspended": "wstrzymany do jutra", "tripped": "bezpiecznik — bazowe do 21:00",
                 "active": "włączony"}


def _zone_energy(conn, settings: dict, first: date, last: date) -> dict:
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    start, _ = day_bounds(first, tz_name)
    _, end = day_bounds(last, tz_name)
    kwh = pln = 0.0
    for run in heating_runs.load_range(conn, start, end):
        zone = (run.get("zones") or {}).get(PEAK_ZONE)
        if zone:
            kwh += zone.get("kwh") or 0.0
            pln += zone.get("pln") or 0.0
    return {"kwh": round(kwh, 2), "pln": round(pln, 2)}


def _next_change(now: datetime, workday: bool | None, cfg: g13_peak.Config) -> dict | None:
    """Najbliższa zmiana dziś (13/16/21) z docelowym przesunięciem względem bazowego; poza dniem roboczym brak."""
    if not workday:
        return None
    steps = ((g13_peak.PREHEAT_FROM_H, f"{cfg.preheat_shift:+g}"), (g13_peak.PEAK_FROM_H, f"{cfg.peak_shift:+g}"),
             (g13_peak.WINDOW_END_H, "powrót do bazowego"))
    for hour, value in steps:
        if now.hour < hour:
            return {"at": f"{hour:02d}:00", "value": value}
    return None


def build(conn, settings: dict, now: datetime, is_workday) -> dict:
    today = now.date()
    st = dbm.get_setting(conn, g13_exec.STATE_KEY) or {}
    enabled = bool(settings.get("g13_peak_enabled", False))
    if not enabled:
        status = "off"
    elif st.get("suspended_day") == today.isoformat():
        status = "suspended"
    elif st.get("tripped") and st.get("day") == today.isoformat():
        status = "tripped"
    else:
        status = "active"
    feed = tariff_feed.for_settings(conn, settings, is_workday)
    workday = g13_peak.workday_from_feed(feed, today, now)
    week_from = (today - timedelta(days=WEEK_DAYS - 1)).isoformat()
    return {
        "enabled": enabled, "status": status, "status_label": STATUS_LABELS[status],
        "workday": workday,
        "next": _next_change(now, workday, g13_exec._config(settings)) if status == "active" else None,
        "today": _zone_energy(conn, settings, today, today),
        "week": _zone_energy(conn, settings, today - timedelta(days=WEEK_DAYS - 1), today),
        "trips_7d": sum(1 for d in st.get("trips") or [] if d >= week_from),
    }
