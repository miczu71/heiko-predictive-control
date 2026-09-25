"""Bieżące wartości z HA dla pulpitu — temperatury pokoi, klimatyzatory, pompa,
taryfa. Tylko odczyt. Gettery wstrzykiwane (testy bez HA); ten sam słownik
zasila render strony i endpoint /api/live, z którego pulpit odświeża się co
minutę bez przeładowania."""
from __future__ import annotations

from typing import Any, Callable

from .rooms import House, temp_class

HVAC_LABELS = {
    "off": "wyłączona", "heat": "grzanie", "cool": "chłodzenie", "auto": "auto",
    "heat_cool": "auto", "dry": "osuszanie", "fan_only": "wentylator",
}
PUMP_MODE_LABELS = {
    "standby": "czuwanie", "heating": "grzanie", "dhw": "CWU", "cooling": "chłodzenie",
    "auto": "auto", "off": "wyłączona",
}
_UNAVAILABLE = (None, "", "unavailable", "unknown")


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_temp(value: Any) -> str:
    v = _to_float(value)
    return "—" if v is None else f"{v:.1f}".replace(".", ",") + "°C"


def fmt_price(value: Any) -> str:
    v = _to_float(value)
    return "—" if v is None else f"{v:.2f}".replace(".", ",") + " PLN/kWh"


def _as_bool(state: Any) -> bool | None:
    s = str(state).strip().lower() if state is not None else ""
    if s in ("true", "on", "1"):
        return True
    if s in ("false", "off", "0"):
        return False
    return None


def _state(get_state: Callable, entity: str | None) -> tuple[str | None, dict]:
    data = get_state(entity) if entity else None
    if not data:
        return None, {}
    state = data.get("state")
    return (None if state in _UNAVAILABLE else state), (data.get("attributes") or {})


def collect(settings: dict, get_state: Callable[[str], dict | None],
            get_numeric: Callable[[str], float | None], house: House) -> dict:
    zone_entities = {e.strip() for e in str(settings.get("day_zone_temp_entities", "")).split(",")
                     if e.strip()}

    rooms: dict[str, dict] = {}
    zone_temps: list[float] = []
    for room in house.rooms:
        if room.entity is None:
            continue
        entity = room.entity
        if room.card == "loop_attic":
            # pokój pętli B: ten sam czujnik co pętla — honorujemy nadpisanie w opcjach
            entity = settings.get("attic_temp_entity") or room.entity
        t = get_numeric(entity)
        in_zone = entity in zone_entities
        if in_zone and t is not None:
            zone_temps.append(t)
        rooms[room.key] = {"temp": t, "text": fmt_temp(t), "cls": temp_class(t), "zone": in_zone}

    equipment: dict[str, dict] = {}
    pump_mode = "—"
    for eq in house.equipment:
        state, attrs = _state(get_state, eq.entity)
        if eq.kind == "ac":
            equipment[eq.key] = {
                "state": HVAC_LABELS.get(state, state) if state else "niedostępna",
                "on": state not in (None, "off"),
                "current": fmt_temp(attrs.get("current_temperature")),
                "target": fmt_temp(attrs.get("temperature")),
            }
        else:
            mode = (state or "").strip().lower()
            equipment[eq.key] = {
                "state": PUMP_MODE_LABELS.get(mode, state) if state else "niedostępna",
                "on": bool(state) and mode not in ("standby", "off"),
            }
            pump_mode = equipment[eq.key]["state"]

    tariff_state, _ = _state(get_state, settings.get("tariff_state_entity"))
    peak = _as_bool(tariff_state)
    tariff = {
        "peak": peak,
        "label": "—" if peak is None else ("SZCZYT" if peak else "poza szczytem"),
        "price": fmt_price(get_numeric(settings.get("tariff_price_entity") or "")),
    }

    curve_state, _ = _state(get_state, settings.get("heiko_curve_switch_entity"))
    avg = sum(zone_temps) / len(zone_temps) if zone_temps else None
    heiko = {
        "setpoint": fmt_temp(get_numeric(settings.get("heiko_setpoint_entity") or "")),
        "outdoor": fmt_temp(get_numeric(settings.get("outdoor_temp_entity") or "")),
        "avg": fmt_temp(avg),
        "mode": pump_mode,
        "curve": {True: "włączona", False: "wyłączona"}.get(_as_bool(curve_state), "—"),
    }

    door_state, _ = _state(get_state, settings.get("attic_door_entity"))
    door_open = _as_bool(door_state)
    attic = {"door": {True: "otwarte", False: "zamknięte"}.get(door_open, "—")}

    return {"rooms": rooms, "equipment": equipment, "tariff": tariff, "heiko": heiko,
            "attic": attic}
