"""Dzień roboczy bez wiedzy o świętach — domyślnik, gdy nie ma `binary_sensor.workday`."""
from __future__ import annotations


def default_is_workday(d) -> bool:
    """Dzień roboczy bez wiedzy o świętach (pn–pt) — domyślnik, gdy nie ma `binary_sensor.workday`."""
    return d.weekday() < 5
