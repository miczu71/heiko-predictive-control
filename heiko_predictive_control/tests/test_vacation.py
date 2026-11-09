"""Tryb urlopowy (czysta decyzja): obniżenie krzywej o `drop`, bezpiecznik zimnego pokoju z histerezą."""
from datetime import datetime, timedelta

from heiko_predictive_control import vacation as v

T0 = datetime(2026, 11, 9, 12, 0)
CFG = v.Config()


def dec(now=T0, *, cur=0.0, base=None, room=21.0, state=None, cfg=CFG):
    return v.decide(now, current_shift=cur, base_shift=base, min_room_c=room, state=state or v.State(), cfg=cfg)


def test_constants_and_defaults():
    assert v.SHIFT_LIMIT == 3.0 and v.BREAKER_HOLD == timedelta(minutes=15) and v.RESUME_MARGIN == 1.0
    assert v.Config() == v.Config(drop=2, min_room_c=16.0)


def test_base_from_current_and_drop():
    d = dec(cur=0.0)
    assert (d.target, d.reason, d.base, d.event) == (-2.0, "urlop", 0.0, None)


def test_remembered_base_wins_over_current():
    d = dec(cur=-2.0, base=1.0)
    assert d.base == 1.0 and d.target == -1.0


def test_clamped_to_minus_three():
    assert dec(cur=-2.0, base=-2.0, cfg=v.Config(drop=3)).target == -3.0


def test_drop_clamped_to_1_3():
    assert dec(cfg=v.Config(drop=0)).target == -1.0
    assert dec(cfg=v.Config(drop=7)).target == -3.0
    assert dec(cfg=v.Config(drop=-5)).target == -1.0


def test_breaker_trips_after_15_min_once():
    d = dec(T0, room=15.0)
    assert d.state.cold_since == T0 and d.target == -2.0 and d.event is None
    d = dec(T0 + timedelta(minutes=14), room=15.0, state=d.state)
    assert d.event is None and not d.state.tripped
    d = dec(T0 + timedelta(minutes=15), room=15.0, state=d.state)
    assert d.event == "bezpiecznik" and d.state.tripped and d.target == 0.0 and d.reason == "bezpiecznik"
    d = dec(T0 + timedelta(minutes=20), room=15.0, state=d.state)
    assert d.event is None and d.state.tripped and d.target == 0.0


def test_missing_reading_keeps_state():
    s = v.State(cold_since=T0)
    d = dec(T0 + timedelta(hours=1), room=None, state=s)
    assert d.state == s and d.event is None and d.target == -2.0
    s = v.State(cold_since=T0, tripped=True)
    assert dec(T0 + timedelta(hours=1), room=None, state=s).state == s


def test_warm_reading_resets_counter():
    d = dec(T0, room=15.0)
    d = dec(T0 + timedelta(minutes=10), room=16.0, state=d.state)
    assert d.state.cold_since is None


def test_hysteresis_resume():
    s = v.State(cold_since=T0, tripped=True)
    d = dec(T0 + timedelta(hours=1), room=16.5, state=s)       # >= próg, < próg+1: nadal bezpiecznik
    assert d.state.tripped and d.state.cold_since is None and d.target == 0.0
    d = dec(T0 + timedelta(hours=1, minutes=5), room=17.0, state=d.state)
    assert not d.state.tripped and d.target == -2.0 and d.reason == "urlop"
