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


NO_HISTORY = lambda *a: None          # noqa: E731 — testy nie sięgają do HA (domyślny get_history to prawdziwy recorder)


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
    assert telemetry.detect_changes(conn, [st(eid, "48")], get_history=NO_HISTORY) == []          # baza, bez wpisu
    assert telemetry.detect_changes(conn, [st(eid, "48")], get_history=NO_HISTORY) == []          # bez zmian
    out = telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T11:00:00+00:00", {"parent_id": "x"})],
                                   get_logbook=lambda *a: None, get_history=NO_HISTORY)
    assert [(r["key"], r["old"], r["new"], r["source"]) for r in out] == [("dhw_setpoint", "48", "58", "automatyzacja/skrypt")]
    assert telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T11:00:00+00:00")], get_history=NO_HISTORY) == []   # ta sama zmiana nie liczy się 2x
    assert conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0] == 1


def test_detect_changes_ignores_unavailable_transitions(conn):
    eid = "number.heiko_heat_pump_dhw_setpoint"
    telemetry.detect_changes(conn, [st(eid, "48")], get_history=NO_HISTORY)
    assert telemetry.detect_changes(conn, [st(eid, "unavailable", "2026-09-26T11:00:00+00:00")], get_history=NO_HISTORY) == []
    assert telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T11:05:00+00:00")], get_history=NO_HISTORY) == []   # powrót po unavailable
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
    out = telemetry.run(conn, SETTINGS, now, get_states=lambda: [st("sensor.room_a", "21.0")], get_history=NO_HISTORY)
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
    eid = "select.heiko_heat_pump_backup_priority_dhw"                                   # kanoniczny select slotu 50
    telemetry.detect_changes(conn, [st(eid, "Wyższe")], get_history=NO_HISTORY)
    asked = {}

    def fake_logbook(entity, start, end):
        asked.update(entity=entity, start=start, end=end)
        return [{"when": CHANGED, "state": "Niższe", "context_user_id": "u1", "context_event_type": "call_service"}]

    out = telemetry.detect_changes(conn, [st(eid, "Niższe", CHANGED, {})], get_logbook=fake_logbook, get_history=NO_HISTORY)
    assert out[0]["source"] == "użytkownik HA" and asked["entity"] == eid
    assert asked["start"] < CHANGED < asked["end"]


def test_detect_changes_falls_back_to_state_context_then_unknown(conn):
    eid = "number.heiko_heat_pump_dhw_setpoint"
    telemetry.detect_changes(conn, [st(eid, "48")], get_history=NO_HISTORY)
    out = telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T11:00:00+00:00", {"parent_id": "p"})],
                                   get_logbook=lambda *a: None, get_history=NO_HISTORY)      # logbook niedostępny
    assert out[0]["source"] == "automatyzacja/skrypt"
    out = telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T12:00:00+00:00", {})], get_logbook=lambda *a: [],
                                   get_history=NO_HISTORY)
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


# ── 0.9.3: dziennik liczy zmiany WARTOŚCI (nie odświeżenia last_changed) ──────────────────────────

def _t(h, m, s=0):
    return datetime(2026, 9, 26, h, m, s, tzinfo=timezone.utc)


def test_transitions_ignore_repeats_bad_states_and_report_every_flip():
    hist = [(_t(9, 0), "off"), (_t(9, 30), "unavailable"), (_t(9, 31), "off"),          # przeładowanie: ta sama wartość
            (_t(9, 55, 22), "on"), (_t(9, 55, 42), "off")]                              # szybkie tam i z powrotem
    out = telemetry.transitions(hist, "off", since=_t(9, 0))
    assert [(w, o, n) for w, o, n in out] == [(_t(9, 55, 22), "off", "on"), (_t(9, 55, 42), "on", "off")]
    assert telemetry.transitions([(_t(9, 0), "5"), (_t(10, 0), "5")], "5", since=_t(9, 0)) == []


def test_transitions_first_known_value_is_only_a_reference_and_old_rows_are_skipped():
    assert telemetry.transitions([(_t(9, 0), "unavailable"), (_t(9, 5), "1"), (_t(9, 10), "2")], "unavailable") == \
        [(_t(9, 10), "1", "2")]
    old = [(_t(7, 0), "9"), (_t(9, 0), "1"), (_t(9, 10), "2")]                          # wiersz sprzed `since` nie fałszuje starej wartości
    assert telemetry.transitions(old, "1", since=_t(9, 0)) == [(_t(9, 10), "1", "2")]


def test_detect_changes_reload_of_ha_is_not_a_change(conn):
    """31 encji naraz dostaje nowy `last_changed` po restarcie HA, a wartości są te same — 0 wpisów."""
    eid = "number.heiko_heat_pump_dhw_setpoint"
    telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T10:00:00+00:00")])
    hist = {eid: [(_t(10, 0), "48"), (_t(14, 26), "unavailable"), (_t(14, 27), "48")]}
    out = telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T14:27:00+00:00")], get_logbook=lambda *a: [],
                                   get_history=lambda ids, a, b: hist)
    assert out == [] and conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0] == 0


def test_detect_changes_logs_both_flips_of_a_short_toggle_within_one_cycle(conn):
    eid = "select.heiko_heat_pump_backup_priority_dhw"
    telemetry.detect_changes(conn, [st(eid, "Wyższe", "2026-09-26T13:00:00+00:00")])
    hist = {eid: [(_t(13, 0), "Wyższe"), (_t(14, 34, 22), "Niższe"), (_t(14, 34, 42), "Wyższe")]}
    asked = {}

    def fake_history(ids, start, end):
        asked.update(ids=ids, start=start)
        return hist

    out = telemetry.detect_changes(conn, [st(eid, "Wyższe", "2026-09-26T14:34:42+00:00", {})], get_logbook=lambda *a: [],
                                   get_history=fake_history)
    assert [(r["key"], r["old"], r["new"]) for r in out] == [("backup_heater", "Wyższe", "Niższe"),
                                                              ("backup_heater", "Niższe", "Wyższe")]
    assert asked["ids"] == [eid] and asked["start"].startswith("2026-09-26T13:00:00")
    assert out[0]["ts"].startswith("2026-09-26T14:34:22")


def test_detect_changes_fallback_without_history_logs_only_a_real_difference(conn):
    eid = "number.heiko_heat_pump_dhw_setpoint"
    telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T10:00:00+00:00")])
    same = telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T11:00:00+00:00")], get_history=lambda *a: None)
    diff = telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T12:00:00+00:00")], get_logbook=lambda *a: [],
                                    get_history=lambda *a: None)
    assert same == [] and [(r["old"], r["new"]) for r in diff] == [("48", "58")]


def test_change_during_unavailable_is_not_lost(conn):
    """Encja niedostępna zostaje przy ostatniej znanej wartości — zmiana w tym czasie łapie się po powrocie."""
    eid = "number.heiko_heat_pump_dhw_setpoint"
    telemetry.detect_changes(conn, [st(eid, "48", "2026-09-26T10:00:00+00:00")])
    assert telemetry.detect_changes(conn, [st(eid, "unavailable", "2026-09-26T11:00:00+00:00")]) == []
    hist = {eid: [(_t(10, 0), "48"), (_t(11, 0), "unavailable"), (_t(11, 5), "58")]}
    out = telemetry.detect_changes(conn, [st(eid, "58", "2026-09-26T11:05:00+00:00")], get_logbook=lambda *a: [],
                                   get_history=lambda *a: hist)
    assert [(r["old"], r["new"]) for r in out] == [("48", "58")]


def _fake_rows(conn):
    """Stan produkcyjny z 26.09: 31 odświeżeń po restarcie HA + dwa przełączenia zapisane jako off → off + jedna prawdziwa zmiana."""
    rows = [("2026-09-26T09:10:50+00:00", "dhw_setpoint", "number.heiko_heat_pump_dhw_setpoint", "58", "48", "nieznane"),
            ("2026-09-26T13:57:22+00:00", "backup_heater", "select.heiko_heat_pump_backup_priority_dhw", "Wyższe", "Wyższe", "nieznane"),
            ("2026-09-26T14:26:15+00:00", "backup_heater", "select.heiko_heat_pump_backup_priority_dhw", "Wyższe", "Wyższe", "nieznane"),
            ("2026-09-26T14:26:15+00:00", "anti_leg_program", "switch.heiko_heat_pump_anti_legionella_program", "off", "off", "nieznane"),
            ("2026-09-26T14:34:42+00:00", "backup_heater", "select.heiko_heat_pump_backup_priority_dhw", "Wyższe", "Wyższe", "nieznane")]
    for r in rows:
        conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) VALUES (?, ?, ?, ?, ?, ?)", r)
    conn.commit()


def test_migrate_fake_changes_moves_them_to_legacy_and_restores_real_transitions(conn, monkeypatch):
    _fake_rows(conn)
    monkeypatch.setattr(telemetry.ha_client, "get_logbook", lambda *a: [])
    eid = "select.heiko_heat_pump_backup_priority_dhw"
    hist = {eid: [(_t(12, 0), "Wyższe"), (_t(13, 56, 22), "Niższe"), (_t(13, 57, 22), "Wyższe"),
                  (_t(14, 34, 22), "Niższe"), (_t(14, 34, 42), "Wyższe")]}
    states = [st(eid, "Wyższe"), st("number.heiko_heat_pump_dhw_setpoint", "48")]
    assert telemetry.migrate_fake_changes(conn, states, get_history=lambda *a: hist) is True
    rows = conn.execute("SELECT key, old, new, ts FROM param_changes ORDER BY ts").fetchall()
    assert [(r["key"], r["old"], r["new"]) for r in rows] == [
        ("dhw_setpoint", "58", "48"),                                                      # prawdziwy wpis zostaje
        ("backup_heater", "Wyższe", "Niższe"), ("backup_heater", "Niższe", "Wyższe"),
        ("backup_heater", "Wyższe", "Niższe"), ("backup_heater", "Niższe", "Wyższe")]
    assert conn.execute("SELECT COUNT(*) FROM param_changes_legacy").fetchone()[0] == 4    # 3 × backup_heater + anti_leg
    assert dbm.get_setting(conn, "param_changes_schema") == 2
    before = conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0]
    assert telemetry.migrate_fake_changes(conn, states, get_history=lambda *a: 1 / 0) is True      # idempotentne: nie woła historii
    assert conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0] == before


def test_migrate_fake_changes_retries_later_when_history_is_missing(conn):
    _fake_rows(conn)
    states = [st("select.heiko_heat_pump_backup_priority_dhw", "Wyższe")]
    assert telemetry.migrate_fake_changes(conn, states, get_history=lambda *a: None) is False
    assert conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0] == 5              # nic nie ruszone
    assert conn.execute("SELECT COUNT(*) FROM param_changes_legacy").fetchone()[0] == 0
    assert dbm.get_setting(conn, "param_changes_schema") is None


def test_db_migrate_drops_anti_legionella_and_repoints_backup_heater_in_settings(tmp_path):
    c = dbm.get_conn(str(tmp_path / "old.db"))
    dbm.migrate(c)
    c.execute("DELETE FROM settings WHERE key = 'catalog_schema'")
    dbm.set_setting(c, "advisor_managed_keys", "dhw_setpoint,backup_heater,anti_leg_program")
    dbm.set_setting(c, "catalog_last_seen", {"dhw_setpoint": {"state": "48", "lc": "x"}, "backup_heater": {"state": "on", "lc": "y"},
                                             "anti_leg_setpoint": {"state": "70", "lc": "z"}})
    dbm.migrate(c)
    assert dbm.get_setting(c, "advisor_managed_keys") == "dhw_setpoint,backup_heater"
    assert list(dbm.get_setting(c, "catalog_last_seen")) == ["dhw_setpoint"]
    dbm.set_setting(c, "advisor_managed_keys", "dhw_setpoint,anti_leg_program")            # kolejne migrate już nie rusza
    dbm.migrate(c)
    assert dbm.get_setting(c, "advisor_managed_keys") == "dhw_setpoint,anti_leg_program"
    c.close()
