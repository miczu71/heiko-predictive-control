"""Domyślnik dnia roboczego (pn–pt) — używany, gdy HA nie zna dnia (święta)."""
from datetime import date

from heiko_predictive_control.workday import default_is_workday


def test_monday_to_friday_are_workdays():
    assert all(default_is_workday(date(2026, 1, d)) for d in range(12, 17))     # pn–pt


def test_weekend_is_not_workday():
    assert not default_is_workday(date(2026, 1, 17)) and not default_is_workday(date(2026, 1, 18))
