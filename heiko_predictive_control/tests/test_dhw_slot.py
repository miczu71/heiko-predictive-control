"""Analizator slotu 52 (opóźnienie grzałki AH) — funkcje czyste na syntetycznej migawce."""
from datetime import datetime, timedelta, timezone

import pytest

from heiko_predictive_control.analyzers import Snapshot, dhw_slot

NOW = datetime(2026, 1, 14, 7, 0, tzinfo=timezone.utc)


def cycle(days_ago=1, duration=40.0, outdoor=0.0, deficit=4.0):
    return {"start": (NOW - timedelta(days=days_ago, hours=1)).isoformat(), "duration_min": duration,
            "outdoor_c": outdoor, "deficit_c": deficit}


def winter(n=6, **kw):
    return [cycle(days_ago=1 + i, **kw) for i in range(n)]


def snap(cur=30.0, ah=None, cycles=None, **kw):
    slot = {"cycles": cycles if cycles is not None else [], "ah": ah if ah is not None else {}}
    base = dict(now=NOW, target_c=20.6, room_min_c=18.5, params={"backup_start_delay": cur}, dhw_slot=slot)
    base.update(kw)
    return Snapshot(**base)


def ah(price=0.30, evaluated=5):
    return {"pln_per_saved_min": price, "evaluated": evaluated}


def test_extends_when_ah_is_expensive_per_saved_minute():
    (d,) = dhw_slot.analyze(snap(ah=ah()))
    assert (d.analyzer, d.dedupe_key, d.kind, d.lens, d.param_key) == (
        "dhw_slot", "dhw_slot:backup_start_delay", "zmiana", "ekonomia", "backup_start_delay")
    assert (d.from_value, d.to_value, d.ttl_h, d.confidence, d.effects) == (30.0, 35.0, 72, "niska", {})
    assert d.evidence["pln_per_saved_min"] == 0.30 and d.evidence["ah_cycles"] == 5 and "0.30 zł" in d.reason


def test_shortens_in_winter_when_cycles_run_over_and_water_is_cold():
    (d,) = dhw_slot.analyze(snap(ah=ah(price=0.10), cycles=winter()))
    assert (d.from_value, d.to_value) == (30.0, 25.0)
    assert d.evidence["winter_cycles"] == 6 and d.evidence["median_deficit_c"] == 4.0 and "Zimą" in d.reason


def test_shortens_also_without_ah_measurement():
    (d,) = dhw_slot.analyze(snap(ah={"pln_per_saved_min": None, "evaluated": 0}, cycles=winter()))
    assert d.to_value == 25.0 and "brak pomiaru" in d.reason


@pytest.mark.parametrize("params", [{}, {"backup_start_delay": 32.0}, {"backup_start_delay": 15.0}, {"backup_start_delay": 65.0}])
def test_no_draft_without_value_off_step_or_out_of_range(params):
    assert dhw_slot.analyze(snap(ah=ah(), cycles=winter(), params=params)) == []


def test_no_draft_without_slot_data():
    assert dhw_slot.analyze(Snapshot(now=NOW, target_c=20.6, room_min_c=18.5, params={"backup_start_delay": 30.0})) == []


def test_extend_needs_enough_cycles_and_price_at_threshold():
    assert dhw_slot.analyze(snap(ah=ah(evaluated=4))) == []
    assert dhw_slot.analyze(snap(ah=ah(price=0.2499))) == []
    (d,) = dhw_slot.analyze(snap(ah=ah(price=0.25, evaluated=5)))                      # dokładnie na progu
    assert d.to_value == 35.0


def test_extend_beyond_max_gives_no_draft_and_no_shorten_either():
    assert dhw_slot.analyze(snap(cur=60.0, ah=ah(), cycles=winter(duration=70.0))) == []


def test_shorten_below_min_gives_no_draft():
    assert dhw_slot.analyze(snap(cur=20.0, ah=ah(price=0.1), cycles=winter())) == []


def test_extend_wins_over_shorten():
    (d,) = dhw_slot.analyze(snap(ah=ah(price=0.40), cycles=winter()))
    assert d.to_value == 35.0


def test_shorten_blocked_when_ah_price_is_at_or_above_threshold_but_too_few_cycles_for_extend():
    assert dhw_slot.analyze(snap(ah=ah(price=0.30, evaluated=4), cycles=winter())) == []


def test_shorten_winter_thresholds_exactly_at_the_limit():
    a = ah(price=0.1)
    assert dhw_slot.analyze(snap(ah=a, cycles=winter(n=5))) == []
    assert len(dhw_slot.analyze(snap(ah=a, cycles=winter(n=6)))) == 1                    # dokładnie 6
    assert dhw_slot.analyze(snap(ah=a, cycles=winter(deficit=2.99))) == []
    assert len(dhw_slot.analyze(snap(ah=a, cycles=winter(deficit=3.0)))) == 1            # mediana dokładnie na progu
    assert dhw_slot.analyze(snap(ah=a, cycles=winter(outdoor=5.0))) == []                # outdoor < 5 (ostro)
    assert len(dhw_slot.analyze(snap(ah=a, cycles=winter(outdoor=4.9)))) == 1
    assert dhw_slot.analyze(snap(ah=a, cycles=winter(duration=30.0))) == []             # 30 nie jest > 30
    assert len(dhw_slot.analyze(snap(ah=a, cycles=winter(duration=30.1)))) == 1


def test_shorten_over_share_boundary():
    a = ah(price=0.1)
    half = winter(n=3, duration=40.0) + winter(n=3, duration=20.0)                       # 3 z 6 = 50% (próg włącznie)
    assert len(dhw_slot.analyze(snap(ah=a, cycles=half))) == 1
    fewer = winter(n=2, duration=40.0) + winter(n=4, duration=30.0)                      # 30 nie jest > 30
    assert dhw_slot.analyze(snap(ah=a, cycles=fewer)) == []


def test_shorten_ignores_old_warm_and_incomplete_cycles_and_none_deficits():
    a = ah(price=0.1)
    noise = [cycle(days_ago=15, deficit=9.0), cycle(outdoor=10.0), cycle(duration=None), cycle(outdoor=None)]
    assert dhw_slot.analyze(snap(ah=a, cycles=winter(n=5) + noise)) == []
    (d,) = dhw_slot.analyze(snap(ah=a, cycles=winter(n=6) + [cycle(deficit=None)]))
    assert d.evidence["winter_cycles"] == 7 and d.evidence["median_deficit_c"] == 4.0
