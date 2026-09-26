"""Katalog parametrów pompy: klasy i zakresy wg decyzji z wywiadu 26.09, rozwiązywanie encji po sufiksie."""
import inspect

import pytest

from heiko_predictive_control import catalog


# Dodatkowe źródła ciepła (sloty 47–52) i ograniczona nastawa (77/78) — tylko obserwacja (klasa C).
SLOT_KEYS = ("backup_heating_enabled", "backup_heating_priority", "backup_dhw_enabled", "backup_heater",
             "backup_start_dependency", "backup_start_delay", "reduced_setpoint", "reduced_setpoint_drop")


def test_keys_unique_and_classes_valid():
    keys = [p.key for p in catalog.CATALOG]
    assert len(keys) == len(set(keys))
    assert {p.cls for p in catalog.CATALOG} <= {"A", "B", "C"}
    for p in catalog.CATALOG:
        if p.domain == "number":
            assert p.full_min is not None and p.a_min >= p.full_min and p.a_max <= p.full_max, p.key


@pytest.mark.parametrize("key,cur,new,ok", [
    ("curve_shift", 0, 1, True),
    ("curve_shift", 0, -1, True),
    ("curve_shift", 0, 2, False),           # krok > 1
    ("curve_shift", 4, 5, False),           # poza ±4
    ("curve_shift", 0, 9, False),
    ("heating_stops_dt", 1, 5, True),
    ("heating_stops_dt", 1, 6, False),      # A tylko 1–5
    ("heating_restarts_dt", 2, 3, True),
    ("dhw_restart_dt", 5, 3, True),
    ("dhw_restart_dt", 5, 2, False),        # A tylko 3–10
    ("dhw_restart_dt", 5, 11, False),
    ("dhw_setpoint", 48, 50, True),
    ("dhw_setpoint", 48, 51, False),        # krok > 2
    ("dhw_setpoint", 48, 44, False),        # poniżej 45
    ("dhw_setpoint", 55, 57, False),        # powyżej 55
    ("p0_run_time", 1, 30, True),           # pełny zakres integracji, bez limitu kroku
    ("p0_run_time", 1, 31, False),
    ("p0_stop_time", 6, 60, True),
])
def test_class_a_ranges_and_step(key, cur, new, ok):
    assert catalog.check_class_a(key, cur, new)[0] is ok


@pytest.mark.parametrize("key", ["curve_water_1", "backup_heater", "backup_start_delay", "reduced_setpoint_drop", "working_mode",
                                  "vacation_mode", "p0_speed_heating", "nie_ma_takiego"])
def test_non_class_a_or_non_numeric_never_passes(key):
    assert catalog.check_class_a(key, 1, 2)[0] is False


def test_b_and_c_classes_match_plan():
    for key in ("dhw_storage", "p0_mode", "p0_type", "curve_water_3"):
        assert catalog.BY_KEY[key].cls == "B"
    for key in ("working_mode", "power", "vacation_mode", "heating_curve", *SLOT_KEYS):
        assert catalog.BY_KEY[key].cls == "C"


def _st(eid, state="1"):
    return {"entity_id": eid, "state": state}


def test_resolve_by_suffix_with_and_without_area_prefix():
    states = [
        _st("number.heiko_heat_pump_dhw_setpoint"),
        _st("number.area_x_heiko_heat_pump_circulation_pump_p0_run_time"),        # prefiks obszaru
        _st("number.heiko_heat_pump_anti_legionella_setpoint"),                    # anty-legionella poza katalogiem: nie udaje dhw_setpoint
        _st("select.heiko_heat_pump_working_mode"),
        _st("sensor.heiko_heat_pump_working_mode_2"),                              # inny sensor
        _st("sensor.heiko_heat_pump_working_mode"),
        _st("switch.area_x_heiko_heat_pump_vacation_mode"),
        _st("binary_sensor.area_x_heiko_heat_pump_vacation_mode_active"),          # nie przełącznik
        _st("number.other_pump_dhw_setpoint"),                                     # cudza pompa
    ]
    found = catalog.resolve_params(states)
    assert found["dhw_setpoint"] == "number.heiko_heat_pump_dhw_setpoint"
    assert found["p0_run_time"] == "number.area_x_heiko_heat_pump_circulation_pump_p0_run_time"
    assert not any(k.startswith("anti_leg") for k in found) and "number.heiko_heat_pump_anti_legionella_setpoint" not in found.values()
    assert found["working_mode"] == "select.heiko_heat_pump_working_mode"
    assert found["vacation_mode"] == "switch.area_x_heiko_heat_pump_vacation_mode"
    tel = catalog.resolve_telemetry(states)
    assert tel["working_mode"] == "sensor.heiko_heat_pump_working_mode"
    assert "dhw_setpoint" not in tel


def test_to_number():
    assert catalog.to_number("21.5") == 21.5
    assert catalog.to_number("on") == 1.0 and catalog.to_number("off") == 0.0
    assert catalog.to_number("unavailable") is None and catalog.to_number(None) is None


def test_catalog_module_contains_no_full_entity_ids():
    """Repo jest publiczne: katalog zna tylko sufiksy, pełne entity_id domu rozwiązuje się w czasie pracy."""
    import re
    assert re.findall(r"\b(?:sensor|number|select|switch|binary_sensor)\.\w+", inspect.getsource(catalog)) == []


def test_backup_heater_is_the_canonical_slot_50_select_not_the_reversed_alias_switch():
    """Rejestr idx 50 = menu 9.4 (priorytet HWTBH vs AH). Katalog ma kanoniczny select; alias switch (ON = „Niższe”)
    nie wchodzi, żeby jedna zmiana dawała jeden wpis w dzienniku. Opis jest neutralny: skutek ON niezmierzony."""
    p = catalog.BY_KEY["backup_heater"]
    assert (p.domain, p.suffix, p.cls) == ("select", "backup_priority_dhw", "C")
    assert "AH" in p.label and "HWTBH" in p.label and "9.4" in p.note
    assert "od razu" not in p.note and "niezmierzony" in p.note
    assert "ah_working_time" in catalog.TELEMETRY_SENSORS


def test_slot_entities_resolve_with_area_prefix_and_alias_switch_is_ignored():
    states = [
        _st("switch.obszar_heiko_heat_pump_backup_source_for_heating", "off"),
        _st("select.obszar_heiko_heat_pump_backup_priority_heating", "Niższe dla grzałki wewnętrznej AH"),
        _st("switch.obszar_heiko_heat_pump_backup_source_for_dhw", "on"),
        _st("select.obszar_heiko_heat_pump_backup_priority_dhw", "Wyższe dla grzałki wewnętrznej AH"),
        _st("switch.heiko_heat_pump_backup_heater_hbh", "off"),                                 # alias slotu 50
        _st("number.obszar_heiko_heat_pump_backup_source_start_dependency", "100"),
        _st("number.obszar_heiko_heat_pump_backup_source_start_delay", "20"),
        _st("switch.obszar_heiko_heat_pump_reduced_setpoint", "off"),
        _st("number.obszar_heiko_heat_pump_reduced_setpoint_drop_rise", "2"),
    ]
    found = catalog.resolve_params(states)
    assert set(SLOT_KEYS) <= set(found) and len(found) == len(SLOT_KEYS)
    assert found["backup_heater"] == "select.obszar_heiko_heat_pump_backup_priority_dhw"
    assert found["reduced_setpoint"] == "switch.obszar_heiko_heat_pump_reduced_setpoint"           # nie number …_drop_rise
    assert found["reduced_setpoint_drop"] == "number.obszar_heiko_heat_pump_reduced_setpoint_drop_rise"
    assert "switch.heiko_heat_pump_backup_heater_hbh" not in found.values()


def test_catalog_has_34_params_and_no_anti_legionella():
    assert len(catalog.CATALOG) == 34
    assert not [p for p in catalog.CATALOG if "anti" in p.key or "legionella" in p.suffix]
