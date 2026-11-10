"""E1: walidator definicji testu, rejestr nastaw i opcji (czyste funkcje, bez I/O)."""
import copy

import pytest

from heiko_predictive_control import experiments as ex
from heiko_predictive_control import web
from heiko_predictive_control.testkit import params
from heiko_predictive_control.testkit.params import MEASURES, OVERRIDABLE, PUMP_PARAMS
from heiko_predictive_control.testkit.schema import validate

PREHEAT_AB = {
    "key": "preheat_ab",
    "title": "Preheat A/B",
    "design": "alternate",
    "order": ["B", "A"],
    "arms": {"A": {"overrides": {"g13_peak_preheat_shift": 1}},
             "B": {"overrides": {"g13_peak_preheat_shift": 0}}},
    "days": {"filter": "peak_day", "count": 20},
    "coexist": ["g13_peak"],
    "measures": [
        {"name": "floor_cost", "window": "13:00→13:00", "per": "hdd"},
        {"name": "min_room_at", "at": "21:00"},
        "safety_trips",
        "kwh_day",
    ],
    "verdict": {"primary": "floor_cost", "better": "lower",
                "guards": [{"measure": "min_room_at", "op": ">=", "value": 20.5}],
                "weather_band_c": 3, "min_n": 6},
}


def copy_def(**changes):
    d = copy.deepcopy(PREHEAT_AB)
    d.update(changes)
    return d


def has_error(d, *parts):
    errors = validate(d)
    assert errors, "oczekiwano błędu walidacji"
    assert any(all(p in msg for p in parts) for msg in errors), errors


def test_preheat_fixture_is_valid():
    assert validate(PREHEAT_AB) == []


# Reguła 1: pola wymagane i ich zakresy
@pytest.mark.parametrize("field", ["key", "title", "design", "arms", "days"])
def test_missing_required_field(field):
    d = copy.deepcopy(PREHEAT_AB)
    del d[field]
    has_error(d, field)


def test_key_format():
    has_error(copy_def(key="Bad-Key"), "key")


def test_title_length():
    has_error(copy_def(title="x" * 81), "title")


def test_design_value():
    has_error(copy_def(design="random"), "design")


def test_days_count_and_filter():
    has_error(copy_def(days={"filter": "peak_day", "count": 61}), "days.count")
    has_error(copy_def(days={"filter": "weekend", "count": 5}), "days.filter")


def test_arms_count_and_key_shape():
    arms = {k: {"overrides": {"tail_margin_k": 1}} for k in "ABCDE"}
    has_error(copy_def(arms=arms, order=list("ABCDE")), "arms")
    has_error(copy_def(arms={"a": {"overrides": {}}, "B": {"overrides": {}}}, order=["a", "B"]), "arms")


# Reguła 2: nieznane pola na każdym poziomie
def test_unknown_top_level_field():
    has_error(copy_def(foo=1), "foo")


def test_unknown_field_in_arm():
    d = copy_def()
    d["arms"]["A"]["bogus"] = 1
    has_error(d, "arms.A.bogus")


def test_unknown_requires_field():
    has_error(copy_def(requires={"curve_on": True, "xyz": 1}), "requires.xyz")


# Reguła 3: overrides i writes
def test_override_unknown_option_rejected():
    d = copy_def(coexist=[])
    d["arms"]["A"]["overrides"]["g13_peak_enabled"] = True
    has_error(d, "arms.A.overrides.g13_peak_enabled", "nieznana opcja")


def test_override_out_of_range():
    d = copy_def()
    d["arms"]["A"]["overrides"]["g13_peak_preheat_shift"] = 5
    has_error(d, "arms.A.overrides.g13_peak_preheat_shift")


def test_override_bool_is_not_number():
    d = copy_def()
    d["arms"]["A"]["overrides"]["g13_peak_preheat_shift"] = True
    has_error(d, "arms.A.overrides.g13_peak_preheat_shift")


def test_writes_unknown_param():
    d = copy_def(coexist=[], restore=True)
    d["arms"]["A"]["writes"] = [{"param": "heating_pump_power", "value": 1}]
    has_error(d, "arms.A.writes", "heating_pump_power")


def test_writes_absolute_value_outside_rule():
    d = copy_def(coexist=[])
    d["arms"]["A"]["writes"] = [{"param": "curve_shift", "value": 4}]
    has_error(d, "arms.A.writes")


def test_writes_valid_relative_value_and_window():
    d = copy_def(coexist=[])
    d["arms"]["A"]["writes"] = [{"param": "curve_shift", "value": "base+1", "from": "13:00", "to": "16:00"}]
    d["arms"]["B"]["writes"] = [{"param": "dhw", "value": 45, "from": "zone:tanio"}]
    assert validate(d) == []


def test_writes_bad_relative_value_and_time():
    d = copy_def(coexist=[])
    d["arms"]["A"]["writes"] = [{"param": "curve_shift", "value": "plus1"}]
    has_error(d, "arms.A.writes")
    d["arms"]["A"]["writes"] = [{"param": "curve_shift", "value": 1, "from": "25:00"}]
    has_error(d, "arms.A.writes")


# Reguła 4: design a arms/order
def test_single_needs_exactly_one_arm():
    has_error(copy_def(design="single"), "arms", "single")


def test_alternate_needs_two_arms_and_matching_order():
    has_error(copy_def(order=["B", "C"]), "order")
    one = {"A": {"overrides": {"tail_margin_k": 1}}}
    has_error(copy_def(arms=one, order=["A"]), "arms", "alternate")


# Reguła 5: coexist i writes
def test_writes_with_coexist_g13_peak_rejected():
    d = copy_def(coexist=["g13_peak"])
    d["arms"]["A"]["writes"] = [{"param": "curve_shift", "value": 1}]
    has_error(d, "coexist")


def test_coexist_only_g13_peak_allowed():
    has_error(copy_def(coexist=["vacation"]), "coexist")


# Reguła 6: measures i verdict
def test_unknown_measure():
    has_error(copy_def(measures=["floor_cost", "bogus"]), "measures", "bogus")


def test_measure_missing_required_param():
    d = copy_def(measures=[{"name": "floor_cost"}, {"name": "min_room_at", "at": "21:00"}])
    has_error(d, "measures", "floor_cost", "window")
    d = copy_def(measures=["floor_cost", "min_room_at"])
    has_error(d, "measures", "min_room_at")


def test_verdict_primary_must_be_in_measures():
    d = copy_def(measures=["kwh_day"])
    has_error(d, "verdict.primary")


def test_verdict_better_value():
    d = copy_def()
    d["verdict"]["better"] = "bigger"
    has_error(d, "verdict.better")


def test_verdict_weather_band_and_min_n_limits():
    d = copy_def()
    d["verdict"]["weather_band_c"] = 11
    has_error(d, "verdict.weather_band_c")
    d = copy_def()
    d["verdict"]["min_n"] = 0
    has_error(d, "verdict.min_n")


def test_guard_measure_must_be_in_list():
    d = copy_def()
    d["verdict"]["guards"] = [{"measure": "inertia_refit", "op": ">=", "value": 1}]
    has_error(d, "verdict.guards")


# Reguła 7: restore
def test_restore_false_with_writes_rejected():
    d = copy_def(coexist=[], restore=False)
    d["arms"]["A"]["writes"] = [{"param": "curve_shift", "value": 1}]
    has_error(d, "restore")


def test_restore_false_without_writes_allowed():
    assert validate(copy_def(restore=False)) == []


# Reguła 8: limity rozmiaru i głębokości
def test_json_too_big():
    has_error(copy_def(description="x" * 17000), "16 KB")


def test_too_deep():
    d = copy_def(extra={"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}})
    has_error(d, "głęb")


# Wejście złego typu: błąd, bez wyjątku
@pytest.mark.parametrize("bad", [None, [], "x", 5, True, [PREHEAT_AB]])
def test_non_dict_input_returns_error(bad):
    errors = validate(bad)
    assert errors and isinstance(errors, list)


def test_non_json_value_returns_error():
    errors = validate(copy_def(title={1, 2}))
    assert errors


def test_requires_bad_types_do_not_raise():
    assert validate(copy_def(requires={"after": [["x"]], "curve_on": "yes"}))


# Rejestr nastaw: wiązanie z experiments i web
def test_curve_shift_check_is_write_rule():
    assert PUMP_PARAMS["curve_shift"].check is ex._WRITE_RULES[ex.ALLOWED_ENTITY_SUFFIX]
    assert PUMP_PARAMS["curve_shift"].check(0, 1) is True
    assert PUMP_PARAMS["curve_shift"].check(0, 4) is False


def test_pump_params_suffixes_match_experiments():
    assert PUMP_PARAMS["curve_shift"].suffix == ex.ALLOWED_ENTITY_SUFFIX
    assert PUMP_PARAMS["p0_run"].suffix == ex.P0_RUN_SUFFIX
    assert PUMP_PARAMS["p0_stop"].suffix == ex.P0_STOP_SUFFIX
    assert PUMP_PARAMS["slot52"].suffix == ex.SLOT52_SUFFIX
    assert PUMP_PARAMS["dhw"].suffix == ex.DHW_SUFFIX


def test_overridable_ranges_match_web_for_shared_keys():
    shared = [k for k in OVERRIDABLE if k in web.SETTING_RANGES]
    assert shared, "brak wspólnych kluczy do porównania"
    for key in shared:
        assert OVERRIDABLE[key] == web.SETTING_RANGES[key], key


def test_overridable_excludes_forbidden_options():
    for key in OVERRIDABLE:
        assert not key.endswith("_enabled"), key
        assert not key.startswith(("vacation_", "advisor_")), key
    assert "pv_roi_url" not in OVERRIDABLE
    assert "g13_peak_min_room_c" in OVERRIDABLE


def test_measures_names():
    assert MEASURES == {"floor_cost", "kwh_day", "min_room_at", "safety_trips", "zone_spread",
                        "excitation", "inertia_refit", "writes_confirmed"}
    assert params.MEASURE_PARAMS["floor_cost"]["window"] is True
