"""Tryb urlopowy — wykonanie: zapis przez experiments._write, ręczna zmiana, bezpiecznik, restart. Bez sieci."""
from datetime import datetime, timedelta

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import vacation_exec as vac

SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
DHW = "number.heiko_heat_pump_dhw_setpoint"
ON = {"vacation_enabled": True, "heiko_enabled": True, "heiko_curve_shift_entity": SHIFT, "advisor_notify_service": "notify.x"}
OFF = {**ON, "vacation_enabled": False}


def at(h, m=0, day=9):
    return datetime(2026, 11, day, h, m)


class Pump:
    def __init__(self, shift=0.0, confirm=True, accept=True, dhw=None, dhw_min=16, dhw_accept=True):
        self.shift, self.confirm, self.accept = shift, confirm, accept
        self.dhw, self.dhw_min, self.dhw_accept = dhw, dhw_min, dhw_accept
        self.calls, self.dhw_calls, self.notes = [], [], []

    def get_numeric(self, entity):
        return self.shift if entity == SHIFT else self.dhw if entity == DHW else None

    def get_state(self, entity):
        return {"state": str(self.dhw), "attributes": {"min": self.dhw_min}} if entity == DHW else None

    def writer(self, domain, service, data):
        if data["entity_id"] == DHW:
            self.dhw_calls.append(data["value"])
            if self.dhw_accept and self.confirm:
                self.dhw = data["value"]
            return self.dhw_accept
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
    return vac.run(conn, settings, now, get_numeric=pump.get_numeric, get_state=pump.get_state, writer=pump.writer,
                   notify=pump.notify)


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


# ── CWU (S5) ─────────────────────────────────────────────────────────────────────────────────────────

def test_dhw_entity_and_target():
    assert vac.dhw_entity(ON) == DHW and vac.DHW_AFTER == 48
    assert vac.dhw_target({}) == 20 and vac.dhw_target({"vacation_dhw_setpoint": 25}) == 25
    assert vac.dhw_target({"vacation_dhw_setpoint": 5}) == 16 and vac.dhw_target({"vacation_dhw_setpoint": 99}) == 60


def test_enable_writes_dhw_and_sets_active(conn):
    pump = Pump(dhw=48.0)
    go(conn, pump, at(12))
    assert pump.dhw_calls == [20] and pump.calls == [-2.0]
    st = state(conn)
    assert st["dhw_active"] is True and st["dhw_before"] == 48.0 and st.get("dhw_warn") is None
    go(conn, pump, at(12, 5))
    assert pump.dhw_calls == [20]


def test_dhw_min_above_target_no_write_warns_curve_works(conn, caplog):
    pump = Pump(dhw=48.0, dhw_min=40)
    with caplog.at_level("WARNING"):
        go(conn, pump, at(12))
        go(conn, pump, at(12, 5))
    assert pump.dhw_calls == [] and pump.calls == [-2.0] and pump.notes == []
    st = state(conn)
    assert st["dhw_warn"] == "min" and not st.get("dhw_active")
    assert sum("CWU" in r.message for r in caplog.records) == 1
    pump.dhw_min = 16                                          # po aktualizacji integracji zapis rusza sam
    go(conn, pump, at(12, 10))
    assert pump.dhw_calls == [20] and state(conn)["dhw_active"] is True


def test_dhw_unknown_warns_brak(conn):
    pump = Pump(dhw=None)
    go(conn, pump, at(12))
    assert pump.dhw_calls == [] and state(conn)["dhw_warn"] == "brak" and pump.calls == [-2.0]


def test_dhw_already_target_no_write(conn):
    pump = Pump(dhw=20.0)
    go(conn, pump, at(12))
    assert pump.dhw_calls == []
    st = state(conn)
    assert st["dhw_active"] is True and st["dhw_before"] == 20.0


def test_dhw_rejected_notifies_once_retries_next_day_curve_not_suspended(conn):
    pump = Pump(dhw=48.0, dhw_accept=False)
    go(conn, pump, at(12))
    assert pump.dhw_calls == [20] and pump.calls == [-2.0]
    assert len(pump.notes) == 1 and "CWU" in pump.notes[0] and "48" in pump.notes[0]
    st = state(conn)
    assert not st.get("dhw_active") and st["dhw_fail_day"] == "2026-11-09" and not st.get("suspended_day")
    go(conn, pump, at(12, 5))
    go(conn, pump, at(18))
    assert pump.dhw_calls == [20] and len(pump.notes) == 1
    go(conn, pump, at(8, day=10))
    assert pump.dhw_calls == [20, 20] and len(pump.notes) == 2


def test_disable_restores_dhw_48_once_and_clears(conn):
    pump = Pump(dhw=48.0)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5))
    go(conn, pump, at(13), settings=OFF)
    assert pump.dhw_calls == [20, 48] and pump.dhw == 48
    st = state(conn)
    assert not st.get("dhw_active") and not any(k.startswith("dhw_") and st.get(k) for k in st)
    go(conn, pump, at(14), settings=OFF)
    assert pump.dhw_calls == [20, 48]


def test_disable_dhw_rejected_retries_notify_daily(conn):
    pump = Pump(dhw=48.0)
    go(conn, pump, at(12))
    pump.dhw_accept = False
    go(conn, pump, at(13), settings=OFF)
    go(conn, pump, at(13, 5), settings=OFF)
    assert pump.dhw_calls == [20, 48, 48] and state(conn)["dhw_active"] is True
    assert sum("CWU" in n for n in pump.notes) == 1
    pump.dhw_accept = True
    go(conn, pump, at(13, 10), settings=OFF)
    assert pump.dhw == 48 and not state(conn).get("dhw_active")


def test_disable_restores_dhw_when_base_is_none(conn):
    pump = Pump(dhw=48.0)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5))
    pump.shift = 1.0
    go(conn, pump, at(12, 10))                                 # ręczna zmiana → base None, krzywa wstrzymana
    assert state(conn)["base"] is None and state(conn)["dhw_active"] is True
    go(conn, pump, at(13), settings=OFF)
    assert pump.dhw_calls == [20, 48] and pump.dhw == 48


def test_master_switch_off_restores_dhw(conn):
    pump = Pump(dhw=48.0)
    go(conn, pump, at(12))
    go(conn, pump, at(12, 5), settings={**ON, "heiko_enabled": False})
    assert pump.dhw == 48


def test_restart_after_intent_does_not_rewrite(conn):
    pump = Pump(dhw=20.0)
    dbm.set_setting(conn, vac.STATE_KEY, {"dhw_intent": 20, "dhw_active": False})
    go(conn, pump, at(12))
    assert pump.dhw_calls == [] and state(conn)["dhw_active"] is True


def test_running_test_blocks_dhw(conn):
    pump = Pump(dhw=48.0)
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('zapis', 'w_toku', ?, '[]', 0)",
                 (at(11).isoformat(),))
    conn.commit()
    room(conn, at(12), 21.5)
    vac.run(conn, ON, at(12), get_numeric=pump.get_numeric, get_state=pump.get_state, writer=pump.writer, notify=pump.notify)
    assert pump.dhw_calls == []
    dbm.set_setting(conn, vac.STATE_KEY, {"dhw_active": True})
    vac.run(conn, OFF, at(13), get_numeric=pump.get_numeric, get_state=pump.get_state, writer=pump.writer, notify=pump.notify)
    assert pump.dhw_calls == []
