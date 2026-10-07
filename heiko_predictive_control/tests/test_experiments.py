"""D3a: wykonawca testów — granice, potwierdzenia, zmiana ręczna, komfort, powrót po restarcie. Bez sieci (fałszywa pompa)."""
from datetime import date, datetime, timedelta

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


def test_excitation_counts_curve_shift_steps():
    rows = [{"reduced_active": 0, "curve_shift_c": s, "water_setpoint_c": 24.0 + s, "base_curve_c": 24.0}
            for s in [0, -1, -1, 0, -1, 0, -1, 0]]
    exc = fl.excitation(rows)
    assert exc == {"transitions": 6, "mean_drop_c": 1.0, "sufficient": True}
    old = [{"reduced_active": 0, "water_setpoint_c": 24.0, "base_curve_c": 24.0, "curve_shift_c": None}] * 3  # sprzed 0.12.0
    assert fl.excitation(old)["transitions"] == 0
