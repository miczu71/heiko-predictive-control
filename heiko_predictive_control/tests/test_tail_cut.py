"""Odcinanie „ogona” grzania (tail_cut): zapis trybu przez experiments._write_mode, potwierdzenie z raw_value, powrót do Auto. Bez sieci."""
from datetime import datetime, timedelta

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import g13_exec, tail_cut

SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
ON = {"tail_cut_enabled": True, "heiko_enabled": True, "heiko_curve_shift_entity": SHIFT, "advisor_notify_service": "notify.x",
      "tail_delay_min": 45}
OFF = {**ON, "tail_cut_enabled": False}
T0 = datetime(2026, 11, 9, 12, 0)
TC, SETPOINT = "sensor.heiko_heat_pump_condenser_temperature", "sensor.heiko_heat_pump_water_temperature_setpoint"
WORK, MODE = "sensor.heiko_heat_pump_working_mode_2", tail_cut.MODE_SETTING_ENTITY


@pytest.fixture(autouse=True)
def _no_gap_limit(monkeypatch):
    """Większość testów tyka rzadko (0, 45 min) — limit przerwy włącza tylko test stale_tail_since."""
    monkeypatch.setattr(tail_cut, "MAX_GAP_MIN", 10_000)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


class Pump:
    def __init__(self, tc=40.0, setpoint=38.0, work=2, mode=4, confirm=True, accept=True):
        self.tc, self.setpoint, self.work, self.mode = tc, setpoint, work, mode
        self.confirm, self.accept = confirm, accept
        self.calls, self.notes, self.sleeps = [], [], []

    def get_numeric(self, entity):
        return {TC: self.tc, SETPOINT: self.setpoint}.get(entity)

    def get_state(self, entity):
        raw = {WORK: self.work, MODE: self.mode}.get(entity)
        return None if raw is None else {"state": str(raw), "attributes": {"raw_value": raw}}

    def writer(self, domain, service, data):
        assert (domain, service) == ("heiko_heatpump", "set_mode")
        self.calls.append(data["mode"])
        if self.accept and self.confirm:
            self.mode = ex.MODE_CODES[data["mode"]]
        return self.accept

    def notify(self, service, title, message):
        self.notes.append((title, message))
        return True

    def sleep(self, seconds):
        self.sleeps.append(seconds)


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def room(conn, when, value=21.5):
    conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, min_room_c) VALUES (?, 'heiko', 'komfort', 1, ?)",
                 (when.isoformat(timespec="seconds"), value))
    conn.commit()


def go(conn, pump, now, settings=ON, room_c=21.5):
    if room_c is not None:
        room(conn, now, room_c)
    return tail_cut.run(conn, settings, now, get_state=pump.get_state, get_numeric=pump.get_numeric, writer=pump.writer,
                        notify=pump.notify, sleep=pump.sleep)


def state(conn):
    return dbm.get_setting(conn, tail_cut.STATE_KEY) or {}


def rows(conn):
    return [tuple(r) for r in conn.execute("SELECT entity_id, old, new, ok, confirmed_at FROM pump_writes ORDER BY id")]


def cut(conn, pump, settings=ON):
    """Ogon trwający tail_delay_min (45): pierwszy przebieg zaczyna odliczanie, drugi odcina."""
    assert go(conn, pump, at(0), settings) is None
    return go(conn, pump, at(45), settings)


def test_disabled_never_writes_even_with_tail(conn):
    pump = Pump()
    for m in (0, 45, 90):
        assert go(conn, pump, at(m), OFF) is None
    pump.tc = 30.0
    assert go(conn, pump, at(100), OFF) is None
    assert pump.calls == [] and rows(conn) == []
    assert state(conn).get("tail_since") is None


def test_master_switch_off_never_writes(conn):
    pump = Pump()
    for m in (0, 45):
        go(conn, pump, at(m), {**ON, "heiko_enabled": False})
    assert pump.calls == [] and rows(conn) == []


def test_tail_shorter_than_delay_does_not_cut(conn):
    pump = Pump()
    assert go(conn, pump, at(0)) is None
    assert go(conn, pump, at(44)) is None
    assert pump.calls == [] and state(conn)["tail_since"] == at(0).isoformat()


def test_tail_at_delay_cuts_to_dhw_and_confirms(conn):
    pump = Pump()
    cut(conn, pump)
    assert pump.calls == ["dhw"] and pump.mode == 3
    assert rows(conn) == [(ex.MODE_WRITE_ID, 4, 3, 1, at(45).isoformat(timespec="seconds"))]
    st = state(conn)
    assert st["forced"] is True and st["cut_at"] == at(45).isoformat() and st["tail_since"] is None
    assert st["cuts"] == [at(45).isoformat()]
    assert len(pump.notes) == 1 and pump.notes[0][0] == "Heiko: ogon grzania" and "Odcięto ogon grzania" in pump.notes[0][1]
    assert pump.sleeps == [70]


def test_tc_not_above_threshold_never_starts_countdown(conn):
    pump = Pump(tc=38.0)                                 # Tc == nastawa: próg (ścisłe >) niespełniony
    go(conn, pump, at(0))
    assert state(conn).get("tail_since") is None
    assert go(conn, pump, at(60)) is None and pump.calls == []


def test_tc_dip_during_countdown_does_not_reset_and_cuts(conn):
    """Piła Tc: spadek poniżej progu po starcie odliczania NIE zeruje licznika (jak stara Pompa_fix)."""
    pump = Pump()
    go(conn, pump, at(0))
    pump.tc = 37.0                                       # poniżej nastawy
    assert go(conn, pump, at(10)) is None
    assert state(conn)["tail_since"] == at(0).isoformat()
    pump.tc = 38.4
    assert go(conn, pump, at(45))["reason"] == "odcięto ogon"
    assert pump.calls == ["dhw"]


def test_margin_requires_exceeding_setpoint_plus_margin(conn):
    s = {**ON, "tail_margin_k": 1.0}
    pump = Pump(tc=38.9)                                 # < nastawa + 1,0
    go(conn, pump, at(0), s)
    assert state(conn).get("tail_since") is None
    pump.tc = 39.1
    go(conn, pump, at(10), s)
    assert state(conn)["tail_since"] == at(10).isoformat()


def test_stale_tail_since_after_gap_restarts_countdown(conn, monkeypatch):
    """Restart add-onu / przerwa w tickach: stary `tail_since` nie daje natychmiastowego cięcia."""
    monkeypatch.setattr(tail_cut, "MAX_GAP_MIN", 15)
    pump = Pump()
    go(conn, pump, at(0))
    go(conn, pump, at(10))
    assert go(conn, pump, at(60)) is None                # przerwa 50 min > 15: odliczanie od nowa, mimo 60 > 45 min
    assert pump.calls == [] and state(conn)["tail_since"] == at(60).isoformat()
    for m in range(61, 106):
        go(conn, pump, at(m))
    assert pump.calls == ["dhw"]                         # ciągłe tyki: cięcie po 45 min od at(60)


def test_leaving_heating_clears_countdown(conn):
    pump = Pump()
    go(conn, pump, at(0))
    pump.work = 0                                        # Standby
    go(conn, pump, at(20))
    assert state(conn).get("tail_since") is None
    pump.work = 2
    assert go(conn, pump, at(50)) is None                # odliczanie od nowa
    assert pump.calls == []


def test_room_below_threshold_no_cut(conn):
    pump = Pump()
    go(conn, pump, at(0), room_c=20.0)
    assert go(conn, pump, at(45), room_c=20.0) is None
    assert pump.calls == [] and state(conn).get("tail_since") is None


def test_stale_room_reading_no_cut(conn):
    pump = Pump()
    assert go(conn, pump, at(0), room_c=None) is None
    assert go(conn, pump, at(45), room_c=None) is None
    assert pump.calls == []


def test_room_drop_while_forced_returns_to_auto_immediately(conn):
    pump = Pump()
    cut(conn, pump)
    out = go(conn, pump, at(46), room_c=20.0)
    assert pump.calls == ["dhw", "auto"] and pump.mode == 4
    assert out is not None and state(conn)["forced"] is False
    assert state(conn)["pause_until"] == (at(45) + timedelta(minutes=30)).isoformat()


def test_working_mode_not_heating_no_cut(conn):
    pump = Pump(work=4)
    go(conn, pump, at(0))
    assert go(conn, pump, at(45)) is None
    assert pump.calls == [] and state(conn).get("tail_since") is None


def test_mode_setting_not_auto_no_cut(conn):
    pump = Pump(mode=3)                                  # user wybrał DHW — nie ruszamy
    go(conn, pump, at(0))
    assert go(conn, pump, at(45)) is None
    assert pump.calls == [] and state(conn).get("tail_since") is None


def test_running_test_blocks_cut(conn):
    pump = Pump()
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('zapis', ?, ?, '[]', 0)",
                 (ex.RUNNING, at(0).isoformat()))
    conn.commit()
    go(conn, pump, at(0))
    assert go(conn, pump, at(45)) is None
    assert pump.calls == []


def test_running_test_while_forced_returns_to_auto(conn):
    pump = Pump()
    cut(conn, pump)
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('zapis', ?, ?, '[]', 0)",
                 (ex.RUNNING, at(45).isoformat()))
    conn.commit()
    go(conn, pump, at(46))
    assert pump.calls == ["dhw", "auto"] and state(conn)["forced"] is False


def test_option_disabled_while_forced_returns_to_auto(conn):
    pump = Pump()
    cut(conn, pump)
    go(conn, pump, at(46), OFF)
    assert pump.calls == ["dhw", "auto"] and state(conn)["forced"] is False


def test_forced_waits_then_returns_after_hold(conn):
    pump = Pump()
    cut(conn, pump)
    assert go(conn, pump, at(46)) is None
    assert pump.calls == ["dhw"]
    go(conn, pump, at(47))                               # hold 2 min
    assert pump.calls == ["dhw", "auto"] and pump.mode == 4
    st = state(conn)
    assert st["forced"] is False and st["alerted"] is False
    assert st["pause_until"] == (at(45) + timedelta(minutes=30)).isoformat()
    assert rows(conn)[-1][4] == at(47).isoformat(timespec="seconds")


def test_pause_blocks_next_cut(conn):
    pump = Pump()
    cut(conn, pump)
    go(conn, pump, at(47))                               # powrót do Auto, pauza do at(75)
    n = len(pump.calls)
    for m in (50, 60, 74):
        assert go(conn, pump, at(m)) is None
    assert len(pump.calls) == n
    go(conn, pump, at(76))                               # po pauzie liczymy ogon od nowa
    assert state(conn)["tail_since"] == at(76).isoformat()


def test_unconfirmed_dhw_returns_to_auto_with_alarm(conn):
    pump = Pump(confirm=False)
    cut(conn, pump)
    assert pump.calls == ["dhw", "dhw", "auto"]       # 2 próby dhw; pompa zostaje w Auto, więc powrót potwierdzony od razu
    r = rows(conn)
    assert r[0][4] == ex.NO_CONFIRMATION and r[1][4] == ex.NO_CONFIRMATION
    assert all(x[4] is not None for x in r)
    st = state(conn)
    assert st["forced"] is False and st["pause_until"] == (at(45) + timedelta(minutes=30)).isoformat()
    assert any("nie potwierdz" in m.lower() for _, m in pump.notes)


def test_unconfirmed_auto_keeps_forced_one_alert_and_retries(conn):
    pump = Pump()
    cut(conn, pump)
    pump.confirm = False                                 # pompa przestaje słuchać (mode zostaje 3)
    go(conn, pump, at(47))
    assert state(conn)["forced"] is True and state(conn)["alerted"] is True
    alerts = len(pump.notes)
    go(conn, pump, at(48))
    assert pump.calls.count("auto") == 4                 # ponowienie (2 próby w każdym przebiegu)
    assert len(pump.notes) == alerts                     # jedno powiadomienie na epizod
    pump.confirm = True
    go(conn, pump, at(49))
    assert pump.mode == 4 and state(conn)["forced"] is False and state(conn)["alerted"] is False


def test_unread_value_means_no_write(conn):
    for kwargs in ({"tc": None}, {"setpoint": None}, {"work": None}, {"mode": None}):
        pump = Pump(**kwargs)
        go(conn, pump, at(0))
        assert go(conn, pump, at(45)) is None
        assert pump.calls == [] and state(conn).get("tail_since") is None


def test_recover_on_start_with_forced_returns_to_auto(conn):
    pump = Pump(mode=3)
    dbm.set_setting(conn, tail_cut.STATE_KEY, {"forced": True, "cut_at": at(0).isoformat()})
    out = tail_cut.recover_on_start(conn, ON, at(5), get_state=pump.get_state, writer=pump.writer, notify=pump.notify,
                                    sleep=pump.sleep)
    assert out is not None and pump.calls == ["auto"] and state(conn)["forced"] is False


def test_recover_on_start_without_forced_writes_nothing(conn):
    pump = Pump(mode=3)
    assert tail_cut.recover_on_start(conn, ON, at(5), get_state=pump.get_state, writer=pump.writer, notify=pump.notify,
                                     sleep=pump.sleep) is None
    assert pump.calls == [] and rows(conn) == []


def test_empty_options_fall_back_to_defaults(conn):
    pump = Pump()
    s = {**ON, "tail_delay_min": None, "tail_margin_k": None, "tail_min_room_c": None}
    go(conn, pump, at(0), s)
    assert go(conn, pump, at(119), s) is None
    go(conn, pump, at(120), s)
    assert pump.calls == ["dhw"]


def test_no_collision_with_g13(conn):
    """G13 nie widzi wiersza MODE_WRITE_ID jako swojego, a tail_cut nie zmienia `confirmed_at` cudzych wierszy."""
    pump = Pump()
    ex._write(conn, ON, None, 0.0, -3.0, at(-5), lambda *a: True, max_step=ex.G13_MAX_STEP)     # niepotwierdzony zapis G13
    cut(conn, pump)
    assert [r[4] for r in rows(conn)] == [None, at(45).isoformat(timespec="seconds")]
    g13_settings = {**ON, "g13_peak_enabled": True}
    out = g13_exec.run(conn, g13_settings, at(46), get_numeric=lambda e: -3.0, writer=lambda *a: True, notify=pump.notify)
    assert out is None                                   # G13 dalej czeka na własne potwierdzenie
    assert [r[4] for r in rows(conn)] == [None, at(45).isoformat(timespec="seconds")]
