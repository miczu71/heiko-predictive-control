"""Tryb „Szczyt G13” (E3): wykonanie — zapis przez experiments._write, ręczna zmiana, bezpiecznik, restart. Bez sieci."""
from datetime import date, datetime, timedelta

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import g13_exec as g13

SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
ON = {"g13_peak_enabled": True, "heiko_enabled": True, "heiko_curve_shift_entity": SHIFT, "advisor_notify_service": "notify.x"}
OFF = {**ON, "g13_peak_enabled": False}
FRI, MON, SAT = date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 7)


def at(h, m=0, day=FRI):
    return datetime(day.year, day.month, day.day, h, m)


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


def feed(conn, now, workday=True, with_rows=True):
    """Harmonogram taryfy: o 17:00 szczyt (`is_peak`) tylko w dni robocze."""
    if with_rows:
        conn.execute("INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                     "VALUES (?, 'G13', 'szczyt', ?, NULL, 1.0, 'wysoka', ?) ON CONFLICT(hour) DO UPDATE SET "
                     "is_peak = excluded.is_peak, fetched_at = excluded.fetched_at",
                     (now.strftime("%Y-%m-%dT17"), int(workday), now.isoformat(timespec="seconds")))
        conn.commit()


def room(conn, when, value):
    conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, min_room_c) VALUES (?, 'heiko', 'komfort', 1, ?)",
                 (when.isoformat(timespec="seconds"), value))
    conn.commit()


def go(conn, pump, now, settings=ON, room_c=21.5, workday=True, with_feed=True, confirm=True):
    """Jeden przebieg jak w produkcji: potwierdzenia (experiments.tick), potem tryb."""
    feed(conn, now, workday, with_feed)
    if room_c is not None:
        room(conn, now, room_c)
    if confirm:
        ex.tick(conn, settings, now, pump.get_numeric, pump.writer, pump.notify)
    return g13.run(conn, settings, now, get_numeric=pump.get_numeric, writer=pump.writer, notify=pump.notify,
                   is_workday=lambda d: d.weekday() < 5)


def state(conn):
    return dbm.get_setting(conn, g13.STATE_KEY) or {}


def test_state_key():
    assert g13.STATE_KEY == "g13_peak_state"


def test_disabled_writes_nothing(conn):
    pump = Pump()
    assert go(conn, pump, at(17), settings=OFF) is None
    assert pump.calls == [] and state(conn) == {}


def test_running_test_blocks_mode(conn):
    pump = Pump()
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('zapis', 'w_toku', ?, '[]', 0)",
                 (at(12).isoformat(),))
    conn.commit()
    feed(conn, at(17))
    room(conn, at(17), 21.5)
    assert g13.run(conn, ON, at(17), get_numeric=pump.get_numeric, writer=pump.writer, notify=pump.notify) is None
    assert pump.calls == [] and pump.notes == []


def test_weekend_writes_nothing(conn):
    pump = Pump()
    for h in (13, 16, 18):
        go(conn, pump, at(h, day=SAT), workday=False)
    assert pump.calls == []


def test_holiday_writes_nothing(conn):
    pump = Pump()
    for h in (13, 16, 18):
        go(conn, pump, at(h), workday=False)                  # piątek, ale bez szczytu w harmonogramie
    assert pump.calls == []


def test_no_feed_writes_nothing(conn):
    pump = Pump()
    for h in (13, 16, 18):
        go(conn, pump, at(h), with_feed=False)
    assert pump.calls == []


def test_preheat_writes_plus_one_with_base_remembered(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(13))
    assert pump.calls == [1.0]
    st = state(conn)
    assert st["base"] == 0.0 and st["last_written"] == 1.0 and st["intent"] == 1.0
    go(conn, pump, at(13, 5))
    go(conn, pump, at(14))
    assert pump.calls == [1.0]                                 # nic się nie zmieniło — bez drugiego zapisu


def test_peak_writes_minus_four_in_one_jump(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(13))
    go(conn, pump, at(13, 5))
    go(conn, pump, at(16))
    assert pump.calls == [1.0, -4.0]
    assert pump.shift == -4.0 and state(conn)["last_written"] == -4.0


def test_return_to_base_at_21_and_state_cleared_after_confirmation(conn):
    pump = Pump(shift=0.0)
    for now in (at(13), at(13, 5), at(16), at(16, 5), at(20, 55)):
        go(conn, pump, now)
    go(conn, pump, at(21))
    assert pump.calls == [1.0, -4.0, 0.0]
    assert state(conn)["base"] == 0.0                          # jeszcze niewyzerowane — czekamy na potwierdzenie
    go(conn, pump, at(21, 5))
    st = state(conn)
    assert st["base"] is None and st["last_written"] is None and st["intent"] is None
    assert pump.calls == [1.0, -4.0, 0.0] and pump.notes == []


def test_breaker_returns_to_base_with_single_notification(conn):
    pump = Pump(shift=0.0)
    for now in (at(13), at(13, 5)):
        go(conn, pump, now)
    for now in (at(16), at(16, 5), at(16, 10)):
        go(conn, pump, now, room_c=20.0)
    assert pump.calls == [1.0, -4.0] and pump.notes == []
    go(conn, pump, at(16, 15), room_c=20.0)
    assert pump.calls == [1.0, -4.0, 0.0]
    assert len(pump.notes) == 1 and "20.5" in pump.notes[0] and "15 min" in pump.notes[0]
    for now in (at(16, 20), at(16, 25), at(17)):
        go(conn, pump, now, room_c=20.0)
    assert pump.calls == [1.0, -4.0, 0.0] and len(pump.notes) == 1
    assert dbm.get_setting(conn, g13.STATE_KEY)["trips"] == [FRI.isoformat()]      # jedno zadziałanie na dobę


def test_missing_room_reading_does_not_trip_breaker(conn):
    pump = Pump(shift=0.0)
    room(conn, at(12), 19.0)                                   # stary wiersz (> 20 min) — ignorowany
    for now in (at(13), at(13, 5), at(16), at(16, 30), at(17), at(18)):
        go(conn, pump, now, room_c=None)
    assert pump.calls == [1.0, -4.0] and pump.notes == []
    assert state(conn)["tripped"] is False


def test_manual_change_suspends_until_tomorrow(conn):
    pump = Pump(shift=0.0)
    for now in (at(13), at(13, 5), at(16), at(16, 3)):
        go(conn, pump, now)
    pump.shift = 2.0                                           # user zmienił przesunięcie ręcznie
    go(conn, pump, at(16, 5))
    assert pump.calls == [1.0, -4.0]
    assert len(pump.notes) == 1 and "ręcznie" in pump.notes[0] and "2" in pump.notes[0]
    st = state(conn)
    assert st["suspended_day"] == FRI.isoformat() and st["base"] is None
    for now in (at(17), at(20), at(21), at(21, 5)):
        go(conn, pump, now)
    assert pump.calls == [1.0, -4.0] and len(pump.notes) == 1
    go(conn, pump, at(13, day=MON))                            # kolejny dzień roboczy — tryb znów działa (baza = 2)
    assert pump.calls == [1.0, -4.0, 3.0]


def test_restart_in_window_is_not_a_manual_change(conn):
    pump = Pump(shift=-4.0)
    dbm.set_setting(conn, g13.STATE_KEY, {"day": FRI.isoformat(), "base": 0.0, "intent": -4.0, "last_written": None,
                                          "tripped": False, "cold_since": None, "suspended_day": None})
    go(conn, pump, at(17))
    assert pump.calls == [] and pump.notes == []
    assert state(conn)["suspended_day"] is None and state(conn)["base"] == 0.0


def test_unconfirmed_write_waits(conn):
    pump = Pump(shift=0.0, confirm=False)
    go(conn, pump, at(13))
    assert pump.calls == [1.0]
    go(conn, pump, at(13, 5))
    go(conn, pump, at(13, 8))
    assert pump.calls == [1.0] and pump.notes == []


def test_no_confirmation_suspends_day_and_still_returns_after_21(conn):
    pump = Pump(shift=0.0, confirm=False)
    go(conn, pump, at(16))
    assert pump.calls == [-4.0]
    go(conn, pump, at(16, 11))                                 # tick oznacza NO_CONFIRMATION i wysyła powiadomienie
    assert len(pump.notes) == 1 and "nie potwierdzi" in pump.notes[0]
    assert state(conn)["suspended_day"] == FRI.isoformat()
    go(conn, pump, at(16, 20))
    go(conn, pump, at(18))
    assert pump.calls == [-4.0]
    pump.shift = -4.0                                          # pompa jednak przyjęła zapis
    go(conn, pump, at(21))
    assert pump.calls == [-4.0, 0.0]
    assert len(pump.notes) == 1


def test_writer_rejection_suspends_day_with_notification(conn):
    pump = Pump(shift=0.0, accept=False)
    go(conn, pump, at(16))
    assert pump.calls == [-4.0]
    assert len(pump.notes) == 1 and "odrzuci" in pump.notes[0]
    assert state(conn)["suspended_day"] == FRI.isoformat() and state(conn).get("last_written") is None
    go(conn, pump, at(16, 5))
    go(conn, pump, at(17))
    assert pump.calls == [-4.0] and len(pump.notes) == 1


def test_disabling_mid_window_returns_to_base_once(conn):
    pump = Pump(shift=0.0)
    for now in (at(13), at(13, 5), at(16), at(16, 5)):
        go(conn, pump, now)
    assert pump.shift == -4.0
    go(conn, pump, at(16, 10), settings=OFF)
    assert pump.calls == [1.0, -4.0, 0.0] and pump.shift == 0.0
    assert state(conn).get("base") is None
    go(conn, pump, at(16, 15), settings=OFF)
    go(conn, pump, at(17), settings=OFF)
    assert pump.calls == [1.0, -4.0, 0.0]


def test_disabling_after_manual_change_does_not_overwrite(conn):
    pump = Pump(shift=0.0)
    for now in (at(13), at(13, 5)):
        go(conn, pump, now)
    pump.shift = 3.0
    go(conn, pump, at(14), settings=OFF)
    assert pump.calls == [1.0] and pump.shift == 3.0


def test_manual_change_to_base_value_in_peak_is_detected(conn):
    pump = Pump(shift=0.0)
    for now in (at(13), at(13, 5), at(16), at(16, 3)):
        go(conn, pump, now)
    assert pump.calls == [1.0, -4.0]
    pump.shift = 0.0                                           # user ustawił ręcznie wartość bazową
    go(conn, pump, at(16, 5))
    go(conn, pump, at(16, 10))
    assert pump.calls == [1.0, -4.0] and pump.shift == 0.0
    assert len(pump.notes) == 1 and "ręcznie" in pump.notes[0]
    assert state(conn)["suspended_day"] == FRI.isoformat()


def test_return_failure_after_21_notifies_once_per_day(conn):
    pump = Pump(shift=-4.0, accept=False)
    dbm.set_setting(conn, g13.STATE_KEY, {"day": FRI.isoformat(), "base": 0.0, "intent": -4.0, "last_written": -4.0})
    for now in (at(21), at(21, 5), at(21, 10), at(22)):
        go(conn, pump, now)
    assert len(pump.notes) == 1 and len(pump.calls) == 4


def test_restart_after_21_returns_to_base(conn):
    pump = Pump(shift=-4.0)
    dbm.set_setting(conn, g13.STATE_KEY, {"day": FRI.isoformat(), "base": 0.0, "intent": -4.0, "last_written": -4.0,
                                          "tripped": False, "cold_since": None, "suspended_day": None})
    go(conn, pump, at(21, 5))
    assert pump.calls == [0.0] and pump.notes == []


def test_nc_of_foreign_write_is_ignored(conn):
    """NO_CONFIRMATION cudzego zapisu (nie z trybu) nie wstrzymuje dnia."""
    pump = Pump(shift=0.0)
    conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok, confirmed_at) VALUES (?, NULL, ?, 0, 1, 1, ?)",
                 (at(12).isoformat(), SHIFT, "brak potwierdzenia"))
    conn.commit()
    go(conn, pump, at(13))
    assert pump.calls == [1.0]


def test_manual_change_after_no_confirmation_is_detected_and_not_overwritten_after_21(conn):
    pump = Pump(shift=0.0, confirm=False)
    go(conn, pump, at(16))
    go(conn, pump, at(16, 11))                                 # NO_CONFIRMATION, tryb wstrzymany
    assert pump.calls == [-4.0] and len(pump.notes) == 1
    pump.shift = 2.0                                           # user ustawił ręcznie (ani stara wartość, ani zapis trybu)
    go(conn, pump, at(18))
    assert len(pump.notes) == 2 and "ręcznie" in pump.notes[1]
    go(conn, pump, at(21, 5))
    assert pump.calls == [-4.0]


def test_late_applied_write_after_no_confirmation_then_disable_returns_to_base(conn):
    pump = Pump(shift=0.0, confirm=False)
    go(conn, pump, at(16))
    go(conn, pump, at(16, 11))                                 # NO_CONFIRMATION
    pump.shift = -4.0                                          # pompa przyjęła zapis z opóźnieniem
    go(conn, pump, at(17), settings=OFF)
    assert pump.calls == [-4.0, 0.0]
    assert state(conn).get("base") is None


def test_rejected_write_keeps_previous_value_known(conn):
    """Odrzucony zapis nie może zrobić z poprzedniej wartości „ręcznej zmiany”."""
    pump = Pump(shift=0.0, accept=False)
    go(conn, pump, at(16))
    go(conn, pump, at(16, 5))
    assert pump.calls == [-4.0] and len(pump.notes) == 1       # tylko powiadomienie o odrzuceniu


def test_disable_with_rejected_return_keeps_state_and_retries(conn):
    pump = Pump(shift=0.0)
    for now in (at(13), at(16)):
        go(conn, pump, now)
    assert pump.shift == -4.0
    pump.accept = False
    go(conn, pump, at(16, 10), settings=OFF)                  # powrót odrzucony: stan nie ginie
    st = dbm.get_setting(conn, g13.STATE_KEY)
    assert pump.shift == -4.0 and st["base"] == 0.0
    notes = len(pump.notes)
    assert notes == 1 and "ponawiam" in pump.notes[0]
    go(conn, pump, at(16, 15), settings=OFF)                  # kolejna odmowa tego samego dnia: bez drugiego powiadomienia
    assert len(pump.notes) == notes
    pump.accept = True
    go(conn, pump, at(16, 20), settings=OFF)
    assert pump.shift == 0.0
    assert dbm.get_setting(conn, g13.STATE_KEY)["base"] is None


def test_write_applied_despite_error_is_not_manual_change_and_returns_to_base(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(13))
    go(conn, pump, at(13, 5))                                 # potwierdzenie z ramki
    assert pump.shift == 1.0
    good_writer = pump.writer

    def late_accept(domain, service, data):                   # zapis dociera do pompy, ale usługa wraca z błędem (timeout)
        pump.shift = data["value"]
        return False
    pump.writer = late_accept
    go(conn, pump, at(16), confirm=False)
    assert pump.shift == -4.0
    notes = len(pump.notes)
    go(conn, pump, at(16, 5), confirm=False)
    st = dbm.get_setting(conn, g13.STATE_KEY)
    assert st["base"] == 0.0 and len(pump.notes) == notes          # nie „ręczna zmiana”, baza zachowana
    pump.writer = good_writer
    go(conn, pump, at(21), confirm=False)
    assert pump.shift == 0.0


def test_master_switch_off_blocks_writes(conn):
    pump = Pump(shift=0.0)
    go(conn, pump, at(16), settings={**ON, "heiko_enabled": False})
    assert pump.calls == []                                   # główny wyłącznik Heiko gasi też tryb Szczyt G13


def test_null_options_fall_back_to_defaults(conn):
    cfg = g13._config({"g13_peak_min_room_c": None, "g13_peak_preheat_shift": None, "g13_peak_peak_shift": None})
    assert (cfg.min_room_c, cfg.preheat_shift, cfg.peak_shift) == (20.5, 1.0, -4.0)
