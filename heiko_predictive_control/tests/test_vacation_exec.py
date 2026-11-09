"""Tryb urlopowy — wykonanie: zapis przez experiments._write, ręczna zmiana, bezpiecznik, restart. Bez sieci."""
from datetime import datetime, timedelta

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import vacation_exec as vac

SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
ON = {"vacation_enabled": True, "heiko_enabled": True, "heiko_curve_shift_entity": SHIFT, "advisor_notify_service": "notify.x"}
OFF = {**ON, "vacation_enabled": False}


def at(h, m=0, day=9):
    return datetime(2026, 11, day, h, m)


class Pump:
    def __init__(self, shift=0.0, confirm=True, accept=True):
        self.shift, self.confirm, self.accept = shift, confirm, accept
        self.calls, self.notes = [], []

    def get_numeric(self, entity):
        return self.shift if entity == SHIFT else None

    def writer(self, domain, service, data):
        self.calls.append(data["value"])
        if self.accept and self.confirm:
            self.shift = data["value"]
        return self.accept

    def notify(self, service, title, message):
        self.notes.append(message)
        return True


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def room(conn, when, value, name=None):
    conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, min_room_c, min_room_name) "
                 "VALUES (?, 'heiko', 'komfort', 1, ?, ?)", (when.isoformat(timespec="seconds"), value, name))
    conn.commit()


def go(conn, pump, now, settings=ON, room_c=21.5, confirm=True, room_name=None):
    if room_c is not None:
        room(conn, now, room_c, room_name)
    if confirm:
        ex.tick(conn, settings, now, pump.get_numeric, pump.writer, pump.notify)
    return vac.run(conn, settings, now, get_numeric=pump.get_numeric, writer=pump.writer, notify=pump.notify)


def state(conn):
    return dbm.get_setting(conn, vac.STATE_KEY) or {}


def test_state_key_and_title():
    assert vac.STATE_KEY == "vacation_state" and vac.TITLE == "Heiko: tryb urlopowy"


def test_active_and_config_defaults():
    assert vac.active({"vacation_enabled": True, "heiko_enabled": True})
    assert not vac.active({"heiko_enabled": True}) and not vac.active({"vacation_enabled": True})
    cfg = vac._config({"vacation_curve_drop": None, "vacation_min_room_c": None})
    assert (cfg.drop, cfg.min_room_c) == (2, 16.0)
    cfg = vac._config({"vacation_curve_drop": 3, "vacation_min_room_c": 17})
    assert (cfg.drop, cfg.min_room_c) == (3, 17.0)


def test_enable_writes_drop_and_remembers_base(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(12))
    assert pump.calls == [-2.0]
    st = state(conn)
    assert st["base"] == 0.0 and st["last_written"] == -2.0 and st["intent"] == -2.0
    go(conn, pump, at(12, 5))
    go(conn, pump, at(18))
    assert pump.calls == [-2.0]                                # działa całą dobę, bez drugiego zapisu


def test_clamped_to_minus_three(conn):
    pump = Pump(shift=-2.0)
    go(conn, pump, at(12), settings={**ON, "vacation_curve_drop": 3})
    assert pump.calls == [-3.0]


def test_disable_returns_to_base_once(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5))
    go(conn, pump, at(13), settings=OFF)
    assert pump.calls == [-2.0, 0.0] and pump.shift == 0.0
    assert state(conn).get("base") is None
    go(conn, pump, at(14), settings=OFF)
    assert pump.calls == [-2.0, 0.0]


def test_disable_after_manual_change_does_not_overwrite(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5))
    pump.shift = 3.0
    go(conn, pump, at(13), settings=OFF)
    assert pump.calls == [-2.0] and pump.shift == 3.0


def test_disable_with_rejected_return_retries(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(12))
    pump.accept = False
    go(conn, pump, at(12, 10), settings=OFF)
    assert pump.shift == -2.0 and state(conn)["base"] == 0.0
    assert len(pump.notes) == 1 and "ponawiam" in pump.notes[0]
    go(conn, pump, at(12, 15), settings=OFF)
    assert len(pump.notes) == 1
    pump.accept = True
    go(conn, pump, at(12, 20), settings=OFF)
    assert pump.shift == 0.0 and state(conn)["base"] is None


def test_restart_in_progress_is_not_manual_change(conn):
    pump = Pump(shift=-2.0)
    dbm.set_setting(conn, vac.STATE_KEY, {"base": 0.0, "intent": -2.0, "last_written": None, "tripped": False,
                                          "cold_since": None, "suspended_day": None})
    go(conn, pump, at(12))
    assert pump.calls == [] and pump.notes == []
    assert state(conn)["suspended_day"] is None and state(conn)["base"] == 0.0


def test_rejected_write_suspends_with_maybe_and_notification(conn):
    pump = Pump(shift=0.0, accept=False)
    go(conn, pump, at(12))
    assert pump.calls == [-2.0]
    assert len(pump.notes) == 1 and "odrzuci" in pump.notes[0]
    st = state(conn)
    assert st["suspended_day"] == "2026-11-09" and st.get("last_written") is None and st["maybe"] == -2.0
    go(conn, pump, at(12, 5))
    go(conn, pump, at(15))
    assert pump.calls == [-2.0] and len(pump.notes) == 1


def test_no_confirmation_suspends_day(conn):
    pump = Pump(shift=0.0, confirm=False)
    go(conn, pump, at(12))
    assert pump.calls == [-2.0]
    go(conn, pump, at(12, 11))
    assert len(pump.notes) == 1 and "nie potwierdzi" in pump.notes[0]
    assert state(conn)["suspended_day"] == "2026-11-09"
    go(conn, pump, at(14))
    assert pump.calls == [-2.0]


def test_unconfirmed_write_waits(conn):
    pump = Pump(shift=0.0, confirm=False)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5))
    go(conn, pump, at(12, 8))
    assert pump.calls == [-2.0] and pump.notes == []


def test_manual_change_suspends_until_tomorrow_then_resumes(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5))
    pump.shift = 1.0
    go(conn, pump, at(12, 10))
    assert len(pump.notes) == 1 and "wstrzymany do jutra" in pump.notes[0] and "ręcznie" in pump.notes[0] and "1" in pump.notes[0]
    st = state(conn)
    assert st["suspended_day"] == "2026-11-09" and st["base"] is None
    go(conn, pump, at(20))
    assert pump.calls == [-2.0]
    go(conn, pump, at(8, day=10))                              # następny dzień: wstrzymanie wygasa, baza = 1
    assert pump.calls == [-2.0, -1.0]


def test_breaker_returns_to_base_notifies_once_and_resumes(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5))
    for m in (10, 15, 20):
        go(conn, pump, at(12, m), room_c=15.0, room_name="Salon")
    assert pump.calls == [-2.0] and pump.notes == []
    for m in (25, 30, 35):                                     # 15 min od 12:10 → zadziałanie o 12:25, jedno powiadomienie
        go(conn, pump, at(12, m), room_c=15.0, room_name="Salon")
    assert pump.calls == [-2.0, 0.0] and len(pump.notes) == 1 and "Salon" in pump.notes[0] and "15" in pump.notes[0]
    st = state(conn)
    assert st["trips"] == ["2026-11-09"] and st["trip_room"] == {"name": "Salon", "c": 15.0}
    go(conn, pump, at(13), room_c=16.5)                        # histereza: jeszcze nie wznawia
    assert pump.calls == [-2.0, 0.0]
    go(conn, pump, at(13, 5), room_c=17.5)
    assert pump.calls == [-2.0, 0.0, -2.0] and len(pump.notes) == 1


def test_master_switch_off_blocks_writes_and_returns(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(12), settings={**ON, "heiko_enabled": False})
    assert pump.calls == []
    go(conn, pump, at(12, 5))
    go(conn, pump, at(12, 10))
    assert pump.shift == -2.0
    go(conn, pump, at(12, 15), settings={**ON, "heiko_enabled": False})
    assert pump.shift == 0.0


def test_running_test_blocks_mode(conn):
    pump = Pump()
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('zapis', 'w_toku', ?, '[]', 0)",
                 (at(11).isoformat(),))
    conn.commit()
    room(conn, at(12), 21.5)
    assert vac.run(conn, ON, at(12), get_numeric=pump.get_numeric, writer=pump.writer, notify=pump.notify) is None
    assert pump.calls == [] and pump.notes == []
