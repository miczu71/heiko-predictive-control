import sqlite3
from datetime import datetime

from heiko_predictive_control import db as dbm
from heiko_predictive_control.attic import AtticState
from heiko_predictive_control.cycle import run_attic_cycle

MON = datetime(2026, 1, 12)
AC = "climate.ac_test"

SETTINGS = {
    "attic_ac_entity": AC, "attic_temp_entity": "sensor.temp",
    "attic_door_entity": "binary_sensor.door", "attic_power_entity": "sensor.power",
    "attic_window_entities": "binary_sensor.w1, binary_sensor.w2",
    "attic_pause_entity": "input_boolean.pause",
    "tariff_price_entity": "sensor.price", "notify_service": "notify.test",
    "attic_enabled": True, "attic_active_profile": "komfort",
    "attic_comfort_target_c": 22.0, "attic_economy_target_c": 20.5,
    "attic_work_start_hour": 8, "attic_work_end_hour": 16,
    "attic_preheat_lead_min": 45, "attic_preheat_max_min": 120,
}


def at(h, m=0):
    return MON.replace(hour=h, minute=m)


class Env:
    """Atrapa HA: stany encji + rejestr wywołań usług i powiadomień."""

    def __init__(self, ac="off", ac_temp=24.0, temp=16.0, power=0.0, price=0.63,
                 windows=None, service_ok=True):
        self.states = {
            AC: {"state": ac, "attributes": {"temperature": ac_temp}},
            "binary_sensor.door": {"state": "on"},
            "binary_sensor.w1": {"state": "off"}, "binary_sensor.w2": {"state": "off"},
        }
        self.numeric = {"sensor.temp": temp, "sensor.power": power, "sensor.price": price}
        for k, v in (windows or {}).items():
            self.states[k] = v
        self.service_ok = service_ok
        self.calls, self.notes = [], []

    def get_state(self, e):
        return self.states.get(e)

    def get_numeric(self, e):
        return self.numeric.get(e)

    def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        return self.service_ok

    def notify(self, service, title, message):
        self.notes.append((service, title, message))
        return True

    def run(self, conn, settings, now, workday=True, vacation=False):
        return run_attic_cycle(conn, settings, now, workday, vacation,
                               get_state=self.get_state, get_numeric=self.get_numeric,
                               call_service=self.call_service, notify=self.notify)


def make_conn():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    return conn


def test_takeover_calls_climate_service_and_persists_state():
    conn, env = make_conn(), Env(temp=16.0)
    row = env.run(conn, SETTINGS, at(6, 15))
    assert env.calls == [("climate", "set_temperature",
                          {"entity_id": AC, "temperature": 25.5, "hvac_mode": "heat"})]
    assert row["wrote"] == 1 and row["phase"] == "dogrzewanie"
    st = AtticState.from_dict(dbm.get_setting(conn, "attic_ctrl_state"))
    assert st.owned and st.last_cmd["temp"] == 25.5
    assert [n[1] for n in env.notes] == ["Heiko Predictive: AC poddasza"]


def test_disabled_control_never_calls_service_and_keeps_state():
    conn, env = make_conn(), Env(temp=16.0)
    row = env.run(conn, dict(SETTINGS, attic_enabled=False), at(6, 15))
    assert env.calls == [] and env.notes == []
    assert row["phase"] == "dry_run" and row["wrote"] == 0 and row["write_enabled"] == 0
    assert dbm.get_setting(conn, "attic_ctrl_state") is None


def test_failed_write_is_not_persisted_and_notifies_once():
    conn, env = make_conn(), Env(temp=16.0, service_ok=False)
    row = env.run(conn, SETTINGS, at(6, 15))
    assert row["wrote"] == 0
    assert dbm.get_setting(conn, "attic_ctrl_state") is None
    assert [n[1] for n in env.notes] == ["Heiko Predictive: błąd AC poddasza"]
    # następny cykl próbuje ponownie (nie zakłada, że AC już jest włączone)
    env.run(conn, SETTINGS, at(6, 20))
    assert len(env.calls) == 2


def test_open_window_from_ha_last_changed_pauses_ac():
    conn = make_conn()
    dbm.set_setting(conn, "attic_ctrl_state", AtticState(
        owned=True, owned_day="2026-01-12", mode="utrzymanie",
        last_cmd={"hvac": "heat", "temp": 23.5, "ts": at(9, 0).isoformat()}).as_dict())
    env = Env(ac="heat", ac_temp=23.5, temp=21.0, windows={
        "binary_sensor.w2": {"state": "on", "last_changed": "2026-01-12T09:50:00"}})
    row = env.run(conn, SETTINGS, at(10, 0))
    assert env.calls == [("climate", "turn_off", {"entity_id": AC})]
    assert row["phase"] == "pauza_okno" and row["window_open"] == 1


def test_pause_switch_on_blocks_takeover():
    conn, env = make_conn(), Env(temp=16.0)
    env.states["input_boolean.pause"] = {"state": "on"}
    row = env.run(conn, SETTINGS, at(8, 30))
    assert env.calls == [] and row["phase"] == "wstrzymane"


def test_pause_switch_on_turns_off_owned_ac():
    conn = make_conn()
    dbm.set_setting(conn, "attic_ctrl_state", AtticState(
        owned=True, owned_day="2026-01-12", mode="utrzymanie",
        last_cmd={"hvac": "heat", "temp": 23.5, "ts": at(9, 0).isoformat()}).as_dict())
    env = Env(ac="heat", ac_temp=23.5, temp=21.0)
    env.states["input_boolean.pause"] = {"state": "on"}
    row = env.run(conn, SETTINGS, at(10, 0))
    assert env.calls == [("climate", "turn_off", {"entity_id": AC})]
    assert row["phase"] == "wstrzymane"
    assert AtticState.from_dict(dbm.get_setting(conn, "attic_ctrl_state")).owned is False


def test_pause_switch_unavailable_or_missing_is_ignored():
    conn, env = make_conn(), Env(temp=16.0)
    env.states["input_boolean.pause"] = {"state": "unavailable"}
    env.run(conn, SETTINGS, at(8, 30))
    assert len(env.calls) == 1
    conn2, env2 = make_conn(), Env(temp=16.0)          # encji w ogóle nie ma
    env2.run(conn2, SETTINGS, at(8, 30))
    assert len(env2.calls) == 1


def test_pause_switch_off_does_not_block():
    conn, env = make_conn(), Env(temp=16.0)
    env.states["input_boolean.pause"] = {"state": "off"}
    env.run(conn, SETTINGS, at(8, 30))
    assert len(env.calls) == 1


def test_unavailable_ac_no_calls():
    conn, env = make_conn(), Env(ac="unavailable", temp=16.0)
    row = env.run(conn, SETTINGS, at(7, 0))
    assert env.calls == [] and row["phase"] == "brak_ac"


def test_manual_override_notifies_and_is_persisted():
    conn = make_conn()
    dbm.set_setting(conn, "attic_ctrl_state", AtticState(
        owned=True, owned_day="2026-01-12", mode="utrzymanie",
        last_cmd={"hvac": "heat", "temp": 23.5, "ts": at(9, 0).isoformat()}).as_dict())
    env = Env(ac="heat", ac_temp=27.0, temp=21.0)
    row = env.run(conn, SETTINGS, at(9, 30))
    assert env.calls == [] and row["phase"] == "przejecie_reczne"
    assert "ręczną zmianę" in env.notes[0][2]
    st = AtticState.from_dict(dbm.get_setting(conn, "attic_ctrl_state"))
    assert st.override_date == "2026-01-12" and st.owned is False


def test_energy_and_cost_integrated_between_cycles():
    conn, env = make_conn(), Env(ac="heat", temp=22.0, power=1200.0, price=0.5)
    env.run(conn, SETTINGS, at(12, 0))
    row = env.run(conn, SETTINGS, at(12, 5))
    assert abs(row["attic_energy_kwh"] - 0.1) < 1e-9        # 1,2 kW * 5 min
    assert abs(row["attic_cost_pln"] - 0.05) < 1e-9


def test_energy_gap_is_capped_to_half_hour():
    conn, env = make_conn(), Env(ac="heat", temp=22.0, power=1000.0, price=1.0)
    env.run(conn, SETTINGS, at(1, 0))
    row = env.run(conn, SETTINGS, at(12, 0))               # add-on był wyłączony
    assert abs(row["attic_energy_kwh"] - 0.5) < 1e-9


def test_price_falls_back_to_schedule_when_entity_missing():
    conn, env = make_conn(), Env(temp=22.0)
    env.numeric.pop("sensor.price")
    row = env.run(conn, SETTINGS, at(9, 0))                # szczyt G12w
    assert row["price_pln_kwh"] > 1.0


def test_targets_hidden_outside_window():
    conn, env = make_conn(), Env(temp=16.0)
    row = env.run(conn, SETTINGS, at(18, 0))
    assert row["setpoint_komfort"] is None and row["phase"] == "poza_oknem"


def test_migration_adds_columns_to_0_3_0_database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, loop TEXT NOT NULL,
            active_profile TEXT NOT NULL, tariff_peak INTEGER, price_pln_kwh REAL,
            outdoor_temp_c REAL, indoor_temp_c REAL, setpoint_komfort REAL,
            setpoint_ekonomia REAL, coefficient REAL, baseline_cost_today_pln REAL,
            sim_cost_today_komfort_pln REAL, sim_cost_today_ekonomia_pln REAL,
            write_enabled INTEGER NOT NULL);
        INSERT INTO cycles (ts, loop, active_profile, write_enabled)
            VALUES ('2026-09-25T10:00:00', 'attic', 'ekonomia', 0);
    """)
    dbm.migrate(conn)
    dbm.migrate(conn)                                       # idempotentnie
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cycles)")}
    assert {"phase", "offset_c", "wrote", "attic_cost_pln", "planned_start"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1
    env = Env(temp=22.0)
    env.run(conn, SETTINGS, at(9, 0))
    assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 2
