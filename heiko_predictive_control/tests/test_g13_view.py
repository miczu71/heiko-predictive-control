"""Tryb „Szczyt G13” (E4): widok dla pulpitu — stan, następna zmiana, energia strefy szczytu, bezpiecznik."""
import json
from datetime import date, datetime

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import g13_exec, g13_view
from tests.test_g13_exec import FRI, ON, SUMMER, at, feed


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def run(conn, start_iso, zones):
    conn.execute("INSERT INTO heating_runs (start, end, data) VALUES (?, ?, ?)",
                 (start_iso, start_iso, json.dumps({"start": start_iso, "zones": zones})))
    conn.commit()


def test_wylaczony_bez_nastepnej_zmiany(conn):
    feed(conn, at(10), True)
    v = g13_view.build(conn, {}, at(10))
    assert (v["status"], v["enabled"], v["next"]) == ("off", False, None)


def test_nastepna_zmiana_w_dzien_roboczy(conn):
    feed(conn, at(14), True)
    v = g13_view.build(conn, ON, at(14))
    assert v["status"] == "active" and v["next"] == {"at": "16:00", "value": "-3"}
    assert g13_view.build(conn, ON, at(22))["next"] is None


def test_teraz_faza_i_przesuniecie_z_ostatniego_zapisu(conn):
    feed(conn, at(14), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "base": 0.0, "day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(14))["now"] == {"phase": "przedgrzewanie do 16:00", "shift": "+1", "base": "0"}
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": -3.0, "base": 0.0, "day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(17))["now"] == {"phase": "szczyt do 21:00", "shift": "-3", "base": "0"}
    assert g13_view.build(conn, ON, at(22))["now"] is None
    assert g13_view.build(conn, ON, at(12))["now"] is None
    assert g13_view.build(conn, {}, at(14))["now"] is None


def test_now_view_tylko_gdy_aktywny_w_oknie_dnia_roboczego(conn):
    feed(conn, at(14), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "day": FRI.isoformat()})
    assert g13_view.now_view(conn, ON, at(14)) == {"phase": "przedgrzewanie do 16:00", "shift": "+1", "base": None}
    assert g13_view.now_view(conn, {}, at(14)) is None                          # wyłączony
    assert g13_view.now_view(conn, {**ON, "heiko_enabled": False}, at(14)) is None   # zablokowany
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "day": FRI.isoformat(), "suspended_day": at(14).date().isoformat()})
    assert g13_view.now_view(conn, ON, at(14)) is None                          # wstrzymany do jutra
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "tripped": True, "day": at(14).date().isoformat()})
    assert g13_view.now_view(conn, ON, at(14)) is None                          # bezpiecznik


def test_now_view_dzien_wolny_i_granice_okna(conn):
    feed(conn, at(14), False)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 0.0, "day": FRI.isoformat()})
    assert g13_view.now_view(conn, ON, at(14)) is None
    feed(conn, at(14), True)
    assert g13_view.now_view(conn, ON, at(13))["shift"] == "+0"                 # 13:00 już okno, zapis 0 nie jest „brak”
    assert g13_view.now_view(conn, ON, at(16))["phase"] == "szczyt do 21:00"
    assert g13_view.now_view(conn, ON, at(21)) is None


def test_teraz_brak_gdy_zapis_z_wczoraj(conn):
    feed(conn, at(14), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "base": 0.0, "day": "2026-11-05"})
    assert g13_view.now_view(conn, ON, at(14)) is None


def test_teraz_brak_bez_zapisu(conn):
    feed(conn, at(14), True)
    assert g13_view.build(conn, ON, at(14))["now"] is None


def test_dzien_wolny_brak_nastepnej(conn):
    feed(conn, at(10), False)
    assert g13_view.build(conn, ON, at(10))["next"] is None


def test_wstrzymany_i_bezpiecznik_tylko_dzis(conn):
    feed(conn, at(10), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"suspended_day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(10))["status"] == "suspended"
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"tripped": True, "day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(10))["status"] == "tripped"
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"tripped": True, "day": "2026-11-05", "suspended_day": "2026-11-05"})
    assert g13_view.build(conn, ON, at(10))["status"] == "active"


def test_energia_strefy_szczytu_dzis_i_tydzien(conn):
    feed(conn, at(10), True)
    run(conn, "2026-11-06T16:30:00+00:00", {"evening_peak": {"kwh": 2.0, "pln": 3.0}, "other": {"kwh": 5.0, "pln": 3.1}})
    run(conn, "2026-11-02T16:00:00+00:00", {"evening_peak": {"kwh": 1.5, "pln": 2.25}})
    run(conn, "2026-10-20T16:00:00+00:00", {"evening_peak": {"kwh": 9.0, "pln": 9.0}})      # poza 7 dniami
    v = g13_view.build(conn, ON, at(10))
    assert v["today"] == {"kwh": 2.0, "pln": 3.0}
    assert v["week"] == {"kwh": 3.5, "pln": 5.25}


def test_nazwa_pokoju_bezpiecznika_tylko_dzis(conn):
    feed(conn, at(10), True)
    room = {"name": "Salon", "c": 20.1}
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"tripped": True, "day": FRI.isoformat(), "trip_room": room})
    assert g13_view.build(conn, ON, at(10))["trip_room"] == room
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"tripped": True, "day": "2026-11-05", "trip_room": room})
    assert g13_view.build(conn, ON, at(10))["trip_room"] is None


def test_zadzialania_bezpiecznika_7_dni(conn):
    feed(conn, at(10), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"trips": ["2026-10-30", "2026-11-02", "2026-11-06"]})
    assert g13_view.build(conn, ON, at(10))["trips_7d"] == 2


def test_wstrzymany_gdy_glowny_wylacznik_heiko_wylaczony(conn):
    feed(conn, at(10), True)
    v = g13_view.build(conn, {**ON, "heiko_enabled": False}, at(10))
    assert v["status"] == "blocked" and v["next"] is None


def test_okno_letnie_next_now_window(conn):
    feed(conn, at(10), True, window=SUMMER)
    v = g13_view.build(conn, ON, at(10))
    assert v["workday"] is True and v["window"] == {"start": "19:00", "end": "22:00"}
    assert v["next"] == {"at": "16:00", "value": "+1"}
    assert g13_view.build(conn, ON, at(17))["next"] == {"at": "19:00", "value": "-3"}
    assert g13_view.build(conn, ON, at(20))["next"] == {"at": "22:00", "value": "powrót do bazowego"}
    assert g13_view.build(conn, ON, at(23))["next"] is None
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": -3.0, "base": 0.0, "day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(20))["now"] == {"phase": "szczyt do 22:00", "shift": "-3", "base": "0"}
    assert g13_view.now_view(conn, ON, at(17))["phase"] == "przedgrzewanie do 19:00"
    assert g13_view.now_view(conn, ON, at(22)) is None


def test_okno_do_polnocy_bez_kroku_konca(conn):
    feed(conn, at(10), True, window=(20, 24))
    assert g13_view.build(conn, ON, at(21))["next"] is None
    assert g13_view.build(conn, ON, at(18))["next"] == {"at": "20:00", "value": "-3"}


def test_brak_szczytu_i_brak_harmonogramu(conn):
    feed(conn, at(10), False)
    v = g13_view.build(conn, ON, at(10))
    assert v["workday"] is False and v["window"] is None and v["next"] is None
    conn.execute("DELETE FROM tariff_hours")
    conn.commit()
    v = g13_view.build(conn, ON, at(10))
    assert v["workday"] is None and v["window"] is None and v["next"] is None


def test_status_urlop_etykieta_bez_now_next(conn):
    feed(conn, at(17), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": -3.0, "base": 0.0, "day": FRI.isoformat()})
    on = {**ON, "vacation_enabled": True}
    v = g13_view.build(conn, on, at(17))
    assert v["status"] == "vacation" and v["status_label"] == "urlop: szczyt −3"
    assert v["now"] is None and v["next"] is None
    assert g13_view.now_view(conn, on, at(17)) is None
    assert g13_view.build(conn, {**on, "heiko_enabled": False}, at(17))["status"] == "blocked"
    assert g13_view.build(conn, {**on, "g13_peak_enabled": False}, at(17))["status"] == "off"
