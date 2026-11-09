"""Tryb „Szczyt G13” — widok dla pulpitu (E4), tylko odczyt: stan trybu, następna zmiana, energia i koszt
podłogówki w strefie `evening_peak` (dziś i 7 dni, z dziennika runów) oraz zadziałania bezpiecznika."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db as dbm
from . import g13_exec, g13_peak, heating_runs, tariff_feed, vacation
from .summaries import day_bounds

PEAK_ZONE = g13_peak.PEAK_ZONE
WEEK_DAYS = 7
STATUS_LABELS = {"off": "wyłączony", "blocked": "wstrzymany: sterowanie Heiko wyłączone", "suspended": "wstrzymany do jutra", "tripped": "bezpiecznik — bazowe do końca szczytu",
                 "active": "włączony", "vacation": "urlop: szczyt −3"}


def _zone_energy(runs: list[dict]) -> dict:
    kwh = pln = 0.0
    for run in runs:
        zone = (run.get("zones") or {}).get(PEAK_ZONE)
        if zone:
            kwh += zone.get("kwh") or 0.0
            pln += zone.get("pln") or 0.0
    return {"kwh": round(kwh, 2), "pln": round(pln, 2)}


def _next_change(now: datetime, window: g13_peak.Window | None, cfg: g13_peak.Config) -> dict | None:
    """Najbliższa zmiana dziś (dogrzanie/szczyt/koniec okna) z docelowym przesunięciem względem bazowego; bez okna brak.
    Okno do północy nie ma kroku „koniec” (powrót przy zmianie doby)."""
    if window is None or window.empty:
        return None
    steps = [(window.preheat_from, f"{cfg.preheat_shift:+g}"), (window.start, f"{cfg.peak_shift:+g}")]
    if window.end < 24:
        steps.append((window.end, "powrót do bazowego"))
    for hour, value in steps:
        if now.hour < hour:
            return {"at": f"{hour:02d}:00", "value": value}
    return None


def _now_phase(now: datetime, window: g13_peak.Window | None, st: dict) -> dict | None:
    """Faza okna i przesunięcie krzywej (wartość bezwzględna: bazowe + dogrzanie/szczyt) ostatnio zapisane przez tryb
    dziś; poza oknem, bez szczytu w taryfie, bez dzisiejszego zapisu albo z danymi z wczoraj brak."""
    shift = st.get("last_written")
    if window is None or window.empty or shift is None or st.get("day") != now.date().isoformat():
        return None
    if window.preheat_from <= now.hour < window.start:
        phase = f"przedgrzewanie do {window.start:02d}:00"
    elif window.start <= now.hour < window.end:
        phase = f"szczyt do {window.end:02d}:00"
    else:
        return None
    base = st.get("base")
    return {"phase": phase, "shift": f"{shift:+g}", "base": None if base is None else f"{base:g}"}


def _status(settings: dict, st: dict, today: date) -> str:
    if not settings.get("g13_peak_enabled", False):
        return "off"
    if vacation.is_active(settings, today) and settings.get("heiko_enabled"):
        return "vacation"
    if not g13_exec.active(settings):
        return "blocked"
    if st.get("suspended_day") == today.isoformat():
        return "suspended"
    if st.get("tripped") and st.get("day") == today.isoformat():
        return "tripped"
    return "active"


def now_view(conn, settings: dict, now: datetime) -> dict | None:
    """Tylko „teraz” (faza i przesunięcie) — lekkie, bez runów; dla kafla „Zapisy do pompy” na pulpicie."""
    st = dbm.get_setting(conn, g13_exec.STATE_KEY) or {}
    if _status(settings, st, now.date()) != "active":
        return None
    window = g13_peak.window_from_feed(tariff_feed.for_settings(conn, settings), now.date())
    return _now_phase(now, window, st)


def build(conn, settings: dict, now: datetime) -> dict:
    today = now.date()
    st = dbm.get_setting(conn, g13_exec.STATE_KEY) or {}
    status = _status(settings, st, today)
    feed = tariff_feed.for_settings(conn, settings)
    window = g13_peak.window_from_feed(feed, today)
    week_start = today - timedelta(days=WEEK_DAYS - 1)
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    today_start, _ = day_bounds(today, tz_name)
    week_runs = heating_runs.load_range(conn, day_bounds(week_start, tz_name)[0], day_bounds(today, tz_name)[1])
    today_runs = [r for r in week_runs if datetime.fromisoformat(r["start"]) >= today_start]
    return {
        "enabled": bool(settings.get("g13_peak_enabled", False)), "status": status, "status_label": STATUS_LABELS[status],
        "workday": None if window is None else not window.empty,       # None = brak harmonogramu, False = brak szczytu
        "window": None if window is None or window.empty else {"start": f"{window.start:02d}:00", "end": f"{window.end:02d}:00"},
        "next": _next_change(now, window, g13_exec._config(settings)) if status == "active" else None,
        "now": _now_phase(now, window, st) if status == "active" else None,
        "today": _zone_energy(today_runs),
        "week": _zone_energy(week_runs),
        "trip_room": st.get("trip_room") if status == "tripped" else None,
        "trips_7d": sum(1 for d in st.get("trips") or [] if d >= week_start.isoformat()),
    }
