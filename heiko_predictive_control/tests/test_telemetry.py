import json
from datetime import datetime, timedelta, timezone

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import telemetry


@pytest.fixture
def conn(tmp_path):
    c = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(c)
    yield c
    c.close()


def st(eid, state, lc="2026-09-26T10:00:00+00:00", ctx=None):
    return {"entity_id": eid, "state": state, "last_changed": lc, "context": ctx or {}}


SETTINGS = {"day_zone_temp_entities": "sensor.room_a,sensor.room_b", "outdoor_temp_entity": "sensor.out",
            "pump_energy_entity": "sensor.energy"}


def test_collect_values_pump_by_suffix_extras_by_entity_id():
    states = [st("sensor.heiko_heat_pump_compressor_frequency", "35.0"),
              st("sensor.heiko_heat_pump_cop_estimated", "unknown"),
              st("binary_sensor.x_heiko_heat_pump_water_pump_p1", "on"),
              st("sensor.room_a", "21.5"), st("sensor.room_b", "unavailable"),
              st("sensor.out", "12.0"), st("sensor.energy", "3000.5")]
    v = telemetry.collect_values(SETTINGS, states)
    assert v == {"compressor_frequency": 35.0, "water_pump_p1": 1.0, "sensor.room_a": 21.5,
                 "sensor.out": 12.0, "sensor.energy": 3000.5}


def test_store_sample_is_idempotent_within_a_minute(conn):
    now = datetime(2026, 9, 26, 10, 7, 30)
    assert telemetry.store_sample(conn, now, {"a": 1.0, "b": 2.0}) == 2
    telemetry.store_sample(conn, now.replace(second=50), {"a": 5.0})
    rows = conn.execute("SELECT k.name, t.value FROM telemetry t JOIN telemetry_keys k ON k.id = t.key_id "
                        "ORDER BY k.name").fetchall()
    assert [(r["name"], r["value"]) for r in rows] == [("a", 5.0), ("b", 2.0)]


def test_change_source():
    assert telemetry.change_source({"user_id": "u1"}) == "użytkownik HA"
    assert telemetry.change_source({"parent_id": "p1"}) == "automatyzacja/skrypt"
    assert telemetry.change_source({}) == "nieznane" and telemetry.change_source(None) == "nieznane"


def test_detect_changes_baseline_then_changes(conn):
    eid = "number.heiko_heat_pump_dhw_setpoint"
    assert telemetry.detect_changes(conn, [st(eid, "48")]) == []                        # baza, bez wpisu
    assert telemetry.detect_changes(conn, [st(eid, "48")]) == []                        # bez zmian
    out = telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T11:00:00+00:00", {"parent_id": "x"})])
    assert [(r["key"], r["old"], r["new"], r["source"]) for r in out] == [("dhw_setpoint", "48", "58", "automatyzacja/skrypt")]
    assert telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T11:00:00+00:00")]) == []   # ta sama zmiana nie liczy się 2x
    assert conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0] == 1


def test_detect_changes_ignores_unavailable_transitions(conn):
    eid = "number.heiko_heat_pump_dhw_setpoint"
    telemetry.detect_changes(conn, [st(eid, "48")])
    assert telemetry.detect_changes(conn, [st(eid, "unavailable", "2026-09-26T11:00:00+00:00")]) == []
    assert telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T11:05:00+00:00")]) == []   # powrót po unavailable
    assert conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0] == 0


def test_retention_rolls_old_raw_into_hourly(conn):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(days=10, minutes=-7)                         # 10 dni temu, 7 min po pełnej godzinie
    base = int(old.replace(minute=0, second=0).timestamp())
    telemetry.store_sample(conn, old.replace(minute=0), {"x": 10.0})
    telemetry.store_sample(conn, old.replace(minute=15), {"x": 20.0})
    telemetry.store_sample(conn, now - timedelta(hours=1), {"x": 99.0})     # świeża — zostaje
    assert telemetry.apply_retention(conn, now, keep_days=5) == 2
    row = conn.execute("SELECT mean, min, max, n FROM telemetry_hourly").fetchone()
    assert (row["mean"], row["min"], row["max"], row["n"]) == (15.0, 10.0, 20.0, 2)
    assert conn.execute("SELECT hour_ts FROM telemetry_hourly").fetchone()[0] == base
    assert conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 1


def test_changes_summary_counts_by_time_not_text(conn):
    now = datetime(2026, 9, 26, 12, 0)                                  # naiwny czas lokalny jak w main
    local_now = now.astimezone()
    rows = [(local_now.isoformat(), "a"),                                # dziś
            ((local_now - timedelta(days=3)).isoformat(), "b"),          # w 30 dniach
            ((local_now - timedelta(days=60)).isoformat(), "c")]         # starsza
    for ts, key in rows:
        conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) VALUES (?, ?, 'e', '1', '2', 'x')", (ts, key))
    assert telemetry.changes_summary(conn, now) == {"today": 1, "days30": 2, "total": 3}


def test_run_with_fake_states(conn):
    now = datetime(2026, 9, 26, 10, 0)
    out = telemetry.run(conn, SETTINGS, now, get_states=lambda: [st("sensor.room_a", "21.0")])
    assert out == {"samples": 1, "changes": 0, "ok": True}
    assert telemetry.run(conn, SETTINGS, now, get_states=lambda: None)["ok"] is False
    assert json.loads(json.dumps(out)) == out


# ── źródło zmiany z logbooka (kontekst stanu ginie przy następnej ramce z pompy) ───────────────

CHANGED = "2026-09-26T07:55:41.147783+00:00"


def test_source_from_logbook_user_automation_and_none():
    user = [{"when": "2026-09-26T07:55:41.147783+00:00", "state": "on", "context_user_id": "u1",
             "context_event_type": "call_service", "context_service": "turn_on"}]
    assert telemetry.source_from_logbook(user, CHANGED) == "użytkownik HA"
    auto = [{"when": "2026-09-26T07:55:42+00:00", "state": "on", "context_entity_id": "automation.x",
             "context_event_type": "automation_triggered"}]
    assert telemetry.source_from_logbook(auto, CHANGED) == "automatyzacja/skrypt"
    no_ctx = [{"when": CHANGED, "state": "on"}]                               # panel pompy / integracja
    assert telemetry.source_from_logbook(no_ctx, CHANGED) is None
    far = [{"when": "2026-09-26T07:59:00+00:00", "context_user_id": "u1"}]     # wpis spoza tolerancji
    assert telemetry.source_from_logbook(far, CHANGED) is None
    assert telemetry.source_from_logbook(None, CHANGED) is None and telemetry.source_from_logbook([], CHANGED) is None
    assert telemetry.source_from_logbook(user, "nie-data") is None


def test_source_from_logbook_prefers_the_closest_entry():
    entries = [{"when": "2026-09-26T07:55:44+00:00", "context_entity_id": "automation.x"},
               {"when": "2026-09-26T07:55:41.5+00:00", "context_user_id": "u1"}]
    assert telemetry.source_from_logbook(entries, CHANGED) == "użytkownik HA"


def test_detect_changes_uses_logbook_when_state_context_is_empty(conn):
    """Przypadek z produkcji: kliknięcie w UI, a stan ma już pusty kontekst (nadpisany ramką z pompy)."""
    eid = "switch.heiko_heat_pump_backup_heater_hbh"
    telemetry.detect_changes(conn, [st(eid, "off")])
    asked = {}

    def fake_logbook(entity, start, end):
        asked.update(entity=entity, start=start, end=end)
        return [{"when": CHANGED, "state": "on", "context_user_id": "u1", "context_event_type": "call_service"}]

    out = telemetry.detect_changes(conn, [st(eid, "on", CHANGED, {})], get_logbook=fake_logbook)
    assert out[0]["source"] == "użytkownik HA" and asked["entity"] == eid
    assert asked["start"] < CHANGED < asked["end"]


def test_detect_changes_falls_back_to_state_context_then_unknown(conn):
    eid = "number.heiko_heat_pump_dhw_setpoint"
    telemetry.detect_changes(conn, [st(eid, "48")])
    out = telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T11:00:00+00:00", {"parent_id": "p"})],
                                   get_logbook=lambda *a: None)                    # logbook niedostępny
    assert out[0]["source"] == "automatyzacja/skrypt"
    out = telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T12:00:00+00:00", {})], get_logbook=lambda *a: [])
    assert out[0]["source"] == "nieznane"


def test_reattribute_unknown_fixes_only_what_logbook_explains(conn):
    for key, ts in (("dhw_setpoint", CHANGED), ("curve_shift", "2026-09-25T10:00:00+00:00")):
        conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) VALUES (?, ?, 'e.' || ?, '1', '2', 'nieznane')",
                     (ts, key, key))
    conn.commit()

    def fake_logbook(entity, start, end):
        return [{"when": CHANGED, "context_user_id": "u1"}] if entity == "e.dhw_setpoint" else []

    assert telemetry.reattribute_unknown(conn, get_logbook=fake_logbook) == 1
    rows = {r["key"]: r["source"] for r in conn.execute("SELECT key, source FROM param_changes")}
    assert rows == {"dhw_setpoint": "użytkownik HA", "curve_shift": "nieznane"}
    assert telemetry.reattribute_unknown(conn, get_logbook=fake_logbook) == 0      # idempotentne
