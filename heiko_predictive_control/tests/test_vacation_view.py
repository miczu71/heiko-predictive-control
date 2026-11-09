"""Tryb urlopowy (S7): widok dla pulpitu — status, etykiety, podsumowanie, ostrzeżenie CWU, bezpiecznik 30 dni."""
from datetime import date, datetime

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import vacation_exec, vacation_view

NOW = datetime(2026, 11, 9, 12, 0)
BASE = {"vacation_enabled": True, "heiko_enabled": True, "vacation_from": "2026-11-01", "vacation_to": "2026-11-15"}


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def build(conn, settings, state=None):
    if state is not None:
        dbm.set_setting(conn, vacation_exec.STATE_KEY, state)
    return vacation_view.build(conn, settings, NOW)


def test_off(conn):
    v = build(conn, {})
    assert (v["status"], v["status_label"], v["enabled"], v["summary"]) == ("off", "wyłączony", False, "")


def test_scheduled(conn):
    v = build(conn, {**BASE, "vacation_from": "2026-11-12"})
    assert v["status"] == "scheduled" and v["status_label"] == "zaplanowany od 12.11"


def test_expired(conn):
    v = build(conn, {**BASE, "vacation_to": "2026-11-08"})
    assert v["status"] == "expired" and v["status_label"] == "zakończony"


def test_blocked(conn):
    v = build(conn, {**BASE, "heiko_enabled": False})
    assert v["status"] == "blocked" and v["status_label"] == "wstrzymany: sterowanie Heiko wyłączone"


def test_tripped_with_room(conn):
    v = build(conn, BASE, {"tripped": True, "trip_room": {"name": "Salon", "c": 15.0}, "suspended_day": "2026-11-09"})
    assert v["status"] == "tripped" and v["status_label"] == "bezpiecznik — bazowe do ocieplenia"
    assert v["trip_room"] == {"name": "Salon", "c": 15.0}


def test_suspended(conn):
    v = build(conn, BASE, {"suspended_day": "2026-11-09"})
    assert v["status"] == "suspended" and v["status_label"] == "wstrzymany do jutra" and v["trip_room"] is None
    assert build(conn, BASE, {"suspended_day": "2026-11-08"})["status"] == "active"


def test_active_fields_and_defaults(conn):
    v = build(conn, BASE)
    assert v["status"] == "active" and v["status_label"] == "włączony"
    assert (v["enabled"], v["from"], v["to"]) == (True, "2026-11-01", "2026-11-15")
    assert (v["drop"], v["min_room_c"], v["dhw"]) == (2, 16.0, 20)
    assert v["with_away"] is False and v["peak_pressing"] is False
    assert v["summary"] == "Urlop do 15.11 · krzywa −2 · CWU 20"


def test_summary_without_end_date_and_custom_values(conn):
    v = build(conn, {**BASE, "vacation_to": "", "vacation_curve_drop": 3, "vacation_dhw_setpoint": 25,
                     "vacation_min_room_c": 18, "vacation_with_away": True, "g13_peak_enabled": True})
    assert v["summary"] == "Urlop · krzywa −3 · CWU 25" and v["to"] is None
    assert (v["drop"], v["min_room_c"], v["dhw"], v["with_away"], v["peak_pressing"]) == (3, 18.0, 25, True, True)


def test_dhw_warn_and_trips_30d(conn):
    v = build(conn, BASE, {"dhw_warn": "min", "trips": ["2026-10-05", "2026-10-11", "2026-11-01", "2026-11-09"]})
    assert v["dhw_warn"] == "min" and v["trips_30d"] == 3
    assert build(conn, BASE, {})["dhw_warn"] is None
