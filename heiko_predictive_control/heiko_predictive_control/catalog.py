"""Katalog parametrów pompy Heiko dostępnych w integracji `heiko_heatpump` (funkcje czyste).

Katalog opisuje, CO doradca może kiedyś proponować (klasy bezpieczeństwa A/B/C i zakresy),
i które sensory pompy warto logować. **Nie zapisuje niczego** — w D1 służy wyłącznie do
odczytu i logowania zmian. Repo add-onu jest publiczne, więc encje są identyfikowane po
SUFIKSIE (`heiko_heat_pump_<klucz>`); pełny entity_id (z ewentualnym prefiksem obszaru
domu, np. dla pompy obiegowej) rozwiązuje się w czasie pracy z listy stanów HA.

Klasy:
  A — rutynowe, odwracalne, małe zakresy: zwykłe zatwierdzenie (zakres A i limit kroku niżej),
  B — wymagają silniejszych dowodów i dodatkowego potwierdzenia (także wartości spoza zakresu A),
  C — add-on nie zapisuje nigdy (doradca tylko wspomina w tekście)."""
from __future__ import annotations

import re
from dataclasses import dataclass

CLASS_A, CLASS_B, CLASS_C = "A", "B", "C"


@dataclass(frozen=True)
class Param:
    key: str
    suffix: str                       # koniec object_id encji: heiko_heat_pump_<suffix>
    domain: str                       # number | select | switch
    cls: str                          # klasa bezpieczeństwa
    label: str
    impact: str                       # na co wpływa (po polsku)
    unit: str = ""
    full_min: float | None = None     # pełny zakres integracji (number)
    full_max: float | None = None
    a_min: float | None = None        # zakres klasy A (poza nim → klasa B)
    a_max: float | None = None
    max_step: float | None = None     # maks. zmiana na jedno zatwierdzenie (klasa A)
    options: tuple[str, ...] = ()     # dozwolone wartości (select)
    note: str = ""


def _n(key, suffix, cls, label, impact, unit, full, a=None, step=None, note=""):
    a = a or full
    return Param(key, suffix, "number", cls, label, impact, unit, full[0], full[1], a[0], a[1], step, (), note)


_SPEED = ("Wysokie obroty", "Średnie obroty", "Niskie obroty")

CATALOG: tuple[Param, ...] = (
    # ── Klasa A ────────────────────────────────────────────────────────────
    _n("curve_shift", "heating_curve_parallel_shift", CLASS_A, "Przesunięcie krzywej grzewczej",
       "komfort, koszt ogrzewania", "°C", (-9, 9), (-4, 4), 1,
       "działa tylko przy włączonej krzywej"),
    _n("heating_stops_dt", "heating_stops_dt", CLASS_A, "Histereza: stop ogrzewania",
       "cykle sprężarki, COP, rozrzut temperatury", "K", (1, 15), (1, 5)),
    _n("heating_restarts_dt", "heating_restarts_dt", CLASS_A, "Histereza: restart ogrzewania",
       "cykle sprężarki, COP, rozrzut temperatury", "K", (1, 15), (1, 5)),
    _n("dhw_restart_dt", "dhw_restart_dt", CLASS_A, "Histereza CWU",
       "częstość podgrzewania CWU, koszt", "K", (1, 15), (3, 10)),
    _n("dhw_setpoint", "dhw_setpoint", CLASS_A, "Nastawa CWU",
       "koszt CWU, komfort ciepłej wody", "°C", (40, 60), (45, 55), 2,
       "w domu zarządzana automatyzacjami (48/58 wg nadwyżki PV)"),
    _n("p0_run_time", "circulation_pump_p0_run_time", CLASS_A, "Pompa obiegowa P0: czas pracy",
       "zużycie prądu, przepływ, komfort zimą", "min", (1, 30)),
    _n("p0_stop_time", "circulation_pump_p0_stop_time", CLASS_A, "Pompa obiegowa P0: czas postoju",
       "zużycie prądu, przepływ, komfort zimą", "min", (1, 60)),
    Param("p0_speed_heating", "circulation_pump_p0_speed_heating", "select", CLASS_A,
          "Pompa obiegowa P0: obroty (ogrzewanie)", "zużycie prądu, przepływ", options=_SPEED),
    Param("p0_speed_dhw", "circulation_pump_p0_speed_dhw", "select", CLASS_A,
          "Pompa obiegowa P0: obroty (CWU)", "zużycie prądu, przepływ", options=_SPEED),
    # ── Klasa B ────────────────────────────────────────────────────────────
    *(_n(f"curve_ambient_{i}", f"curve_ambient_temp_{i}", CLASS_B, f"Krzywa: temp. zewn. punkt {i}",
         "kształt krzywej grzewczej", "°C", (-25, 20)) for i in range(1, 6)),
    *(_n(f"curve_water_{i}", f"curve_water_temp_{i}", CLASS_B, f"Krzywa: temp. wody punkt {i}",
         "kształt krzywej grzewczej", "°C", (15, 60)) for i in range(1, 6)),
    Param("p0_mode", "circulation_pump_p0_mode", "select", CLASS_B, "Pompa obiegowa P0: tryb",
          "zużycie prądu, przepływ"),
    Param("p0_type", "circulation_pump_p0_type", "select", CLASS_B, "Pompa obiegowa P0: typ",
          "zużycie prądu, przepływ"),
    Param("dhw_storage", "dhw_storage", "switch", CLASS_B, "Magazynowanie CWU", "zaplanowane CWU, koszt"),
    # Rejestr idx 50 = pozycja 9.4 menu „Priorytet dla dodatkowego źródła ciepła w podgrzewaczu c.w.u.” (HWTBH vs AH),
    # a NIE włącznik grzałki HBH (HBH to 9.1/9.2 = idx 47/48, niewystawione w integracji). Odwrócona logika:
    # ON = 0.0 = „Niższe dla grzałki wewnętrznej AH” (AH ma pierwszeństwo jako wspomaganie CWU).
    Param("backup_heater", "backup_heater_hbh", "switch", CLASS_B, "Priorytet grzałek CWU: AH vs HWTBH",
          "koszt (prąd) przy CWU, szybkość podgrzewu CWU",
          note="w integracji „Backup Heater (HBH)”, w rzeczywistości menu 9.4; ON = AH ma pierwszeństwo"),
    Param("anti_leg_program", "anti_legionella_program", "switch", CLASS_B, "Anti-legionella: program",
          "higiena wody, koszt", note="nigdy nie obniżać poniżej normy higienicznej"),
    _n("anti_leg_setpoint", "anti_legionella_setpoint", CLASS_B, "Anti-legionella: temperatura",
       "higiena wody, koszt", "°C", (40, 70)),
    _n("anti_leg_duration", "anti_legionella_duration", CLASS_B, "Anti-legionella: czas",
       "higiena wody, koszt", "min", (1, 120)),
    _n("anti_leg_finish", "anti_legionella_finish_time", CLASS_B, "Anti-legionella: okno",
       "higiena wody, koszt", "min", (1, 240)),
    # ── Klasa C (tylko obserwacja; doradca nie zapisuje) ───────────────────
    Param("working_mode", "working_mode", "select", CLASS_C, "Tryb pracy pompy", "krytyczne"),
    Param("power", "heat_pump_power", "switch", CLASS_C, "Zasilanie pompy", "krytyczne"),
    Param("vacation_mode", "vacation_mode", "switch", CLASS_C, "Tryb wakacyjny", "krytyczne"),
    Param("heating_curve", "heating_curve", "switch", CLASS_C, "Krzywa grzewcza (włącznik)",
          "krytyczne", note="włącza user na starcie sezonu"),
)

BY_KEY: dict[str, Param] = {p.key: p for p in CATALOG}

# Sensory pompy logowane co cykl (sufiks object_id; domena sensor). Wartość tekstowa/`unknown`
# zapisuje się jako NULL; kod trybu pracy (`working_mode`) to liczba, nie napis.
TELEMETRY_SENSORS: tuple[str, ...] = (
    "ah_working_time", "ambient_air_temperature", "compressor_current", "compressor_frequency", "condenser_temperature",
    "cop_estimated", "discharge_pressure", "discharge_temperature", "electrical_power",
    "expansion_valve_opening", "fan_1_speed", "fan_2_speed", "hbh_working_time", "hwtbh_working_time",
    "outdoor_unit_inlet_temperature", "outdoor_unit_outlet_temperature", "pipe_temperature",
    "pwm_duty_cycle", "refrigerant_temperature", "suction_pressure", "suction_temperature",
    "supply_voltage", "thermal_output_power", "water_circuit_delta_t", "water_pump_state",
    "water_temperature", "water_temperature_setpoint", "working_mode",
)
TELEMETRY_BINARY: tuple[str, ...] = ("water_pump_p1", "water_pump_p2")

_PREFIX = "heiko_heat_pump_"


def _matches(object_id: str, suffix: str) -> bool:
    return re.search(rf"(?:^|_){_PREFIX}{re.escape(suffix)}$", object_id) is not None


def _find(states: list[dict], domain: str, suffix: str) -> str | None:
    for st in states:
        eid = st.get("entity_id", "")
        head, _, obj = eid.partition(".")
        if head == domain and _matches(obj, suffix):
            return eid
    return None


def resolve_params(states: list[dict]) -> dict[str, str]:
    """{klucz katalogu: entity_id} dla parametrów, które istnieją w HA."""
    out = {}
    for p in CATALOG:
        eid = _find(states, p.domain, p.suffix)
        if eid:
            out[p.key] = eid
    return out


def resolve_telemetry(states: list[dict]) -> dict[str, str]:
    """{nazwa sensora (sufiks): entity_id} dla sensorów pompy z listy telemetrii."""
    out = {}
    for suffix in TELEMETRY_SENSORS:
        eid = _find(states, "sensor", suffix)
        if eid:
            out[suffix] = eid
    for suffix in TELEMETRY_BINARY:
        eid = _find(states, "binary_sensor", suffix)
        if eid:
            out[suffix] = eid
    return out


def check_class_a(key: str, current: float, new: float) -> tuple[bool, str]:
    """Czy zmianę current→new wolno zaproponować jako klasę A (zakres + limit kroku)?
    Poza zakresem A albo większy krok → nie (wymaga klasy B). Klasy B/C: zawsze nie."""
    p = BY_KEY.get(key)
    if p is None or p.cls != CLASS_A or p.domain != "number":
        return False, "parametr nie jest liczbowym parametrem klasy A"
    if not (p.full_min <= new <= p.full_max):
        return False, f"poza zakresem integracji ({p.full_min}…{p.full_max})"
    if not (p.a_min <= new <= p.a_max):
        return False, f"poza zakresem klasy A ({p.a_min}…{p.a_max}) — wymaga klasy B"
    if p.max_step is not None and abs(new - current) > p.max_step:
        return False, f"krok większy niż {p.max_step:g} na jedno zatwierdzenie"
    return True, ""


def to_number(state) -> float | None:
    """Stan encji → liczba do telemetrii (on/off → 1/0; nienumeryczne → None)."""
    if state is None:
        return None
    s = str(state).strip().lower()
    if s in ("on", "true"):
        return 1.0
    if s in ("off", "false"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def describe(p: Param) -> dict:
    """Sam opis parametru (do /api/catalog) — bez stanu HA."""
    return {"key": p.key, "label": p.label, "cls": p.cls, "domain": p.domain, "impact": p.impact,
            "unit": p.unit, "full_range": [p.full_min, p.full_max], "class_a_range": [p.a_min, p.a_max],
            "max_step": p.max_step, "options": list(p.options), "note": p.note}
