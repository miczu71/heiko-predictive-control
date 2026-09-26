"""Alarm komfortu — WYŁĄCZNIE powiadomienie dla użytkownika (`notify`), nigdy zapis do
pompy. Najzimniejszy pokój stref dziennych poniżej minimum przez ≥ 30 min → jedno
powiadomienie na epizod; epizod kończy się, gdy pokój wróci powyżej minimum + margines."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import db as dbm
from . import ha_client

logger = logging.getLogger(__name__)

ALARM_MINUTES = 30
RECOVERY_MARGIN_C = 0.3
_STATE_KEY = "heiko_comfort_alarm"


def comfort_alarm(conn, settings: dict, now: datetime, notify=ha_client.notify) -> str | None:
    """Zwraca "alarm" (wysłano/oznaczono nowy epizod), "koniec" albo None."""
    room_min = float(settings.get("heiko_room_min_c", 18.5))
    since = (now - timedelta(minutes=ALARM_MINUTES + 10)).isoformat()
    rows = conn.execute(
        "SELECT ts, min_room_c, min_room_name, reduced_active FROM cycles "
        "WHERE loop = 'heiko' AND ts >= ? ORDER BY id", (since,)).fetchall()
    state = dbm.get_setting(conn, _STATE_KEY) or {"active": False}
    if not rows:
        return None
    last = rows[-1]
    if state.get("active"):
        if last["min_room_c"] is not None and last["min_room_c"] >= room_min + RECOVERY_MARGIN_C:
            dbm.set_setting(conn, _STATE_KEY, {"active": False, "ended": now.isoformat(timespec="minutes")})
            return "koniec"
        return None
    if len(rows) < 3 or any(r["min_room_c"] is None or r["min_room_c"] >= room_min for r in rows):
        return None
    span = (datetime.fromisoformat(rows[-1]["ts"]) - datetime.fromisoformat(rows[0]["ts"])).total_seconds() / 60
    if span < ALARM_MINUTES - 1:
        return None
    msg = f"{last['min_room_name'] or 'Pokój'} ma {last['min_room_c']:.1f}°C (minimum {room_min:.1f}°C) od ponad {ALARM_MINUTES} min."
    if last["reduced_active"]:
        msg += " Ograniczona nastawa jest aktywna — rozważ jej wyłączenie na panelu pompy (menu 5.1)."
    dbm.set_setting(conn, _STATE_KEY, {"active": True, "since": rows[0]["ts"]})
    notify(settings.get("notify_service", ""), "Heiko Predictive: pokój za zimny", msg)
    logger.warning("Alarm komfortu: %s", msg)
    return "alarm"
