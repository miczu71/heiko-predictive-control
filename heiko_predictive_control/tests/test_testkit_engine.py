"""E4: silnik testów z definicji (`testkit.engine`) + przełączony `experiments` — złote testy względem `legacy_oracle`,
testy nakładek (definicja bez `writes`), zamrożona definicja i współistnienie z G13/urlopem. Bez sieci, fałszywa pompa."""
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import legacy_oracle as lo
from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import tariff_feed as tf
from heiko_predictive_control.testkit import engine
from heiko_predictive_control.testkit.builtin import BUILTIN
from heiko_predictive_control.testkit.schema import validate

SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
RUN = "number.lazienka_heiko_heat_pump_circulation_pump_p0_run_time"
STOP = "number.lazienka_heiko_heat_pump_circulation_pump_p0_stop_time"
SETTINGS = {"heiko_enabled": True, "heiko_curve_switch_entity": "switch.curve", "heiko_curve_shift_entity": SHIFT,
            "heiko_room_min_c": 18.5, "advisor_notify_service": "notify.x",
            "day_zone_temp_entities": "sensor.salon,sensor.sypialnia", "timezone": "Europe/Warsaw"}
T0 = datetime(2026, 10, 7, 10, 0)          # środa
TZ = ZoneInfo("Europe/Warsaw")

PREHEAT = {
    "key": "preheat_ab", "title": "Preheat A/B", "design": "alternate", "order": ["B", "A"],
    "arms": {"A": {"overrides": {"g13_peak_preheat_shift": 1}}, "B": {"overrides": {"g13_peak_preheat_shift": 0}}},
    "days": {"filter": "peak_day", "count": 20}, "coexist": ["g13_peak"],
    "measures": [{"name": "floor_cost", "window": "13:00→13:00", "per": "hdd"}, {"name": "min_room_at", "at": "21:00"},
                 "safety_trips", "kwh_day"],
    "verdict": {"primary": "floor_cost", "better": "lower",
                "guards": [{"measure": "min_room_at", "op": ">=", "value": 20.5}], "weather_band_c": 3, "min_n": 6},
}


def workday(d: date) -> bool:
    return d.weekday() < 5


class Pump:
    """Fałszywa pompa: przesunięcie krzywej + para P0; `strict=True` — każdy odczyt liczbowy to błąd testu."""

    def __init__(self, shift=0.0, curve="off", pair=(1.0, 6.0), strict=False, accept=True):
        self.shift, self.curve, self.vals, self.strict, self.accept = shift, curve, {RUN: pair[0], STOP: pair[1]}, strict, accept
        self.calls, self.log, self.notes = [], [], []

    def get_state(self, entity):
        return {"state": self.curve} if entity == "switch.curve" else None

    def get_numeric(self, entity):
        if self.strict:
            raise AssertionError(f"odczyt {entity} w teście bez zapisów")
        return self.shift if entity == SHIFT else self.vals.get(entity)

    def writer(self, domain, service, data):
        self.calls.append((domain, service, data))
        self.log.append((data["entity_id"], data["value"]))
        if self.accept:
            if data["entity_id"] == SHIFT:
                self.shift = data["value"]
            else:
                self.vals[data["entity_id"]] = data["value"]
        return self.accept

    def notify(self, service, title, message):
        self.notes.append(message)
        return True


@pytest.fixture(autouse=True)
def no_ha(monkeypatch):
    states = [{"entity_id": RUN, "state": "1"}, {"entity_id": STOP, "state": "6"}]
    monkeypatch.setattr(ex.ha_client, "get_all_states", lambda: states)
    monkeypatch.setattr(ex, "_day_kwh", lambda c, s, d, n: None)


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    engine.seed_builtin(c)
    return c


def _start(conn, pump, key="zapis", now=T0, settings=SETTINGS):
    return ex.start(conn, settings, key, now, pump.get_state, pump.get_numeric, workday)


def _tick(conn, pump, now, settings=SETTINGS):
    return ex.tick(conn, settings, now, pump.get_numeric, pump.writer, pump.notify)


def _test(conn):
    return conn.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 1").fetchone()


def _d4b(conn):
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('bezwladnosc', ?, ?, '[]', 0)",
                 (ex.DONE, "2026-10-01T10:00:00"))
    conn.commit()


def _add_def(conn, defn):
    conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES (?, ?, 0, 'x', 'x')",
                 (defn["key"], json.dumps(defn)))
    conn.commit()


def _fill_p0(conn, steps):
    """Telemetria (rozrzut 1/6 = 1.0, 5/5 = 0.5) i temp. zewn. dla dób testu P0."""
    spreads = {"1/6": 1.0, "5/5": 0.5}
    for step in steps[:-1]:
        label = f"{step['value']:g}/{step['stop']:g}"
        day = datetime.fromisoformat(step["at"])
        for i in range(16):
            ts = int((day + timedelta(minutes=15 * i)).replace(tzinfo=TZ).timestamp())
            for name, value in (("working_mode", 0.0), ("sensor.salon", 21 + spreads[label] / 2),
                                ("sensor.sypialnia", 21 - spreads[label] / 2)):
                conn.execute("INSERT OR IGNORE INTO telemetry_keys (name) VALUES (?)", (name,))
                key = conn.execute("SELECT id FROM telemetry_keys WHERE name = ?", (name,)).fetchone()["id"]
                conn.execute("INSERT OR REPLACE INTO telemetry (ts, key_id, value) VALUES (?, ?, ?)", (ts, key, value))
        conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, outdoor_temp_c) VALUES (?, 'heiko', 'ekonomia', 0, 8)",
                     ((day + timedelta(hours=12)).isoformat(timespec="seconds"), ))
    conn.commit()


# ── czyste funkcje silnika ──────────────────────────────────────────────────────────────────────────────────

def test_engine_plan_equals_legacy_for_builtins():
    feed = None
    now = datetime(2026, 10, 7, 5, 0)
    for key, base, legacy_base in (("zapis", {"curve_shift": 0.0}, 0.0), ("zapis", {"curve_shift": 3.0}, 3.0),
                                   ("bezwladnosc", {"curve_shift": 0.0}, 0.0),
                                   ("p0_ab", {"p0_run": 1.0, "p0_stop": 6.0}, (1.0, 6.0)),
                                   ("p0_ab", {"p0_run": 5.0, "p0_stop": 5.0}, (5.0, 5.0))):
        steps, arms = engine.plan(BUILTIN[key], base, now, workday, feed)
        assert steps == lo.build_schedule(key, legacy_base, now, workday, feed), (key, base)
        assert engine.validate_steps(steps, legacy_base) is None
        assert arms and all(a in BUILTIN[key]["arms"] for a in arms.values())


def test_to_steps_merges_p0_pair_and_maps_curve():
    plan_steps = [{"at": "a", "param": "p0_run", "value": 5.0}, {"at": "a", "param": "p0_stop", "value": 5.0},
                  {"at": "b", "param": "p0_run", "value": 1.0}, {"at": "b", "param": "p0_stop", "value": 6.0}]
    assert engine.to_steps(plan_steps) == [{"at": "a", "value": 5.0, "stop": 5.0}, {"at": "b", "value": 1.0, "stop": 6.0}]
    assert engine.to_steps([{"at": "a", "param": "curve_shift", "value": 1.0}]) == [{"at": "a", "value": 1.0}]


def test_validate_steps_keeps_legacy_messages():
    assert "zakresem" in engine.validate_steps([{"at": "x", "value": -5}, {"at": "y", "value": -4}], -4)
    assert "krok" in engine.validate_steps([{"at": "x", "value": 2}, {"at": "y", "value": 0}], 0)
    assert "powrotem" in engine.validate_steps([{"at": "x", "value": 1}], 0)
    bad = [{"at": "x", "value": 1.0, "stop": 5.0}, {"at": "y", "value": 1.0, "stop": 6.0}]
    assert "spoza dozwolonych" in engine.validate_steps(bad, (1.0, 6.0))
    assert "powrotem" in engine.validate_steps([{"at": "x", "value": 5.0, "stop": 5.0}], (1.0, 6.0))


def test_has_writes():
    assert engine.has_writes(BUILTIN["zapis"]) and engine.has_writes(BUILTIN["p0_ab"])
    assert not engine.has_writes(PREHEAT)


def test_end_at_overlay_only_adds_window_end_after_midnight():
    arms = {"2026-10-07": "B", "2026-10-08": "A"}
    assert engine.end_at(PREHEAT, arms) == datetime(2026, 10, 9, 13, 0)             # 13:00→13:00 przez północ
    plain = {**PREHEAT, "measures": ["safety_trips"]}
    assert engine.end_at(plain, arms) == datetime(2026, 10, 9, 0, 0)
    same_day = {**PREHEAT, "measures": [{"name": "floor_cost", "window": "06:00→22:00"}]}
    assert engine.end_at(same_day, arms) == datetime(2026, 10, 9, 0, 0)
    assert engine.end_at(PREHEAT, {}) is None


def test_seed_builtin_and_lookup(conn):
    assert [d["key"] for d in engine.list_defs(conn)] == ["zapis", "bezwladnosc", "p0_ab"]
    assert engine.get_def(conn, "p0_ab") == BUILTIN["p0_ab"] and engine.get_def(conn, "nie_ma") is None
    _add_def(conn, PREHEAT)
    assert [d["key"] for d in engine.list_defs(conn)] == ["zapis", "bezwladnosc", "p0_ab", "preheat_ab"]
    engine.seed_builtin(conn)
    engine.seed_builtin(conn)
    assert conn.execute("SELECT COUNT(*) FROM test_defs").fetchone()[0] == 4


def test_get_def_falls_back_to_builtin_without_seed():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    assert engine.get_def(c, "zapis") == BUILTIN["zapis"]


def test_backfilled_old_row_has_frozen_def_equal_builtin():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    c.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('p0_ab', ?, 'x', '[]', 1)", (ex.DONE,))
    c.execute("UPDATE tests SET def_json = NULL, def_key = NULL")
    c.commit()
    assert engine.frozen_def(_test(c)) == BUILTIN["p0_ab"]
    dbm.migrate(c)
    assert json.loads(_test(c)["def_json"]) == BUILTIN["p0_ab"]


# ── start z definicji == legacy ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,pump_kw,now,legacy_base", [
    ("zapis", {}, T0, 0.0),
    ("zapis", {"shift": 3.0}, T0, 3.0),
    ("bezwladnosc", {"curve": "on"}, datetime(2026, 10, 7, 5, 0), 0.0),
    ("p0_ab", {"curve": "on"}, T0, (1.0, 6.0)),
    ("p0_ab", {"curve": "on", "pair": (5.0, 5.0)}, T0, (5.0, 5.0)),
])
def test_start_gives_the_legacy_schedule_and_baseline(conn, key, pump_kw, now, legacy_base):
    _d4b(conn)
    pump = Pump(**pump_kw)
    assert _start(conn, pump, key, now)["ok"] and pump.calls == []
    row = _test(conn)
    feed = tf.for_settings(conn, SETTINGS)
    assert json.loads(row["schedule"]) == lo.build_schedule(key, legacy_base, now, workday, feed)
    assert row["baseline"] == (legacy_base[0] if isinstance(legacy_base, tuple) else legacy_base)
    assert row["def_key"] == key and json.loads(row["def_json"]) == BUILTIN[key] and row["status"] == ex.RUNNING
    assert json.loads(row["arms_json"]) == engine.plan(BUILTIN[key], _base(key, legacy_base), now, workday, feed)[1]


def _base(key, legacy_base):
    if isinstance(legacy_base, tuple):
        return {"p0_run": legacy_base[0], "p0_stop": legacy_base[1]}
    return {"curve_shift": legacy_base}


def test_start_blocked_matches_legacy_reasons(conn):
    assert "włączonej krzywej" in _start(conn, Pump(curve="off"), "bezwladnosc")["error"]
    assert "Bezwładność" in _start(conn, Pump(curve="on"), "p0_ab")["error"]
    assert _start(conn, Pump(), "xyz") == {"ok": False, "error": "nieznany test"}


# ── pełne przebiegi przez tick == legacy ────────────────────────────────────────────────────────────────────

def test_p0_full_run_writes_and_result_equal_legacy(conn):
    pump = Pump(curve="on")
    _d4b(conn)
    assert _start(conn, pump, "p0_ab")["ok"]
    steps = json.loads(_test(conn)["schedule"])
    _fill_p0(conn, steps)
    for step in steps:
        now = datetime.fromisoformat(step["at"])
        for minutes in (0, 2, 3):
            _tick(conn, pump, now + timedelta(minutes=minutes))
    row = _test(conn)
    assert row["status"] == ex.DONE
    assert pump.log == [(RUN, 5.0), (STOP, 5.0), (RUN, 1.0), (STOP, 6.0)] * 3
    legacy = lo._result(conn, row, datetime.fromisoformat(row["ended"]), SETTINGS)
    assert json.loads(row["result"]) == json.loads(json.dumps(legacy))
    assert json.loads(row["result"])["verdict"] == "5/5"
    assert "werdykt 5/5" in pump.notes[-1] and pump.notes[-1] == lo._done_message(row, legacy)


def test_inertia_full_run_writes_and_result_equal_legacy(conn):
    pump = Pump(curve="on")
    assert _start(conn, pump, "bezwladnosc", datetime(2026, 10, 7, 5, 0))["ok"]
    steps = json.loads(_test(conn)["schedule"])
    for step in steps:
        now = datetime.fromisoformat(step["at"])
        for minutes in (0, 2, 3):
            _tick(conn, pump, now + timedelta(minutes=minutes))
    row = _test(conn)
    assert row["status"] == ex.DONE and pump.shift == 0.0
    assert [c[2]["value"] for c in pump.calls] == [s["value"] for s in steps]
    legacy = lo._result(conn, row, datetime.fromisoformat(row["ended"]), SETTINGS)
    assert json.loads(row["result"]) == json.loads(json.dumps(legacy))
    assert pump.notes[-1] == lo._done_message(row, legacy)


def test_write_test_result_and_message_equal_legacy(conn):
    pump = Pump()
    _start(conn, pump)
    for m in (0, 5, 10, 15):
        _tick(conn, pump, T0 + timedelta(minutes=m))
    row = _test(conn)
    legacy = lo._result(conn, row, datetime.fromisoformat(row["ended"]), SETTINGS)
    assert row["status"] == ex.DONE and json.loads(row["result"]) == json.loads(json.dumps(legacy))
    assert pump.notes[-1] == lo._done_message(row, legacy)


def _row(conn, kind, result, ended="2026-10-04T10:00:00"):
    conn.execute("INSERT INTO tests (kind, status, started, ended, schedule, baseline, result) "
                 "VALUES (?, ?, 'x', ?, '[]', 0, ?)", (kind, ex.DONE, ended, json.dumps(result)))
    conn.commit()


@pytest.mark.parametrize("kind,result", [
    ("zapis", {"writes": 2, "confirmed": 2}), ("zapis", {"writes": 2, "confirmed": 1}), ("zapis", {"writes": 0, "confirmed": 0}),
    ("bezwladnosc", {"writes": 4, "confirmed": 4, "excitation": {"sufficient": True}, "refit": {"identified": True}}),
    ("bezwladnosc", {"writes": 4, "confirmed": 4, "excitation": {"sufficient": True}, "refit": {"identified": False}}),
    ("bezwladnosc", {"writes": 4, "confirmed": 4, "excitation": {"sufficient": False}}),
    ("p0_ab", {"writes": 6, "confirmed": 6, "verdict": "5/5", "confidence": "wysoka"}),
    ("p0_ab", {"writes": 6, "confirmed": 6, "verdict": "1/6", "confidence": "niska"}),
    ("p0_ab", {"writes": 6, "confirmed": 6, "verdict": None, "confidence": None}),
])
def test_passed_equals_legacy(conn, kind, result):
    _row(conn, kind, result)
    assert engine.passed(conn, BUILTIN[kind]) == lo._passed(conn, kind)


@pytest.mark.parametrize("kind,extra", [
    ("zapis", {}), ("bezwladnosc", {}),
    ("p0_ab", {"verdict": None, "confidence": None}),
    ("p0_ab", {"verdict": "5/5", "gain_c": 0.6, "confidence": "wysoka", "extra_kwh_day": 0.4}),
    ("p0_ab", {"verdict": "1/6", "gain_c": -0.2, "confidence": "niska"}),
])
def test_done_message_equals_legacy(kind, extra):
    result = {"writes": 2, "confirmed": 2, **extra}
    test = {"kind": kind}
    assert engine.done_message(BUILTIN[kind], test, result) == lo._done_message(test, result)


def test_generic_definition_passed_and_message():
    defn = {**PREHEAT}
    result = {"writes": 0, "confirmed": 0, "verdict": {"winner": "B", "confidence": "wysoka"}}
    assert engine.done_message(defn, {"kind": "preheat_ab"}, result) == \
        "Test „Preheat A/B” zakończony: B (pewność wysoka). Wynik w zakładce Testy."
    none = {"verdict": {"winner": None, "confidence": "niska"}}
    assert "brak rozstrzygnięcia (pewność niska)" in engine.done_message(defn, {"kind": "preheat_ab"}, none)


# ── definicja bez `writes` (nakładka) ───────────────────────────────────────────────────────────────────────

def test_overlay_test_starts_with_g13_on_and_writing_definitions_do_not(conn):
    _add_def(conn, PREHEAT)
    pump = Pump(curve="on")
    g13 = {**SETTINGS, "g13_peak_enabled": True}
    assert _start(conn, pump, "preheat_ab", settings=g13)["ok"]
    ex.abort(conn, g13, T0, writer=pump.writer, notify=pump.notify)
    res = _start(conn, pump, "zapis", settings=g13)
    assert res["ok"] is False and "Szczyt G13" in res["error"]
    assert pump.calls == []


def test_overlay_without_g13_coexist_is_blocked_by_g13(conn):
    _add_def(conn, {k: v for k, v in PREHEAT.items() if k not in ("coexist",)} | {"key": "preheat_no_coexist"})
    res = _start(conn, Pump(), "preheat_no_coexist", settings={**SETTINGS, "g13_peak_enabled": True})
    assert res["ok"] is False and "Szczyt G13" in res["error"]


def test_vacation_blocks_every_definition(conn):
    _add_def(conn, PREHEAT)
    settings = {**SETTINGS, "vacation_enabled": True}
    for key in ("preheat_ab", "zapis"):
        res = _start(conn, Pump(), key, settings=settings)
        assert res["ok"] is False and "urlopowy" in res["error"]


def test_overlay_test_start_state(conn):
    _add_def(conn, PREHEAT)
    pump = Pump(curve="on")
    assert _start(conn, pump, "preheat_ab")["ok"]
    row = _test(conn)
    assert json.loads(row["schedule"]) == [] and row["baseline"] == 0.0 and pump.calls == []
    arms = json.loads(row["arms_json"])
    assert len(arms) == 20 and list(arms.values()) == ["B", "A"] * 10 and next(iter(arms)) == "2026-10-07"
    assert all(workday(date.fromisoformat(d)) for d in arms)


def test_overlay_tick_never_reads_pump_nor_writes(conn):
    _add_def(conn, PREHEAT)
    pump = Pump(curve="on")
    _start(conn, pump, "preheat_ab")
    pump.strict = True
    out = _tick(conn, pump, T0 + timedelta(hours=3))
    assert out["status"] == ex.RUNNING and out["arm"] == "B"
    assert _tick(conn, pump, T0 + timedelta(days=1, hours=2))["arm"] == "A"
    assert pump.calls == [] and conn.execute("SELECT COUNT(*) FROM pump_writes").fetchone()[0] == 0


def test_arm_for_day_and_active_overlay(conn):
    _add_def(conn, PREHEAT)
    assert engine.active_overlay(conn, T0) is None                                   # brak testu
    _start(conn, Pump(curve="on"), "preheat_ab")
    assert engine.arm_for_day(_test(conn), date(2026, 10, 7)) == "B"
    assert engine.arm_for_day(_test(conn), date(2026, 10, 8)) == "A"
    assert engine.arm_for_day(_test(conn), date(2026, 10, 10)) is None               # sobota
    assert engine.active_overlay(conn, T0) == {"overrides": {"g13_peak_preheat_shift": 0}}
    assert engine.active_overlay(conn, T0 + timedelta(days=1)) == {"overrides": {"g13_peak_preheat_shift": 1}}
    assert engine.active_overlay(conn, datetime(2026, 10, 10, 12, 0)) is None         # poza dobami
    assert engine.active_overlay(conn, datetime(2026, 12, 1, 12, 0)) is None
    ex.abort(conn, SETTINGS, T0, notify=Pump().notify)
    assert engine.active_overlay(conn, T0) is None                                   # test zakończony


def test_effective_does_not_mutate_settings(conn):
    _add_def(conn, PREHEAT)
    _start(conn, Pump(curve="on"), "preheat_ab")
    settings = {"g13_peak_preheat_shift": 1, "x": 5}
    eff = engine.effective(conn, settings, T0)
    assert eff == {"g13_peak_preheat_shift": 0, "x": 5} and settings == {"g13_peak_preheat_shift": 1, "x": 5}
    assert engine.effective(conn, settings, datetime(2026, 10, 10, 12, 0)) == settings


def test_overlay_test_finishes_after_end_at_with_evaluated_result(conn):
    _add_def(conn, PREHEAT)
    pump = Pump(curve="on")
    _start(conn, pump, "preheat_ab")
    pump.strict = True
    row = _test(conn)
    end = engine.end_at(engine.frozen_def(row), json.loads(row["arms_json"]))
    assert end == datetime.fromisoformat(max(json.loads(row["arms_json"]))) + timedelta(days=1, hours=13)
    assert _tick(conn, pump, end - timedelta(minutes=1))["status"] == ex.RUNNING
    out = _tick(conn, pump, end)
    assert out["status"] == ex.DONE and _test(conn)["status"] == ex.DONE
    result = json.loads(_test(conn)["result"])
    assert result["verdict"]["winner"] is None and set(result["arms"]) == {"A", "B"} and result["writes"] == 0
    assert "Preheat A/B" in pump.notes[-1] and "zakończony" in pump.notes[-1]
    assert pump.calls == []


def test_restart_keeps_overlay_test_but_aborts_writing_test(conn):
    _add_def(conn, PREHEAT)
    pump = Pump(curve="on")
    _start(conn, pump, "preheat_ab")
    assert ex.recover_on_start(conn, SETTINGS, T0 + timedelta(hours=1), pump.writer, pump.notify) is None
    assert ex.running_test(conn)["status"] == ex.RUNNING
    ex.abort(conn, SETTINGS, T0, notify=pump.notify)
    assert _start(conn, Pump(), "zapis")["ok"]
    out = ex.recover_on_start(conn, SETTINGS, T0 + timedelta(hours=1), pump.writer, pump.notify)
    assert out["status"] == ex.ABORTED and ex.running_test(conn) is None


def test_writing_test_semantics(conn):
    _add_def(conn, PREHEAT)
    assert ex.writing_test(conn) is None
    _start(conn, Pump(curve="on"), "preheat_ab")
    assert ex.running_test(conn) is not None and ex.writing_test(conn) is None
    ex.abort(conn, SETTINGS, T0, notify=Pump().notify)
    _start(conn, Pump(), "zapis")
    assert ex.writing_test(conn)["kind"] == "zapis"
    ex.abort(conn, SETTINGS, T0, notify=Pump().notify)
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('stary', ?, 'x', '[]', 0)", (ex.RUNNING,))
    conn.commit()
    assert ex.writing_test(conn)["kind"] == "stary"                                  # brak def_json = jak legacy


def test_one_test_at_a_time_includes_overlay_tests(conn):
    _add_def(conn, PREHEAT)
    pump = Pump(curve="on")
    assert _start(conn, pump, "preheat_ab")["ok"]
    assert _start(conn, pump, "zapis")["error"] == "inny test jest w toku"


# ── zakres E4: nastawy w `writes`, zamrożona definicja ──────────────────────────────────────────────────────

SLOT = {"key": "slot_test", "title": "Slot", "design": "single", "days": {"filter": "all", "count": 1},
        "arms": {"A": {"writes": [{"param": "slot52", "value": 30}]}}}


def test_mixed_params_in_writes_are_refused_by_start(conn):
    mixed = {**SLOT, "key": "mix", "arms": {"A": {"writes": [{"param": "slot52", "value": 30}, {"param": "dhw", "value": 40}]}}}
    assert validate(mixed) == []
    _add_def(conn, mixed)
    res = _start(conn, Pump(), "mix")
    assert res["ok"] is False and "tylko jedną nastawę" in res["error"]
    assert ex.running_test(conn) is None


def test_out_of_allowlist_value_is_rejected_by_write_not_by_engine(conn):
    jump = {"key": "skok", "title": "Skok", "design": "single", "days": {"filter": "all", "count": 1},
            "arms": {"A": {"writes": [{"param": "curve_shift", "value": 2}]}}}
    _add_def(conn, jump)
    res = _start(conn, Pump(), "skok")
    assert res["ok"] is False and "większy niż" in res["error"]                      # walidacja kroków przy starcie
    pump = Pump()                                                                    # a gdyby ją ominąć, zapis odmówi
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline, def_key, def_json) VALUES "
                 "('skok', ?, ?, ?, 0, 'skok', ?)", (ex.RUNNING, T0.isoformat(),
                                                      json.dumps([{"at": T0.isoformat(), "value": 2.0}]), json.dumps(jump)))
    conn.commit()
    with pytest.raises(ValueError):
        _tick(conn, pump, T0)
    assert pump.calls == []


def test_edit_of_definition_after_start_does_not_change_running_test(conn):
    pump = Pump()
    _start(conn, pump)
    conn.execute("UPDATE test_defs SET def_json = ? WHERE key = 'zapis'",
                 (json.dumps({**BUILTIN["zapis"], "title": "Zmieniony", "measures": []}),))
    conn.commit()
    assert engine.frozen_def(_test(conn)) == BUILTIN["zapis"]
    for m in (0, 5, 10, 15):
        _tick(conn, pump, T0 + timedelta(minutes=m))
    assert _test(conn)["status"] == ex.DONE and "Test „Test zapisu (poza sezonem)” zakończony" in pump.notes[-1]


def test_overview_lists_every_definition_with_legacy_card_shape(conn):
    _add_def(conn, PREHEAT)
    pump = Pump()
    data = ex.overview(conn, SETTINGS, T0, pump.get_state, pump.get_numeric, workday)
    assert [k["key"] for k in data["kinds"]] == ["zapis", "bezwladnosc", "p0_ab", "preheat_ab"]
    for card in data["kinds"]:
        assert {"key", "title", "description", "measure", "schedule", "blocked", "passed", "abort_conditions"} <= set(card)
    pre = data["kinds"][3]
    assert pre["schedule"] == [] and pre["measure"] and pre["abort_conditions"] == ex.ABORT_CONDITIONS
    assert data["kinds"][0]["measure"] == ex.KINDS["zapis"].measure
    assert data["kinds"][2]["abort_conditions"] == ex.P0_ABORT_CONDITIONS


# ── E7a: slot52 i dhw jako cel testu ────────────────────────────────────────────────────────────────────────

FIXTURES = os.path.join(os.path.dirname(__file__))
SLOT_ENT = "number.heiko_heat_pump_backup_source_start_delay"
DHW_ENT = "number.heiko_heat_pump_dhw_setpoint"


def _fixture(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _ent_pump(monkeypatch, slot=55.0, dhw=48.0, present=True):
    states = ([{"entity_id": SLOT_ENT, "state": "1"}, {"entity_id": DHW_ENT, "state": "1"}] if present else [])
    monkeypatch.setattr(ex.ha_client, "get_all_states", lambda: states)
    pump = Pump(curve="off")
    pump.vals.update({SLOT_ENT: slot, DHW_ENT: dhw})
    return pump


def _single(key, param, value):
    return {"key": key, "title": key, "design": "single", "days": {"filter": "all", "count": 1},
            "arms": {"A": {"writes": [{"param": param, "value": value}]}}}


def test_writes_target_slot52_dhw_mixed_and_missing():
    assert engine.writes_target(_fixture("ah_delay_ab")) == ("slot52", None)
    assert engine.writes_target(_fixture("dhw_setpoint_ab")) == ("dhw", None)
    mixed = {"arms": {"A": {"writes": [{"param": "slot52", "value": 30}, {"param": "dhw", "value": 40}]}}}
    assert engine.writes_target(mixed)[0] is None and "tylko jedną nastawę" in engine.writes_target(mixed)[1]
    shift_dhw = {"arms": {"A": {"writes": [{"param": "curve_shift", "value": 1}]},
                          "B": {"writes": [{"param": "dhw", "value": 40}]}}}
    assert "tylko jedną nastawę" in engine.writes_target(shift_dhw)[1]
    no_write = {"arms": {"A": {"writes": [{"param": "dhw", "value": 40}]}, "B": {}}}
    assert engine.writes_target(no_write)[0] is None and "wariant bez zapisu" in engine.writes_target(no_write)[1]


def test_to_steps_and_validate_steps_for_slot52_and_dhw():
    assert engine.to_steps([{"at": "a", "param": "slot52", "value": 60.0}, {"at": "b", "param": "dhw", "value": 45.0}]) == [
        {"at": "a", "value": 60.0}, {"at": "b", "value": 45.0}]
    ok = [{"at": "a", "value": 60.0}, {"at": "b", "value": 55.0}]
    assert engine.validate_steps(ok, 55.0, "slot52") is None
    assert "poza dozwolonymi regułami nastawy slot52" in engine.validate_steps([{"at": "a", "value": 65.0}, {"at": "b", "value": 60.0}], 60.0, "slot52")
    assert "krok 55 → 45 niedozwolony" == engine.validate_steps([{"at": "a", "value": 45.0}, {"at": "b", "value": 55.0}], 55.0, "slot52")
    assert "nie kończy się powrotem" in engine.validate_steps([{"at": "a", "value": 60.0}], 55.0, "slot52")
    assert engine.validate_steps([{"at": "a", "value": 45.0}, {"at": "b", "value": 48.0}], 48.0, "dhw") is None
    assert "poza dozwolonymi regułami nastawy dhw" in engine.validate_steps([{"at": "a", "value": 14.0}, {"at": "b", "value": 17.0}], 17.0, "dhw")


@pytest.mark.parametrize("key,ent,base,other", [("ah_delay_ab", SLOT_ENT, 55.0, 60.0), ("dhw_setpoint_ab", DHW_ENT, 48.0, 45.0)])
def test_new_definition_starts_with_base_and_variant_values_and_returns_to_base(conn, monkeypatch, key, ent, base, other):
    pump = _ent_pump(monkeypatch, slot=base, dhw=base)
    _add_def(conn, _fixture(key))
    res = _start(conn, pump, key)
    assert res["ok"], res
    test = _test(conn)
    values = [s["value"] for s in json.loads(test["schedule"])]
    assert set(values) == {base, other} and values[-1] == base and test["baseline"] == base
    assert pump.calls == []


@pytest.mark.parametrize("key,ent", [("ah_delay_ab", SLOT_ENT), ("dhw_setpoint_ab", DHW_ENT)])
def test_new_definition_blocks(conn, monkeypatch, key, ent):
    _add_def(conn, _fixture(key))
    pump = _ent_pump(monkeypatch)
    assert "Szczyt G13" in _start(conn, pump, key, settings={**SETTINGS, "g13_peak_enabled": True})["error"]
    absent = _ent_pump(monkeypatch, present=False)
    assert "nie mogę odczytać" in _start(conn, absent, key)["error"]
    no_read = _ent_pump(monkeypatch)
    no_read.vals.pop(ent)
    assert "nie mogę odczytać" in _start(conn, no_read, key)["error"]
    assert ex.running_test(conn) is None


def test_slot52_base_60_blocks_start_and_dhw_base_17_is_rejected(conn, monkeypatch):
    _add_def(conn, _fixture("ah_delay_ab"))
    _add_def(conn, _fixture("dhw_setpoint_ab"))
    pump = _ent_pump(monkeypatch, slot=60.0, dhw=17.0)
    res = _start(conn, pump, "ah_delay_ab")
    assert res["ok"] is False and "slot52" in res["error"]
    res = _start(conn, pump, "dhw_setpoint_ab")
    assert res["ok"] is False and "dhw" in res["error"]


def _run(conn, pump, key, ent):
    """Start i przebieg testu tickami co 5 min do końca; zwraca (wpisy zapisów, ostatni tick)."""
    assert _start(conn, pump, key)["ok"]
    steps = json.loads(_test(conn)["schedule"])
    now, last = T0, None
    for step in steps:
        now = max(now, datetime.fromisoformat(step["at"]))
        last = _tick(conn, pump, now)
        now += timedelta(minutes=3)
        last = _tick(conn, pump, now)                      # potwierdzenie (ramka po ≥ 2 min)
        now += timedelta(minutes=3)
        _tick(conn, pump, now)
    return steps, last


@pytest.mark.parametrize("key,ent,base", [("ah_delay_ab", SLOT_ENT, 55.0), ("dhw_setpoint_ab", DHW_ENT, 48.0)])
def test_new_definition_full_run_writes_only_its_entity_and_returns_to_base(conn, monkeypatch, key, ent, base):
    pump = _ent_pump(monkeypatch, slot=base, dhw=base)
    _add_def(conn, _fixture(key))
    _run(conn, pump, key, ent)
    for _ in range(3):
        _tick(conn, pump, T0 + timedelta(days=30))
    assert pump.calls and {e for e, _ in pump.log} == {ent}
    assert pump.vals[ent] == base
    assert {r["entity_id"] for r in conn.execute("SELECT entity_id FROM pump_writes")} == {ent}
    assert _test(conn)["status"] in (ex.DONE, ex.ABORTED)
    assert _test(conn)["status"] == ex.DONE, _test(conn)["abort_reason"]


def test_manual_dhw_change_aborts_without_write(conn, monkeypatch):
    pump = _ent_pump(monkeypatch, dhw=48.0)
    _add_def(conn, _fixture("dhw_setpoint_ab"))
    assert _start(conn, pump, "dhw_setpoint_ab")["ok"]
    first = datetime.fromisoformat(json.loads(_test(conn)["schedule"])[0]["at"])
    pump.vals[DHW_ENT] = 50.0                                # ręczna zmiana (np. automatyzacja PV) przed krokiem
    res = _tick(conn, pump, max(T0, first))
    assert res["status"] == ex.ABORTED and "ręczna zmiana nastawy CWU" in _test(conn)["abort_reason"]
    assert pump.calls == []


@pytest.mark.parametrize("key,ent,base,changed", [("ah_delay_ab", SLOT_ENT, 55.0, 60.0), ("dhw_setpoint_ab", DHW_ENT, 48.0, 45.0)])
def test_user_abort_and_restart_return_to_base(conn, monkeypatch, key, ent, base, changed):
    for how in ("abort", "restart"):
        c = dbm.get_conn(":memory:")
        dbm.migrate(c)
        _add_def(c, _fixture(key))
        pump = _ent_pump(monkeypatch, slot=base, dhw=base)
        assert _start(c, pump, key)["ok"]
        steps = json.loads(_test(c)["schedule"])
        step = next(s for s in steps if s["value"] != base)
        now = T0
        for s in steps[:steps.index(step) + 1]:                      # kroki po kolei, aż do pierwszego wariantu ≠ baza
            now = max(now, datetime.fromisoformat(s["at"]))
            _tick(c, pump, now)
            now += timedelta(minutes=10)
            _tick(c, pump, now)
        assert pump.log and pump.vals[ent] == step["value"] == changed   # test faktycznie zapisał wariant
        now += timedelta(minutes=3)
        _tick(c, pump, now)
        now += timedelta(minutes=1)
        if how == "abort":
            assert ex.abort(c, SETTINGS, now, writer=pump.writer, notify=pump.notify)["ok"]
        else:
            assert ex.recover_on_start(c, SETTINGS, now, writer=pump.writer, notify=pump.notify)["status"] == ex.ABORTED
        assert pump.vals[ent] == base
        assert {e for e, _ in pump.log} == {ent}
        assert _test(c)["status"] == ex.ABORTED


def test_overview_shows_new_definitions_with_block_reason(conn, monkeypatch):
    pump = _ent_pump(monkeypatch)
    _add_def(conn, _fixture("ah_delay_ab"))
    data = ex.overview(conn, {**SETTINGS, "g13_peak_enabled": True}, T0, pump.get_state, pump.get_numeric, workday)
    card = next(k for k in data["kinds"] if k["key"] == "ah_delay_ab")
    assert "Szczyt G13" in card["blocked"]
