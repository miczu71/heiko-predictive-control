"""E2: `testkit.schedule.build` i złote testy zgodności z `experiments.build_schedule` (czyste, bez I/O)."""
import copy
import dataclasses
from datetime import date, datetime, time, timedelta

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import tariff_feed as tf
from heiko_predictive_control.testkit import schedule as sc
from heiko_predictive_control.testkit.builtin import BUILTIN
from heiko_predictive_control.testkit.schema import validate

T0 = datetime(2026, 10, 7, 10, 0)           # środa, jak w test_experiments.py
ENTITY = "sensor.pv_roi_tracker_pv_tariff_zone"
NOW = datetime(2026, 1, 14, 10, 7)          # środa, jak w test_tariff_feed.py


def workday(d: date) -> bool:
    return d.weekday() < 5


def legacy(plan):
    """Adapter: kroki planu → format `experiments.build_schedule` (krzywa: at/value; P0: at/value=run/stop)."""
    out = []
    for s in plan.steps:
        if s["param"] in ("curve_shift", "p0_run"):
            out.append({"at": s["at"], "value": s["value"]})
        elif s["param"] == "p0_stop":
            assert out[-1]["at"] == s["at"]
            out[-1]["stop"] = s["value"]
        else:
            raise AssertionError(s)
    return out


def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def schedule(start, hours=48, peak=lambda dt: 6 <= dt.hour < 13 or 15 <= dt.hour < 22, tariff="G12w"):
    out = []
    for i in range(hours):
        dt = start + timedelta(hours=i)
        p = peak(dt)
        out.append({"hour": dt.strftime("%Y-%m-%dT%H"), "tariff": tariff, "zone": "peak" if p else "offpeak",
                    "is_peak": p, "level": "expensive" if p else "cheap", "gross": 1.5 if p else 0.5,
                    "net": 1.0, "confidence": "invoice"})
    return out


def state(items, st="peak"):
    return {"state": st, "attributes": {"schedule": items}}


class FakeHA:
    def __init__(self, data=None):
        self.data = data

    def get_state(self, entity):
        return self.data


def feed(c, data):
    return tf.TariffFeed(FakeHA(data), c, ENTITY)


def baseline_curve(v):
    return {"curve_shift": v}


# --- Złote testy: wejścia identyczne jak w test_experiments.py / test_tariff_feed.py ---

def test_zapis_matches_legacy_for_base_0_and_3():
    for base in (0.0, 3.0):
        plan = sc.build(BUILTIN["zapis"], baseline_curve(base), T0, workday)
        assert legacy(plan) == ex.build_schedule("zapis", base, T0, workday)
        assert plan.arms_by_day == {"2026-10-07": "A"}
    assert [s["value"] for s in sc.build(BUILTIN["zapis"], baseline_curve(3.0), T0, workday).steps] == [2.0, 3.0]


def test_bezwladnosc_without_feed_matches_legacy_three_workdays():
    now = datetime(2026, 10, 8, 7, 0)       # czwartek, okno 6–12 już trwa
    for base in (0.0, -2.0):
        plan = sc.build(BUILTIN["bezwladnosc"], baseline_curve(base), now, workday)
        assert legacy(plan) == ex.build_schedule("bezwladnosc", base, now, workday)
    plan = sc.build(BUILTIN["bezwladnosc"], baseline_curve(0.0), now, workday)
    assert plan.arms_by_day == {"2026-10-08": "A", "2026-10-09": "A", "2026-10-12": "A"}
    assert len(plan.steps) == 2 * (1 + 2 + 2)


def test_bezwladnosc_with_feed_matches_legacy_and_default_days():
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 0), peak=lambda dt: 19 <= dt.hour < 22 or 7 <= dt.hour < 9)))
    assert f.refresh(NOW)
    now = datetime(2026, 1, 14, 5)
    plan = sc.build(BUILTIN["bezwladnosc"], baseline_curve(0.0), now, lambda d: True, f)
    assert legacy(plan) == ex.build_schedule("bezwladnosc", 0.0, now, lambda d: True, f)
    assert [s["at"][11:16] for s in plan.steps[:4]] == ["07:00", "08:00", "19:00", "21:00"]
    now2 = datetime(2026, 1, 16, 5)
    plan2 = sc.build(BUILTIN["bezwladnosc"], baseline_curve(0.0), now2, lambda d: d.weekday() < 5, f)
    assert legacy(plan2) == ex.build_schedule("bezwladnosc", 0.0, now2, lambda d: d.weekday() < 5, f)
    assert [s["at"][11:16] for s in plan2.steps[:4]] == ["06:00", "12:00", "15:00", "21:00"]   # feed nie zna doby


def test_p0_matches_legacy_for_both_pairs_and_empty_for_foreign_baseline():
    for pair in ((1.0, 6.0), (5.0, 5.0)):
        base = {"p0_run": pair[0], "p0_stop": pair[1]}
        plan = sc.build(BUILTIN["p0_ab"], base, T0, workday)
        assert legacy(plan) == ex.build_schedule("p0_ab", pair, T0, workday)
        assert ex.validate(legacy(plan), pair) is None
        assert list(plan.arms_by_day.items()) == [
            (f"2026-10-{d:02d}", arm) for d, arm in zip(range(8, 14), "ABABAB")]
    plan = sc.build(BUILTIN["p0_ab"], {"p0_run": 3.0, "p0_stop": 6.0}, T0, workday)
    assert plan.steps == [] and plan.arms_by_day == {}
    assert ex.build_schedule("p0_ab", (3.0, 6.0), T0, workday) == []


def test_p0_swapped_baseline_first_day_is_base_then_other():
    plan = sc.build(BUILTIN["p0_ab"], {"p0_run": 5.0, "p0_stop": 5.0}, T0, workday)
    first = [(s["param"], s["value"]) for s in plan.steps if s["at"] == "2026-10-08T00:00"]
    assert first == [("p0_run", 5.0), ("p0_stop", 5.0)]              # A = baza
    second = [(s["param"], s["value"]) for s in plan.steps if s["at"] == "2026-10-09T00:00"]
    assert second == [("p0_run", 1.0), ("p0_stop", 6.0)]             # B = druga para


def test_plan_is_frozen_dataclass_with_sorted_steps():
    plan = sc.build(BUILTIN["p0_ab"], {"p0_run": 1.0, "p0_stop": 6.0}, T0, workday)
    assert dataclasses.is_dataclass(plan)
    assert [s["at"] for s in plan.steps] == sorted(s["at"] for s in plan.steps)
    try:
        plan.steps = []
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("Plan powinien być niezmienny")


def test_unresolvable_zone_or_missing_baseline_gives_empty_plan():
    d = copy.deepcopy(BUILTIN["zapis"])
    d["arms"]["A"]["writes"][0]["from"] = "zone:tanio"
    d["arms"]["A"]["writes"][0].pop("to")
    empty = sc.build(d, baseline_curve(0.0), T0, workday)
    assert empty.steps == [] and empty.arms_by_day == {}
    none = sc.build(BUILTIN["zapis"], {}, T0, workday)            # brak bazy dla `base~1`
    assert none.steps == [] and none.arms_by_day == {}


# --- preheat_ab: definicja z roadmapy E6 (same overrides), bez kodu ---

PREHEAT = {
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

HOLIDAY = date(2026, 11, 2)                  # poniedziałek bez szczytu w feedzie (święto)


def _preheat_peak(dt):
    return dt.weekday() < 5 and dt.date() != HOLIDAY and (6 <= dt.hour < 13 or 15 <= dt.hour < 22)


def test_preheat_ab_is_valid_and_has_no_steps_only_day_arms():
    assert validate(PREHEAT) == []
    c = conn()
    f = feed(c, state(schedule(T0.replace(minute=0), hours=24 * 45, peak=_preheat_peak)))
    assert f.refresh(T0)
    plan = sc.build(PREHEAT, {}, T0, workday, f)
    assert plan.steps == []
    days = []
    d = T0.date()
    while len(days) < 20:
        if _preheat_peak(datetime.combine(d, time(7))):          # dzień ze szczytem w feedzie
            days.append(d)
        d += timedelta(days=1)
    assert list(plan.arms_by_day) == [x.isoformat() for x in days]
    assert list(plan.arms_by_day.values()) == ["B", "A"] * 10
    assert plan.arms_by_day[days[0].isoformat()] == "B"
    assert not any(x.weekday() >= 5 or x == HOLIDAY for x in days)
