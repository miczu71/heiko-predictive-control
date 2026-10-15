"""D3a: wykonawca testów — granice, potwierdzenia, zmiana ręczna, komfort, powrót po restarcie. Bez sieci (fałszywa pompa)."""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import floor_learn as fl

SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
SETTINGS = {"heiko_enabled": True, "heiko_curve_switch_entity": "switch.curve", "heiko_curve_shift_entity": SHIFT,
            "heiko_room_min_c": 18.5, "advisor_notify_service": "notify.x"}
T0 = datetime(2026, 10, 7, 10, 0)          # środa


def workday(d: date) -> bool:
    return d.weekday() < 5


class Pump:
    """Fałszywa pompa: `confirm=True` — ramka od razu pokazuje zapisaną wartość."""

    def __init__(self, shift=0.0, curve="off", confirm=True, accept=True):
        self.shift, self.curve, self.confirm, self.accept = shift, curve, confirm, accept
        self.calls, self.notes = [], []

    def get_state(self, entity):
        return {"state": self.curve} if entity == "switch.curve" else None

    def get_numeric(self, entity):
        return self.shift if entity == SHIFT else None

    def writer(self, domain, service, data):
        self.calls.append((domain, service, data))
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


def _start(conn, pump, kind="zapis", settings=SETTINGS, now=T0):
    return ex.start(conn, settings, kind, now, pump.get_state, pump.get_numeric, workday)


def _tick(conn, pump, now, settings=SETTINGS):
    return ex.tick(conn, settings, now, pump.get_numeric, pump.writer, pump.notify)


def _test(conn):
    return conn.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 1").fetchone()


def test_start_needs_master_switch(conn):
    assert _start(conn, Pump(), settings={**SETTINGS, "heiko_enabled": False}) == {
        "ok": False, "error": "zapis do pompy wyłączony (Opcje → „Sterowanie aktywne (Heiko)”)"}


def test_start_checks_curve_state_and_unknowns(conn):
    assert "wyłączonej krzywej" in _start(conn, Pump(curve="on"))["error"]
    assert "włączonej krzywej" in _start(conn, Pump(curve="off"), kind="bezwladnosc")["error"]
    assert "nie wiem" in _start(conn, Pump(curve="unavailable"))["error"]
    assert "nie mogę odczytać" in _start(conn, Pump(shift=None))["error"]
    assert _start(conn, Pump(), kind="xyz")["error"] == "nieznany test"


def test_only_one_test_at_a_time(conn):
    pump = Pump()
    assert _start(conn, pump)["ok"]
    assert _start(conn, pump)["error"] == "inny test jest w toku"


def test_start_itself_writes_nothing(conn):
    pump = Pump()
    assert _start(conn, pump)["ok"]
    assert pump.calls == [] and _test(conn)["status"] == ex.RUNNING


def test_inertia_schedule_three_workdays_skips_weekend_and_started_windows():
    now = datetime(2026, 10, 8, 7, 0)       # czwartek, okno 6–12 już trwa
    steps = ex.build_schedule("bezwladnosc", 0.0, now, workday)
    days = sorted({s["at"][:10] for s in steps})
    assert days == ["2026-10-08", "2026-10-09", "2026-10-12"]           # czw (tylko 15–21), pt, pon
    assert len(steps) == 2 * (1 + 2 + 2)
    assert [s["value"] for s in steps[:2]] == [-1.0, 0.0] and steps[0]["at"] == "2026-10-08T15:00"
    assert ex.validate(steps, 0.0) is None


def test_validate_hard_limits():
    assert "zakresem" in ex.validate([{"at": "x", "value": -5}, {"at": "y", "value": -4}], -4)
    assert "krok" in ex.validate([{"at": "x", "value": 2}, {"at": "y", "value": 0}], 0)
    assert "powrotem" in ex.validate([{"at": "x", "value": 1}], 0)
    assert ex.build_schedule("zapis", 4.0, T0, workday)[0]["value"] == 3.0     # przy +4 test idzie w dół


def test_write_test_runs_to_completion(conn):
    pump = Pump()
    _start(conn, pump)
    assert _tick(conn, pump, T0)["wrote"] == 1.0
    assert pump.calls == [("number", "set_value", {"entity_id": SHIFT, "value": 1.0})]
    assert _tick(conn, pump, T0 + timedelta(minutes=5))["next"]["value"] == 0.0     # potwierdzone, czeka na krok 2
    assert _tick(conn, pump, T0 + timedelta(minutes=10))["wrote"] == 0.0
    done = _tick(conn, pump, T0 + timedelta(minutes=15))
    assert done["status"] == ex.DONE and done["result"]["writes"] == 2 and done["result"]["confirmed"] == 2
    assert pump.shift == 0.0 and _test(conn)["status"] == ex.DONE and "zakończony" in pump.notes[-1]


def test_optimistic_state_right_after_write_is_not_a_confirmation(conn):
    pump = Pump()                                          # stan HA pokazuje nową wartość od razu (jak integracja)
    _start(conn, pump)
    _tick(conn, pump, T0)
    assert _tick(conn, pump, T0 + timedelta(minutes=1))["waiting"] == "potwierdzenie zapisu"
    assert conn.execute("SELECT confirmed_at FROM pump_writes").fetchone()[0] is None
    assert "next" in _tick(conn, pump, T0 + timedelta(minutes=2))
    assert conn.execute("SELECT confirmed_at FROM pump_writes").fetchone()[0] == "2026-10-07T10:02:00"


def test_unconfirmed_write_aborts_and_restores(conn):
    pump = Pump(confirm=False)
    _start(conn, pump)
    _tick(conn, pump, T0)
    assert _tick(conn, pump, T0 + timedelta(minutes=5))["waiting"] == "potwierdzenie zapisu"
    out = _tick(conn, pump, T0 + timedelta(minutes=11))
    assert out["status"] == ex.ABORTED and "nie potwierdziła" in out["reason"]
    assert pump.calls[-1][2]["value"] == 0.0 and "Przywracam" in pump.notes[-1]
    assert conn.execute("SELECT confirmed_at FROM pump_writes WHERE new = 1").fetchone()[0] == ex.NO_CONFIRMATION


def test_manual_change_wins_without_any_write(conn):
    pump = Pump()
    _start(conn, pump)
    _tick(conn, pump, T0)
    _tick(conn, pump, T0 + timedelta(minutes=5))
    pump.shift = 3.0                                       # user zmienia na panelu
    calls = len(pump.calls)
    assert _tick(conn, pump, T0 + timedelta(minutes=8))["reason"] == "ręczna zmiana"
    assert len(pump.calls) == calls and _test(conn)["status"] == ex.ABORTED and "niczego nie nadpisuje" in pump.notes[-1]


def test_comfort_breach_aborts_and_restores(conn):
    pump = Pump(curve="on")
    _start(conn, pump, kind="bezwladnosc", now=datetime(2026, 10, 7, 5, 0))
    first = datetime(2026, 10, 7, 6, 0)
    _tick(conn, pump, first)
    assert pump.shift == -1.0
    for m in range(0, 45, 15):
        dbm.insert_cycle(conn, {"ts": (first + timedelta(minutes=m)).isoformat(), "loop": "heiko",
                                "active_profile": "ekonomia", "write_enabled": 1, "min_room_c": 18.2})
    out = _tick(conn, pump, first + timedelta(minutes=35))
    assert out["status"] == ex.ABORTED and "poniżej 18.5" in out["reason"]
    assert pump.shift == 0.0


def test_restart_during_test_restores_baseline(conn):
    pump = Pump(curve="on")
    _start(conn, pump, kind="bezwladnosc", now=datetime(2026, 10, 7, 5, 0))
    _tick(conn, pump, datetime(2026, 10, 7, 6, 0))
    out = ex.recover_on_start(conn, SETTINGS, datetime(2026, 10, 7, 7, 0), pump.writer, pump.notify)
    assert out["status"] == ex.ABORTED and _test(conn)["abort_reason"] == "restart add-onu" and pump.shift == 0.0
    assert ex.recover_on_start(conn, SETTINGS, datetime(2026, 10, 7, 7, 5), pump.writer, pump.notify) is None


def test_user_abort_before_first_write_writes_nothing(conn):
    pump = Pump()
    _start(conn, pump)
    assert ex.abort(conn, SETTINGS, T0, writer=pump.writer, notify=pump.notify)["status"] == ex.ABORTED
    assert pump.calls == [] and ex.abort(conn, SETTINGS, T0, writer=pump.writer, notify=pump.notify)["ok"] is False


def test_abort_during_pending_marks_old_write_superseded(conn):
    pump = Pump(confirm=False)
    _start(conn, pump)
    _tick(conn, pump, T0)
    ex.abort(conn, SETTINGS, T0 + timedelta(minutes=2), writer=pump.writer, notify=pump.notify)
    assert [r[0] for r in conn.execute("SELECT confirmed_at FROM pump_writes ORDER BY id")] == [ex.SUPERSEDED, None]


def test_write_refuses_entity_other_than_curve_shift(conn):
    pump = Pump()
    with pytest.raises(ValueError):                     # encja z Opcji wskazująca inny parametr pompy
        ex._write(conn, {**SETTINGS, "heiko_curve_shift_entity": "number.dhw_setpoint"}, None, 0.0, 1.0, T0, pump.writer)
    with pytest.raises(ValueError):                     # krok > 1 mimo walidacji harmonogramu
        ex._write(conn, SETTINGS, None, 0.0, 2.0, T0, pump.writer)
    assert pump.calls == []


def test_rejected_write_aborts(conn):
    pump = Pump(accept=False)
    _start(conn, pump)
    out = _tick(conn, pump, T0)
    assert out["reason"] == "HA odrzuciło zapis" and len(pump.calls) == 1        # bez próby „powrotu” do tej samej wartości


def test_overview_lists_kinds_with_blocks(conn):
    pump = Pump()
    data = ex.overview(conn, SETTINGS, T0, pump.get_state, pump.get_numeric, workday)
    blocked = {k["key"]: k["blocked"] for k in data["kinds"]}
    assert blocked["zapis"] is None and "włączonej krzywej" in blocked["bezwladnosc"]
    assert data["active"] is None and data["shift"] == 0.0 and data["curve_on"] is False


def test_overview_marks_passed_kind_and_moves_it_last(conn):
    pump = Pump()
    _start(conn, pump)
    _tick(conn, pump, T0)
    ex.abort(conn, SETTINGS, T0 + timedelta(minutes=5), writer=pump.writer, notify=pump.notify)
    data = ex.overview(conn, SETTINGS, T0, pump.get_state, pump.get_numeric, workday)
    assert [k["passed"] for k in data["kinds"]] == [None, None, None]              # przerwany ≠ wykonany
    _start(conn, pump, now=T0 + timedelta(minutes=20))
    for m in (20, 30, 35):
        _tick(conn, pump, T0 + timedelta(minutes=m))
    data = ex.overview(conn, SETTINGS, T0, pump.get_state, pump.get_numeric, workday)
    assert [k["key"] for k in data["kinds"]] == ["bezwladnosc", "p0_ab", "zapis"]
    assert data["kinds"][2]["passed"] == {"ended": "2026-10-07T10:35:00", "confirmed": 2, "writes": 2}


def test_excitation_counts_curve_shift_steps():
    rows = [{"reduced_active": 0, "curve_shift_c": s, "water_setpoint_c": 24.0 + s, "base_curve_c": 24.0}
            for s in [0, -1, -1, 0, -1, 0, -1, 0]]
    exc = fl.excitation(rows)
    assert exc == {"transitions": 6, "mean_drop_c": 1.0, "sufficient": True}
    old = [{"reduced_active": 0, "water_setpoint_c": 24.0, "base_curve_c": 24.0, "curve_shift_c": None}] * 3  # sprzed 0.12.0
    assert fl.excitation(old)["transitions"] == 0


# ── Test A/B pompy obiegowej P0 (p0_ab) ─────────────────────────────────────────────────────────────────────────
RUN = "number.lazienka_heiko_heat_pump_circulation_pump_p0_run_time"
STOP = "number.lazienka_heiko_heat_pump_circulation_pump_p0_stop_time"
P0_SETTINGS = {**SETTINGS, "day_zone_temp_entities": "sensor.salon,sensor.sypialnia", "timezone": "Europe/Warsaw"}
TZ = ZoneInfo("Europe/Warsaw")


@pytest.fixture(autouse=True)
def p0_states(monkeypatch):
    states = [{"entity_id": RUN, "state": "1"}, {"entity_id": STOP, "state": "6"}]
    monkeypatch.setattr(ex.ha_client, "get_all_states", lambda: states)


class P0Pump(Pump):
    """Fałszywa pompa z parą P0 (praca, postój) i włączoną krzywą; `log` trzyma kolejność zapisów."""

    def __init__(self, pair=(1.0, 6.0), confirm=True, accept=True):
        super().__init__(curve="on", confirm=confirm, accept=accept)
        self.vals = {RUN: pair[0], STOP: pair[1]}
        self.log = []

    def get_numeric(self, entity):
        return self.vals.get(entity, super().get_numeric(entity))

    def writer(self, domain, service, data):
        self.calls.append((domain, service, data))
        self.log.append((data["entity_id"], data["value"]))
        if self.accept and self.confirm:
            self.vals[data["entity_id"]] = data["value"]
        return self.accept


def _d4b(conn):
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('bezwladnosc', ?, ?, '[]', 0)",
                 (ex.DONE, "2026-10-01T10:00:00"))
    conn.commit()


def _p0_start(conn, pump, now=T0, settings=P0_SETTINGS):
    return ex.start(conn, settings, "p0_ab", now, pump.get_state, pump.get_numeric, workday)


def _p0_tick(conn, pump, now, settings=P0_SETTINGS):
    return ex.tick(conn, settings, now, pump.get_numeric, pump.writer, pump.notify)


def test_p0_schedule_is_six_midnight_blocks_ABABAB_and_returns_to_baseline():
    steps = ex.build_schedule("p0_ab", (1.0, 6.0), T0, workday)
    assert [s["at"] for s in steps] == [f"2026-10-{d:02d}T00:00" for d in range(8, 15)]
    assert [(s["value"], s["stop"]) for s in steps] == [(1, 6), (5, 5), (1, 6), (5, 5), (1, 6), (5, 5), (1, 6)]
    assert ex.validate(steps, (1.0, 6.0)) is None
    swapped = ex.build_schedule("p0_ab", (5.0, 5.0), T0, workday)
    assert (swapped[0]["value"], swapped[1]["value"], swapped[-1]["value"]) == (5, 1, 5)
    assert ex.build_schedule("p0_ab", (3.0, 6.0), T0, workday) == []


def test_p0_validate_rejects_other_pairs_and_missing_return():
    bad_pair = [{"at": "x", "value": 1.0, "stop": 5.0}, {"at": "y", "value": 1.0, "stop": 6.0}]
    assert "spoza dozwolonych" in ex.validate(bad_pair, (1.0, 6.0))
    assert "powrotem" in ex.validate([{"at": "x", "value": 5.0, "stop": 5.0}], (1.0, 6.0))


def test_p0_start_blocks_until_d4b_done_curve_on_and_known_pair(conn):
    pump = P0Pump()
    assert "Bezwładność" in _p0_start(conn, pump)["error"]
    _d4b(conn)
    pump.curve = "off"
    assert "włączonej krzywej" in _p0_start(conn, pump)["error"]
    pump.curve = "on"
    pump.vals[RUN] = 3.0
    assert "tylko 1/6 i 5/5" in _p0_start(conn, pump)["error"]
    pump.vals[RUN] = None
    assert "nie mogę odczytać" in _p0_start(conn, pump)["error"]
    pump.vals[RUN] = 1.0
    assert _p0_start(conn, pump)["ok"] and pump.calls == []            # Start sam nic nie zapisuje
    row = _test(conn)
    assert row["kind"] == "p0_ab" and row["baseline"] == 1.0 and len(json.loads(row["schedule"])) == 7


def test_p0_overview_shows_card_with_own_abort_conditions_and_blocks(conn):
    pump = P0Pump()
    data = ex.overview(conn, P0_SETTINGS, T0, pump.get_state, pump.get_numeric, workday)
    card = next(k for k in data["kinds"] if k["key"] == "p0_ab")
    assert "Bezwładność" in card["blocked"] and len(card["schedule"]) == 7 and data["p0"] == [1.0, 6.0]
    assert "watchdog" not in " ".join(card["abort_conditions"]) and "45 min" not in " ".join(card["abort_conditions"])


def _run_to_second_block(conn, pump):
    _d4b(conn)
    _p0_start(conn, pump)
    assert _p0_tick(conn, pump, T0)["next"]                              # przed pierwszą północą nic się nie dzieje
    m1 = datetime(2026, 10, 8, 0, 0)
    assert "wrote" not in _p0_tick(conn, pump, m1)                       # blok A = wartość wyjściowa, bez zapisu
    return m1 + timedelta(days=1)


def test_p0_switch_writes_both_entities_in_one_moment_and_waits_for_both_confirmations(conn):
    pump = P0Pump()
    m2 = _run_to_second_block(conn, pump)
    assert _p0_tick(conn, pump, m2)["wrote"] == 5.0
    assert pump.log == [(RUN, 5.0), (STOP, 5.0)]                          # nigdy postój < 5 min po drodze
    assert {w["ts"] for w in conn.execute("SELECT ts FROM pump_writes")} == {"2026-10-09T00:00:00"}
    assert all(w["confirmed_at"] is None for w in conn.execute("SELECT * FROM pump_writes"))   # żadnego nie superseded
    assert _p0_tick(conn, pump, m2 + timedelta(minutes=1))["waiting"] == "potwierdzenie zapisu"
    assert "next" in _p0_tick(conn, pump, m2 + timedelta(minutes=5))
    assert all(w["confirmed_at"] for w in conn.execute("SELECT * FROM pump_writes"))


def test_p0_write_refuses_foreign_entities_and_values_outside_pairs(conn):
    pump = P0Pump()
    for entity, new in ((RUN, 3.0), (STOP, 4.0), (STOP, 7.0), ("number.dhw_setpoint", 5.0), ("sensor.x_p0_run_time", 5.0)):
        with pytest.raises(ValueError):
            ex._write(conn, P0_SETTINGS, None, 1.0, new, T0, pump.writer, entity=entity)
    assert pump.calls == []
    assert ex._write(conn, P0_SETTINGS, None, 6.0, 5.0, T0, pump.writer, entity=STOP)      # dozwolone: 6 → 5


def test_p0_abort_restores_baseline_pair(conn):
    pump = P0Pump()
    m2 = _run_to_second_block(conn, pump)
    _p0_tick(conn, pump, m2)
    _p0_tick(conn, pump, m2 + timedelta(minutes=5))
    pump.log.clear()
    assert ex.abort(conn, P0_SETTINGS, m2 + timedelta(minutes=6), writer=pump.writer, notify=pump.notify)["ok"]
    assert pump.log == [(RUN, 1.0), (STOP, 6.0)] and "P0 1/6" in pump.notes[-1]
    assert pump.notes[-1].startswith("Test przerwany") and _test(conn)["status"] == ex.ABORTED


def test_p0_restart_restores_baseline_pair(conn):
    pump = P0Pump()
    m2 = _run_to_second_block(conn, pump)
    _p0_tick(conn, pump, m2)
    pump.log.clear()
    ex.recover_on_start(conn, P0_SETTINGS, m2 + timedelta(minutes=1), writer=pump.writer, notify=pump.notify)
    assert pump.log == [(RUN, 1.0), (STOP, 6.0)]


def test_p0_manual_change_wins_without_any_write(conn):
    pump = P0Pump()
    m2 = _run_to_second_block(conn, pump)
    _p0_tick(conn, pump, m2)
    _p0_tick(conn, pump, m2 + timedelta(minutes=5))
    pump.vals[STOP] = 10.0                                                # ktoś zmienia postój ręcznie
    pump.log.clear()
    assert _p0_tick(conn, pump, m2 + timedelta(minutes=10))["reason"] == "ręczna zmiana"
    assert pump.log == [] and "P0" in pump.notes[-1] and "5/10" in pump.notes[-1]


def test_p0_unconfirmed_write_aborts_and_restores_pair(conn):
    pump = P0Pump(confirm=False)
    m2 = _run_to_second_block(conn, pump)
    _p0_tick(conn, pump, m2)
    pump.confirm = True
    out = _p0_tick(conn, pump, m2 + timedelta(minutes=11))
    assert out["status"] == ex.ABORTED and "nie potwierdziła" in out["reason"]
    assert pump.log[-2:] == [(RUN, 1.0), (STOP, 6.0)]


def test_p0_rejected_second_write_forces_full_restore(conn):
    pump = P0Pump()
    m2 = _run_to_second_block(conn, pump)
    calls = {"n": 0}
    real = pump.writer

    def flaky(domain, service, data):
        calls["n"] += 1
        real(domain, service, data)
        return calls["n"] != 2                                            # drugi zapis (postój) odrzucony
    out = ex.tick(conn, P0_SETTINGS, m2, pump.get_numeric, flaky, pump.notify)
    assert out["reason"] == "HA odrzuciło zapis"
    assert pump.log[-2:] == [(RUN, 1.0), (STOP, 6.0)]                     # wymuszony powrót obu encji


# --- werdykt ---------------------------------------------------------------------------------------------------
def _fill(conn, spreads, outdoor=None, kwh=None, samples=16, insert_test=True):
    """Telemetria, energia i temp. zewn. dla 6 dób testu (start T0). `spreads` = {etykieta: (rozrzut, średnia)}."""
    steps = ex.build_schedule("p0_ab", (1.0, 6.0), T0, workday)
    if insert_test:
        conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('p0_ab', ?, ?, ?, 1.0)",
                     (ex.RUNNING, "2026-10-07T10:00:00", json.dumps(steps)))
    for step in steps[:-1]:
        label = f"{step['value']:g}/{step['stop']:g}"
        day = datetime.fromisoformat(step["at"])
        spread, avg = spreads[label]
        for i in range(samples):
            ts = int((day + timedelta(minutes=15 * i)).replace(tzinfo=TZ).timestamp())
            for name, value in (("working_mode", 0.0), ("sensor.salon", avg + spread / 2), ("sensor.sypialnia", avg - spread / 2)):
                conn.execute("INSERT OR IGNORE INTO telemetry_keys (name) VALUES (?)", (name,))
                key = conn.execute("SELECT id FROM telemetry_keys WHERE name = ?", (name,)).fetchone()["id"]
                conn.execute("INSERT OR REPLACE INTO telemetry (ts, key_id, value) VALUES (?, ?, ?)", (ts, key, value))
        if kwh:
            conn.execute("INSERT OR REPLACE INTO daily_summary (day, topic, data) VALUES (?, 'energy', ?)",
                         (day.date().isoformat(), json.dumps({"kwh": kwh[label]})))
        if outdoor:
            conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, outdoor_temp_c) VALUES (?, 'heiko', 'ekonomia', 0, ?)",
                         ((day + timedelta(hours=12)).isoformat(timespec="seconds"), outdoor[label]))
    conn.commit()
    return _test(conn)


NOW_END = datetime(2026, 10, 14, 0, 5)


def test_p0_verdict_5_5_wins_with_enough_gain_and_reports_kwh_and_confidence(conn):
    test = _fill(conn, {"1/6": (1.0, 21.0), "5/5": (0.6, 21.0)}, {"1/6": 8.0, "5/5": 9.0}, {"1/6": 20.0, "5/5": 20.4})
    r = ex._result(conn, test, NOW_END, P0_SETTINGS)
    assert r["verdict"] == "5/5" and r["gain_c"] == 0.4 and r["extra_kwh_day"] == 0.4 and r["confidence"] == "wysoka"
    assert r["variants"]["1/6"]["days"] == 3 and r["variants"]["5/5"]["spread_c"] == 0.6 and r["outdoor_c"] == 8.5


def test_p0_verdict_stays_1_6_below_gain_threshold(conn):
    test = _fill(conn, {"1/6": (1.0, 21.0), "5/5": (0.8, 21.0)})
    assert ex._result(conn, test, NOW_END, P0_SETTINGS)["verdict"] == "1/6"


def test_p0_verdict_threshold_is_inclusive_despite_float_noise(conn):
    test = _fill(conn, {"1/6": (1.2, 21.0), "5/5": (0.9, 21.0)})
    assert ex._result(conn, test, NOW_END, P0_SETTINGS)["verdict"] == "5/5"


def test_p0_verdict_stays_1_6_when_zone_average_drops(conn):
    test = _fill(conn, {"1/6": (1.0, 21.0), "5/5": (0.4, 20.5)})
    assert ex._result(conn, test, NOW_END, P0_SETTINGS)["verdict"] == "1/6"


def test_p0_weather_difference_over_3c_gives_low_confidence(conn):
    test = _fill(conn, {"1/6": (1.0, 21.0), "5/5": (0.5, 21.0)}, {"1/6": 4.0, "5/5": 8.0})
    r = ex._result(conn, test, NOW_END, P0_SETTINGS)
    assert r["verdict"] == "5/5" and r["confidence"] == "niska"


def test_p0_without_enough_standby_samples_has_no_verdict_and_is_not_passed(conn):
    test = _fill(conn, {"1/6": (1.0, 21.0), "5/5": (0.5, 21.0)}, samples=2)
    r = ex._result(conn, test, NOW_END, P0_SETTINGS)
    assert r["verdict"] is None and r["confidence"] is None
    conn.execute("UPDATE tests SET status = ?, result = ? WHERE id = ?", (ex.DONE, json.dumps({**r, "writes": 0, "confirmed": 0}), test["id"]))
    assert ex._passed(conn, "p0_ab") is None


def test_p0_full_run_finishes_with_verdict_notification_and_passed_card(conn):
    pump = P0Pump()
    _d4b(conn)
    _p0_start(conn, pump)
    steps = ex.build_schedule("p0_ab", (1.0, 6.0), T0, workday)
    _fill(conn, {"1/6": (1.0, 21.0), "5/5": (0.5, 21.0)}, insert_test=False)
    now = T0
    for step in steps:
        now = datetime.fromisoformat(step["at"])
        _p0_tick(conn, pump, now)
        _p0_tick(conn, pump, now + timedelta(minutes=2))
        _p0_tick(conn, pump, now + timedelta(minutes=3))
    row = _test(conn)                                                     # wynik liczy tick po zleceniu ostatniego kroku
    assert row["status"] == ex.DONE and json.loads(row["result"])["verdict"] == "5/5"
    assert pump.vals == {RUN: 1.0, STOP: 6.0}                             # ostatni krok = powrót do wartości wyjściowej
    assert "werdykt 5/5" in pump.notes[-1] and "pewność" in pump.notes[-1]
    card = ex.overview(conn, P0_SETTINGS, now, pump.get_state, pump.get_numeric, workday)["kinds"]
    assert next(k for k in card if k["key"] == "p0_ab")["passed"]["verdict"] == "5/5"
    hist = next(h for h in ex.overview(conn, P0_SETTINGS, now, pump.get_state, pump.get_numeric, workday)["history"]
                if h["kind"] == "p0_ab")
    assert hist["expected"] == 1.0 and hist["baseline_stop"] == 6.0


def test_unknown_kind_row_does_not_break_overview(conn):
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('przyszly', ?, ?, '[]', 0)",
                 (ex.DONE, "2026-10-01T10:00:00"))
    conn.commit()
    assert ex.overview(conn, P0_SETTINGS, T0, Pump().get_state, Pump().get_numeric, workday)["history"][0]["kind"] == "przyszly"


def test_p0_abort_finishes_test_even_when_p0_entities_are_unresolvable(conn, monkeypatch):
    pump = P0Pump()
    m2 = _run_to_second_block(conn, pump)
    _p0_tick(conn, pump, m2)                                              # pompa na 5/5
    monkeypatch.setattr(ex.ha_client, "get_all_states", lambda: [])       # HA jeszcze się uruchamia
    out = ex.recover_on_start(conn, P0_SETTINGS, m2 + timedelta(minutes=10), writer=pump.writer, notify=pump.notify)
    assert out["status"] == ex.ABORTED and ex.running_test(conn) is None
