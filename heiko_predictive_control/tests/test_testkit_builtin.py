"""E2: wbudowane definicje (zapis, bezwladnosc, p0_ab) — zgodne ze schematem, JSON i z experiments.KINDS."""
import json

import pytest

from heiko_predictive_control import experiments as ex
from heiko_predictive_control.testkit.builtin import BUILTIN
from heiko_predictive_control.testkit.schema import validate


def test_builtin_keys_are_the_legacy_kinds():
    assert set(BUILTIN) == set(ex.KINDS) == {"zapis", "bezwladnosc", "p0_ab"}


@pytest.mark.parametrize("key", sorted(BUILTIN))
def test_builtin_definition_is_valid_schema(key):
    assert validate(BUILTIN[key]) == []


@pytest.mark.parametrize("key", sorted(BUILTIN))
def test_builtin_definition_is_json_serializable(key):
    assert json.loads(json.dumps(BUILTIN[key])) == BUILTIN[key]


@pytest.mark.parametrize("key", sorted(BUILTIN))
def test_builtin_key_title_and_description_match_kinds(key):
    d = BUILTIN[key]
    assert d["key"] == key
    assert d["title"] == ex.KINDS[key].title
    assert d["description"] == ex.KINDS[key].description


def test_builtin_counts_and_windows_match_experiments_constants():
    assert BUILTIN["bezwladnosc"]["days"] == {"filter": "workday", "count": ex.INERTIA_DAYS}
    assert BUILTIN["bezwladnosc"]["min_window_h"] == ex.INERTIA_MIN_WINDOW_H
    assert BUILTIN["bezwladnosc"]["windows_fallback"] == [f"{a:02d}:00→{b:02d}:00" for a, b in ex.INERTIA_WINDOWS]
    assert BUILTIN["p0_ab"]["days"] == {"filter": "all", "count": ex.P0_DAYS}
    v = BUILTIN["p0_ab"]["verdict"]
    assert v["min_gain"] == ex.P0_MIN_GAIN_C and v["weather_band_c"] == ex.P0_WEATHER_BAND_C
    assert v["min_n"] == ex.P0_DAYS // 2


def test_builtin_p0_requires_bezwladnosc_first():
    assert BUILTIN["p0_ab"]["requires"] == {"curve_on": True, "heiko_enabled": True, "after": ["bezwladnosc"]}
