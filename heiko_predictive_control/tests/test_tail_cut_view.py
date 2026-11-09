"""Ogon grzania (S7): widok dla kafla na pulpicie — tylko odczyt stanu `tail_cut_state`."""
from datetime import datetime, timedelta

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import tail_cut, tail_cut_view

T0 = datetime(2026, 11, 9, 12, 0)
ON = {"tail_cut_enabled": True, "heiko_enabled": True}


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def iso(delta: timedelta) -> str:
    return (T0 + delta).isoformat(timespec="seconds")


def put(conn, **state):
    dbm.set_setting(conn, tail_cut.STATE_KEY, state)


def test_wylaczony_domyslne_wartosci_opcji(conn):
    v = tail_cut_view.build(conn, {}, T0)
    assert v["status"] == "off" and v["status_label"]
    assert (v["margin_k"], v["delay_min"], v["hold_min"], v["min_room_c"], v["pause_min"]) == (0, 120, 2, 20.5, 30)
    assert v["tail_minutes"] is None and v["last_cut"] is None and v["last"] is None
    assert (v["cuts_today"], v["cuts_7d"], v["cuts_total"]) == (0, 0, 0)


def test_pusta_opcja_daje_wartosc_domyslna(conn):
    v = tail_cut_view.build(conn, {"tail_margin_k": None, "tail_delay_min": None}, T0)
    assert (v["margin_k"], v["delay_min"]) == (0, 120)


def test_zablokowany_gdy_sterowanie_heiko_wylaczone(conn):
    v = tail_cut_view.build(conn, {"tail_cut_enabled": True, "heiko_enabled": False}, T0)
    assert v["status"] == "blocked"


def test_czuwa_bez_stanu(conn):
    assert tail_cut_view.build(conn, ON, T0)["status"] == "watching"


def test_trzyma_gdy_forced(conn):
    put(conn, forced=True, cut_at=iso(timedelta(minutes=-1)), cuts=[iso(timedelta(minutes=-1))])
    v = tail_cut_view.build(conn, ON, T0)
    assert v["status"] == "holding" and v["cuts_today"] == 1


def test_przerwa_do_konca_pause_until(conn):
    put(conn, pause_until=iso(timedelta(minutes=10)))
    v = tail_cut_view.build(conn, ON, T0)
    assert v["status"] == "pause" and v["pause_until"] == iso(timedelta(minutes=10))
    assert tail_cut_view.build(conn, ON, T0 + timedelta(minutes=10))["status"] == "watching"


def test_wylaczona_opcja_ma_pierwszenstwo_przed_stanem(conn):
    put(conn, forced=True)
    assert tail_cut_view.build(conn, {"tail_cut_enabled": False, "heiko_enabled": True}, T0)["status"] == "off"


def test_tail_minutes_od_tail_since(conn):
    put(conn, tail_since=iso(timedelta(minutes=-12)))
    assert tail_cut_view.build(conn, ON, T0)["tail_minutes"] == 12


def test_liczniki_cięc_dzis_7_dni_lacznie(conn):
    cuts = [                                       # chronologicznie, jak zapisuje tail_cut
        "2026-10-01T10:00:00",                     # dawno: tylko łącznie
        "2026-11-02T20:00:00",                     # 7 dni temu: poza 7 dniami
        "2026-11-05T20:00:00",                     # 4 dni temu: w 7 dniach
        iso(timedelta(hours=-4)),                  # dziś 08:00
        iso(timedelta(hours=-2, minutes=-30)),     # dziś 09:30
    ]
    put(conn, cuts=cuts, last={"ts": cuts[-1], "reason": "odcięto ogon"})
    v = tail_cut_view.build(conn, ON, T0)
    assert (v["cuts_today"], v["cuts_7d"], v["cuts_total"]) == (2, 3, 5)
    assert v["last_cut"] == "2026-11-09T09:30:00"
    assert v["last"] == {"ts": cuts[-1], "reason": "odcięto ogon"}
