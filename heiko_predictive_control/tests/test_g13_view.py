"""Tryb „Szczyt G13” (E4): widok dla pulpitu — stan, następna zmiana, energia strefy szczytu, bezpiecznik."""
import json
from datetime import date, datetime

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import g13_exec, g13_view
from tests.test_g13_exec import FRI, ON, at, feed

WD = lambda d: d.weekday() < 5


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
    v = g13_view.build(conn, {}, at(10), WD)
    assert (v["status"], v["enabled"], v["next"]) == ("off", False, None)


def test_nastepna_zmiana_w_dzien_roboczy(conn):
    feed(conn, at(14), True)
    v = g13_view.build(conn, ON, at(14), WD)
    assert v["status"] == "active" and v["next"] == {"at": "16:00", "value": "-3"}
    assert g13_view.build(conn, ON, at(22), WD)["next"] is None


def test_teraz_faza_i_przesuniecie_z_ostatniego_zapisu(conn):
    feed(conn, at(14), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "base": 0.0, "day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(14), WD)["now"] == {"phase": "przedgrzewanie do 16:00", "shift": "+1", "base": "0"}
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": -3.0, "base": 0.0, "day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(17), WD)["now"] == {"phase": "szczyt do 21:00", "shift": "-3", "base": "0"}
    assert g13_view.build(conn, ON, at(22), WD)["now"] is None
    assert g13_view.build(conn, ON, at(12), WD)["now"] is None
    assert g13_view.build(conn, {}, at(14), WD)["now"] is None


def test_now_view_tylko_gdy_aktywny_w_oknie_dnia_roboczego(conn):
    feed(conn, at(14), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "day": FRI.isoformat()})
    assert g13_view.now_view(conn, ON, at(14), WD) == {"phase": "przedgrzewanie do 16:00", "shift": "+1", "base": None}
    assert g13_view.now_view(conn, {}, at(14), WD) is None                          # wyłączony
    assert g13_view.now_view(conn, {**ON, "heiko_enabled": False}, at(14), WD) is None   # zablokowany
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "day": FRI.isoformat(), "suspended_day": at(14).date().isoformat()})
    assert g13_view.now_view(conn, ON, at(14), WD) is None                          # wstrzymany do jutra
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "tripped": True, "day": at(14).date().isoformat()})
    assert g13_view.now_view(conn, ON, at(14), WD) is None                          # bezpiecznik


def test_now_view_dzien_wolny_i_granice_okna(conn):
    feed(conn, at(14), False)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 0.0, "day": FRI.isoformat()})
    assert g13_view.now_view(conn, ON, at(14), WD) is None
    feed(conn, at(14), True)
    assert g13_view.now_view(conn, ON, at(13), WD)["shift"] == "+0"                 # 13:00 już okno, zapis 0 nie jest „brak”
    assert g13_view.now_view(conn, ON, at(16), WD)["phase"] == "szczyt do 21:00"
    assert g13_view.now_view(conn, ON, at(21), WD) is None


def test_teraz_brak_gdy_zapis_z_wczoraj(conn):
    feed(conn, at(14), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"last_written": 1.0, "base": 0.0, "day": "2026-11-05"})
    assert g13_view.now_view(conn, ON, at(14), WD) is None


def test_teraz_brak_bez_zapisu(conn):
    feed(conn, at(14), True)
    assert g13_view.build(conn, ON, at(14), WD)["now"] is None


def test_dzien_wolny_brak_nastepnej(conn):
    feed(conn, at(10), False)
    assert g13_view.build(conn, ON, at(10), WD)["next"] is None


def test_wstrzymany_i_bezpiecznik_tylko_dzis(conn):
    feed(conn, at(10), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"suspended_day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(10), WD)["status"] == "suspended"
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"tripped": True, "day": FRI.isoformat()})
    assert g13_view.build(conn, ON, at(10), WD)["status"] == "tripped"
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"tripped": True, "day": "2026-11-05", "suspended_day": "2026-11-05"})
    assert g13_view.build(conn, ON, at(10), WD)["status"] == "active"


def test_energia_strefy_szczytu_dzis_i_tydzien(conn):
    feed(conn, at(10), True)
    run(conn, "2026-11-06T16:30:00+00:00", {"evening_peak": {"kwh": 2.0, "pln": 3.0}, "other": {"kwh": 5.0, "pln": 3.1}})
    run(conn, "2026-11-02T16:00:00+00:00", {"evening_peak": {"kwh": 1.5, "pln": 2.25}})
    run(conn, "2026-10-20T16:00:00+00:00", {"evening_peak": {"kwh": 9.0, "pln": 9.0}})      # poza 7 dniami
    v = g13_view.build(conn, ON, at(10), WD)
    assert v["today"] == {"kwh": 2.0, "pln": 3.0}
    assert v["week"] == {"kwh": 3.5, "pln": 5.25}


def test_zadzialania_bezpiecznika_7_dni(conn):
    feed(conn, at(10), True)
    dbm.set_setting(conn, g13_exec.STATE_KEY, {"trips": ["2026-10-30", "2026-11-02", "2026-11-06"]})
    assert g13_view.build(conn, ON, at(10), WD)["trips_7d"] == 2


def test_wstrzymany_gdy_glowny_wylacznik_heiko_wylaczony(conn):
    feed(conn, at(10), True)
    v = g13_view.build(conn, {**ON, "heiko_enabled": False}, at(10), WD)
    assert v["status"] == "blocked" and v["next"] is None
