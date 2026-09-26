"""Bieżące wartości z HA dla pulpitu — temperatury pokoi, klimatyzatory, pompa,
taryfa. Tylko odczyt. Gettery wstrzykiwane (testy bez HA); ten sam słownik
zasila render strony i endpoint /api/live, z którego pulpit odświeża się co
minutę bez przeładowania."""
from __future__ import annotations

from typing import Any, Callable

from .cycle import DEFAULT_WATER_TARGET_ENTITY
from .rooms import House, temp_class

HVAC_LABELS = {
    "off": "wyłączona", "heat": "grzanie", "cool": "chłodzenie", "auto": "auto",
    "heat_cool": "auto", "dry": "osuszanie", "fan_only": "wentylator",
}
PUMP_MODE_LABELS = {
    "standby": "czuwanie", "heating": "grzanie", "dhw": "CWU", "cooling": "chłodzenie",
    "auto": "auto", "off": "wyłączona",
}
PHASE_LABELS = {
    "poza_oknem": "poza oknem pracy", "czeka": "czeka na start dogrzewania",
    "dogrzewanie": "dogrzewanie", "utrzymanie": "utrzymanie temperatury",
    "pauza_okno": "pauza — otwarte okno", "przejecie_reczne": "ręczna zmiana — nie steruję do jutra",
    "reczne_uzycie": "klimatyzator włączony ręcznie", "brak_ac": "klimatyzator niedostępny",
    "koniec_okna": "koniec okna pracy", "przekazanie": "sterowanie wyłączone",
    "dry_run": "tryb obserwacji (zapisałbym)",
    "wstrzymane": "wstrzymane (urlop / pauza)",
    "pusto": "pusto — nikogo na poddaszu",
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

    water_target = get_numeric(settings.get("heiko_water_setpoint_entity") or DEFAULT_WATER_TARGET_ENTITY)
    curve_state, _ = _state(get_state, settings.get("heiko_curve_switch_entity"))
    avg = sum(zone_temps) / len(zone_temps) if zone_temps else None
    heiko = {
        # Aktualny cel wody wg pompy (krzywa + przesunięcie + ograniczenie); przy krzywej ON encja
        # `number` nastawy jest niedostępna, więc to jej zastępnik (fallback: nastawa stała).
        "setpoint": fmt_temp(water_target if water_target is not None
                             else get_numeric(settings.get("heiko_setpoint_entity") or "")),
        "reduced": "—",
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


def reduced_label(reduced_active, curve_on) -> str:
    """Stan natywnej „ograniczonej nastawy” (wnioskowany z celu wody vs krzywa)."""
    if reduced_active == 1:
        return "aktywna"
    if reduced_active == 0:
        return "nieaktywna"
    return "nieznany (krzywa wyłączona)" if curve_on == 0 else "—"


def fmt_vacant(presence: Any, vacant_min: Any) -> str:
    """„jest” / „brak od 48 min” / „brak od 1 h 35 min” / „—” (czujnik nieznany)."""
    if presence == 1:
        return "jest"
    minutes = _to_float(vacant_min)
    if presence != 0 or minutes is None:
        return "—"
    total = round(minutes)
    if total < 60:
        return f"brak od {total} min"
    return f"brak od {total // 60} h {total % 60} min"


def ctrl_summary(row: dict | None, state: dict | None, totals: dict | None) -> dict:
    """Stan pętli B do karty poddasza: ostatni cykl (wiersz z bazy), trwały stan
    sterowania i sumy dzienne. Tylko formatowanie — bez I/O."""
    row, state, totals = row or {}, state or {}, totals or {}
    planned = str(row.get("planned_start") or "")
    offset = _to_float(state.get("offset_c"))
    return {
        "phase": PHASE_LABELS.get(row.get("phase"), "—"),
        "owned": "tak" if state.get("owned") else "nie",
        "offset": "—" if offset is None else f"{offset:+.1f}".replace(".", ",") + "°C",
        "planned_start": planned[11:16] if len(planned) >= 16 else "—",
        "last_setpoint": fmt_temp(row.get("ac_cmd_setpoint")),
        "window": {1: "otwarte", 0: "zamknięte"}.get(row.get("window_open"), "—"),
        "presence": fmt_vacant(row.get("presence"), row.get("vacant_min")),
        "today": ("—" if not totals else
                  f"{totals.get('pln', 0.0):.2f}".replace(".", ",") + " PLN · "
                  + f"{totals.get('kwh', 0.0):.2f}".replace(".", ",") + " kWh"),
    }
