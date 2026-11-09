"""Ogon grzania (0.22.0) — widok dla kafla na pulpicie: stan, odliczanie, liczniki odcięć. Tylko odczyt `tail_cut_state`."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import db as dbm
from . import tail_cut

WEEK_DAYS = 7
STATUS_LABELS = {"off": "wyłączony", "blocked": "wstrzymany: sterowanie Heiko wyłączone", "holding": "trzyma CWU",
                 "pause": "przerwa po odcięciu", "watching": "czuwa"}


def _cut_date(iso: str):
    return datetime.fromisoformat(iso).date()


def _status(settings: dict, st: dict, now: datetime) -> str:
    if not settings.get("tail_cut_enabled", False):
        return "off"
    if not settings.get("heiko_enabled"):
        return "blocked"
    if st.get("forced"):
        return "holding"
    if st.get("pause_until") and now < datetime.fromisoformat(st["pause_until"]):
        return "pause"
    return "watching"


def build(conn, settings: dict, now: datetime) -> dict:
    st = dbm.get_setting(conn, tail_cut.STATE_KEY) or {}
    status = _status(settings, st, now)
    cuts = list(st.get("cuts") or [])
    today = now.date()
    week_start = today - timedelta(days=WEEK_DAYS - 1)
    since = st.get("tail_since")
    tail_minutes = None
    if since:
        tail_minutes = max(0, int((now - datetime.fromisoformat(since)).total_seconds() // 60))
    return {
        "status": status, "status_label": STATUS_LABELS[status],
        "margin_k": tail_cut._num(settings, "tail_margin_k", 1.0),
        "delay_min": tail_cut._num(settings, "tail_delay_min", 45),
        "hold_min": tail_cut._num(settings, "tail_dhw_hold_min", 2),
        "min_room_c": tail_cut._num(settings, "tail_min_room_c", 20.5),
        "pause_min": tail_cut._num(settings, "tail_min_pause_min", 30),
        "tail_minutes": tail_minutes,
        "cuts_today": sum(1 for c in cuts if _cut_date(c) == today),
        "cuts_7d": sum(1 for c in cuts if _cut_date(c) >= week_start),
        "cuts_total": len(cuts),
        "last_cut": cuts[-1] if cuts else None,
        "pause_until": st.get("pause_until"),
        "last": st.get("last"),
    }
