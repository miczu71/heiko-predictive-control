"""Tryb urlopowy — widok dla pulpitu (S7), tylko odczyt: status, parametry, podsumowanie i bezpiecznik z 30 dni."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db as dbm
from . import vacation, vacation_exec

TRIP_DAYS = 30
STATUS_LABELS = {"off": "wyłączony", "scheduled": "zaplanowany od {}", "expired": "zakończony",
                 "blocked": "wstrzymany: sterowanie Heiko wyłączone", "tripped": "bezpiecznik — bazowe do ocieplenia",
                 "suspended": "wstrzymany do jutra", "active": "włączony"}


def _iso(value) -> str | None:
    return value or None


def _dm(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m")


def _status(settings: dict, st: dict, today: date) -> str:
    if not settings.get("vacation_enabled", False):
        return "off"
    try:
        start, end = _iso(settings.get("vacation_from")), _iso(settings.get("vacation_to"))
        if start and today < date.fromisoformat(start):
            return "scheduled"
        if end and today > date.fromisoformat(end):
            return "expired"
    except (TypeError, ValueError):
        return "off"
    if not settings.get("heiko_enabled"):
        return "blocked"
    if st.get("tripped"):
        return "tripped"
    if st.get("suspended_day") == today.isoformat():
        return "suspended"
    return "active"


def build(conn, settings: dict, now: datetime) -> dict:
    today = now.date()
    st = dbm.get_setting(conn, vacation_exec.STATE_KEY) or {}
    status = _status(settings, st, today)
    start, end = _iso(settings.get("vacation_from")), _iso(settings.get("vacation_to"))
    label = STATUS_LABELS[status]
    if status == "scheduled":
        label = label.format(_dm(start))
    cfg, dhw = vacation_exec._config(settings), vacation_exec.dhw_target(settings)
    summary = ""
    if status != "off":
        drop_txt = "bez obniżki" if cfg.drop == 0 else f"−{cfg.drop}"
        summary = f"Urlop{f' do {_dm(end)}' if end else ''} · krzywa {drop_txt} · CWU {dhw}"
    since = (today - timedelta(days=TRIP_DAYS)).isoformat()
    return {
        "enabled": bool(settings.get("vacation_enabled", False)), "status": status, "status_label": label,
        "from": start, "to": end, "drop": cfg.drop, "min_room_c": cfg.min_room_c, "dhw": dhw,
        "with_away": bool(settings.get("vacation_with_away", False)),
        "peak_pressing": bool(settings.get("g13_peak_enabled", False)),
        "summary": summary, "dhw_warn": st.get("dhw_warn"),
        "trip_room": st.get("trip_room") if status == "tripped" else None,
        "trips_30d": sum(1 for d in st.get("trips") or [] if d >= since),
    }
