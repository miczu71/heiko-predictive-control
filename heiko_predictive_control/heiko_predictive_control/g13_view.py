"""Tryb „Szczyt G13” — widok dla pulpitu (E4), tylko odczyt: stan trybu, następna zmiana, energia i koszt
podłogówki w strefie `evening_peak` (dziś i 7 dni, z dziennika runów) oraz zadziałania bezpiecznika."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db as dbm
from . import g13_exec, g13_peak, heating_runs, tariff_feed
from .summaries import day_bounds

PEAK_ZONE = "evening_peak"
WEEK_DAYS = 7
STATUS_LABELS = {"off": "wyłączony", "blocked": "wstrzymany: sterowanie Heiko wyłączone", "suspended": "wstrzymany do jutra", "tripped": "bezpiecznik — bazowe do 21:00",
                 "active": "włączony"}


def _zone_energy(runs: list[dict]) -> dict:
    kwh = pln = 0.0
    for run in runs:
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


def _now_phase(now: datetime, workday: bool | None, st: dict) -> dict | None:
    """Faza okna i przesunięcie krzywej (wartość bezwzględna: bazowe + dogrzanie/szczyt) ostatnio zapisane przez tryb
    dziś; poza oknem, w dzień wolny, bez dzisiejszego zapisu albo z danymi z wczoraj brak."""
    shift = st.get("last_written")
    if not workday or shift is None or st.get("day") != now.date().isoformat():
        return None
    if g13_peak.PREHEAT_FROM_H <= now.hour < g13_peak.PEAK_FROM_H:
        phase = f"przedgrzewanie do {g13_peak.PEAK_FROM_H:02d}:00"
    elif g13_peak.PEAK_FROM_H <= now.hour < g13_peak.WINDOW_END_H:
        phase = f"szczyt do {g13_peak.WINDOW_END_H:02d}:00"
    else:
        return None
    base = st.get("base")
    return {"phase": phase, "shift": f"{shift:+g}", "base": None if base is None else f"{base:g}"}


def _status(settings: dict, st: dict, today: date) -> str:
    if not settings.get("g13_peak_enabled", False):
        return "off"
    if not g13_exec.active(settings):
        return "blocked"
    if st.get("suspended_day") == today.isoformat():
        return "suspended"
    if st.get("tripped") and st.get("day") == today.isoformat():
        return "tripped"
    return "active"


def now_view(conn, settings: dict, now: datetime, is_workday) -> dict | None:
    """Tylko „teraz” (faza i przesunięcie) — lekkie, bez runów; dla kafla „Zapisy do pompy” na pulpicie."""
    st = dbm.get_setting(conn, g13_exec.STATE_KEY) or {}
    if _status(settings, st, now.date()) != "active":
        return None
    workday = g13_peak.workday_from_feed(tariff_feed.for_settings(conn, settings, is_workday), now.date(), now)
    return _now_phase(now, workday, st)


def build(conn, settings: dict, now: datetime, is_workday) -> dict:
    today = now.date()
    st = dbm.get_setting(conn, g13_exec.STATE_KEY) or {}
    status = _status(settings, st, today)
    feed = tariff_feed.for_settings(conn, settings, is_workday)
    workday = g13_peak.workday_from_feed(feed, today, now)
    week_start = today - timedelta(days=WEEK_DAYS - 1)
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    today_start, _ = day_bounds(today, tz_name)
    week_runs = heating_runs.load_range(conn, day_bounds(week_start, tz_name)[0], day_bounds(today, tz_name)[1])
    today_runs = [r for r in week_runs if datetime.fromisoformat(r["start"]) >= today_start]
    return {
        "enabled": bool(settings.get("g13_peak_enabled", False)), "status": status, "status_label": STATUS_LABELS[status],
        "workday": workday,
        "next": _next_change(now, workday, g13_exec._config(settings)) if status == "active" else None,
        "now": _now_phase(now, workday, st) if status == "active" else None,
        "today": _zone_energy(today_runs),
        "week": _zone_energy(week_runs),
        "trips_7d": sum(1 for d in st.get("trips") or [] if d >= week_start.isoformat()),
    }
