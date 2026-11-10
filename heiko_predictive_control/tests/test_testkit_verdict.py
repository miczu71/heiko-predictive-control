"""E3: `verdict.decide` — werdykt generyczny (czysta funkcja)."""
from heiko_predictive_control.testkit.verdict import decide


def arm(value, outdoor=5.0, n=6, **measures):
    return {"value": value, "outdoor_c": outdoor, "n": n, "measures": {k: {"value": v} for k, v in measures.items()}}


def test_lower_wins_with_smaller_value():
    r = decide({"primary": "m", "better": "lower"}, {"A": arm(2.0), "B": arm(1.0)})
    assert (r["winner"], r["gain"], r["confidence"], r["outdoor_c"]) == ("B", 1.0, "wysoka", 5.0)


def test_higher_wins_with_bigger_value():
    r = decide({"primary": "m", "better": "higher"}, {"A": arm(2.0), "B": arm(1.0)})
    assert r["winner"] == "A" and r["gain"] == 1.0


def test_gain_below_min_gain_has_no_winner():
    r = decide({"primary": "m", "better": "lower", "min_gain": 0.3}, {"A": arm(1.0), "B": arm(0.8)})
    assert r["winner"] is None and r["gain"] == 0.2 and r["reason"]
    assert decide({"primary": "m", "better": "lower", "min_gain": 0.3}, {"A": arm(1.0), "B": arm(0.7)})["winner"] == "B"


def test_tie_has_no_winner():
    assert decide({"primary": "m", "better": "lower"}, {"A": arm(1.0), "B": arm(1.0)})["winner"] is None


def test_guard_violated_by_winner_cancels_win():
    spec = {"primary": "m", "better": "lower", "guards": [{"measure": "room", "op": ">=", "value": 20.5}]}
    bad = decide(spec, {"A": arm(2.0, room=21.0), "B": arm(1.0, room=19.0)})
    assert bad["winner"] is None and "room" in bad["reason"]
    good = decide(spec, {"A": arm(2.0, room=19.0), "B": arm(1.0, room=21.0)})       # strażnik liczy się tylko zwycięzcy
    assert good["winner"] == "B"


def test_guard_le_operator():
    spec = {"primary": "m", "better": "higher", "guards": [{"measure": "trips", "op": "<=", "value": 0}]}
    assert decide(spec, {"A": arm(2.0, trips=1), "B": arm(1.0, trips=0)})["winner"] is None
    assert decide(spec, {"A": arm(2.0, trips=0), "B": arm(1.0, trips=5)})["winner"] == "A"


def test_guard_without_value_is_unmet():
    spec = {"primary": "m", "better": "lower", "guards": [{"measure": "room", "op": ">=", "value": 20.5}]}
    assert decide(spec, {"A": arm(2.0), "B": arm(1.0, room=None)})["winner"] is None
    assert decide(spec, {"A": arm(2.0), "B": arm(1.0)})["winner"] is None


def test_low_confidence_on_small_n_missing_or_distant_weather():
    spec = {"primary": "m", "better": "lower", "min_n": 6, "weather_band_c": 3}
    assert decide(spec, {"A": arm(2.0, n=5), "B": arm(1.0)})["confidence"] == "niska"
    assert decide(spec, {"A": arm(2.0, outdoor=None), "B": arm(1.0)})["confidence"] == "niska"
    far = decide(spec, {"A": arm(2.0, outdoor=2.0), "B": arm(1.0, outdoor=5.5)})
    assert far["confidence"] == "niska" and far["winner"] == "B"
    assert decide(spec, {"A": arm(2.0, outdoor=2.0), "B": arm(1.0, outdoor=5.0)})["confidence"] == "wysoka"
    assert decide(spec, {"A": arm(2.0, outdoor=2.0), "B": arm(1.0, outdoor=4.0)})["outdoor_c"] == 3.0


def test_not_two_arms_with_value_is_insufficient():
    spec = {"primary": "m", "better": "lower"}
    for per_arm in ({"A": arm(1.0)}, {"A": arm(1.0), "B": arm(None)}, {}, {"A": arm(1), "B": arm(2), "C": arm(3)}):
        r = decide(spec, per_arm)
        assert r["winner"] is None and r["reason"] == "za mało danych"
