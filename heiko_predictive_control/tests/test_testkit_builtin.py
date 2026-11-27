"""E2: wbudowane definicje (zapis, bezwladnosc, p0_ab) — zgodne ze schematem, JSON i z experiments.KINDS."""
import json

import pytest

from heiko_predictive_control import experiments as ex
from heiko_predictive_control.testkit.builtin import BUILTIN
from heiko_predictive_control.testkit.schema import validate


def test_builtin_keys_are_the_legacy_kinds():
    # E1: `ogon_ab` jest wbudowany poza legacy `ex.KINDS` (własny tytuł i opis)
    assert set(BUILTIN) - {"ogon_ab"} == set(ex.KINDS) == {"zapis", "bezwladnosc", "p0_ab"}


@pytest.mark.parametrize("key", sorted(BUILTIN))
def test_builtin_definition_is_valid_schema(key):
    assert validate(BUILTIN[key]) == []


@pytest.mark.parametrize("key", sorted(BUILTIN))
def test_builtin_definition_is_json_serializable(key):
    assert json.loads(json.dumps(BUILTIN[key])) == BUILTIN[key]


@pytest.mark.parametrize("key", sorted(set(BUILTIN) & set(ex.KINDS)))
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


# E1: definicja „Ogon grzania A/B” (tytuł i opis w miejscu, spoza experiments.KINDS)
def test_ogon_ab_validates_and_is_serializable():
    d = BUILTIN["ogon_ab"]
    assert validate(d) == []
    assert json.loads(json.dumps(d)) == d


def test_ogon_ab_shape():
    d = BUILTIN["ogon_ab"]
    assert d["requires"] == {"heiko_enabled": True}
    assert d["coexist"] == ["g13_peak"]
    assert d["days"] == {"filter": "all", "count": 14}
    assert d["design"] == "alternate" and d["order"] == ["A", "B"]
    assert d["arms"]["A"]["overrides"] == {"tail_cut_enabled": True}
    assert d["arms"]["B"]["overrides"] == {"tail_cut_enabled": False}
    assert d["measures"] == ["kwh_hdd", "kwh_day", {"name": "floor_cost", "window": "00:00→00:00"},
                             "room_day", "tail_cuts", "safety_trips"]
    assert d["verdict"] == {"primary": "kwh_hdd", "better": "lower", "min_gain": 0.08,
                            "weather_band_c": 3.0, "min_n": 5}


def test_ogon_ab_schedule_alternates_a_b_over_14_days():
    from datetime import datetime

    from heiko_predictive_control.testkit import engine

    now = datetime(2026, 12, 1, 0, 0)
    steps, arms = engine.plan(BUILTIN["ogon_ab"], {}, now, lambda d: True, None)
    days = sorted(arms)
    assert len(days) == 14
    assert [arms[d] for d in days] == ["A", "B"] * 7
    assert days[0] == "2026-12-01"


def test_ogon_ab_started_during_day_skips_partial_start_day():
    from datetime import datetime

    from heiko_predictive_control.testkit import engine

    _, arms = engine.plan(BUILTIN["ogon_ab"], {}, datetime(2026, 12, 1, 10, 0), lambda d: True, None)
    assert min(arms) == "2026-12-02" and len(arms) == 14
