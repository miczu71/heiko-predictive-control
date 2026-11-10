"""E3: złote testy adaptera `compat` == legacy (`experiments._p0_result` / `_result`) na tych samych danych."""
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
import legacy_oracle as lo
from heiko_predictive_control import floor_learn as fl
from heiko_predictive_control.testkit import compat

TZ = ZoneInfo("Europe/Warsaw")
SETTINGS = {"timezone": "Europe/Warsaw", "day_zone_temp_entities": "sensor.z1,sensor.z2"}
NOW = datetime(2026, 10, 20, 12, 0)
FIRST = date(2026, 10, 8)
A, B = (1.0, 6.0), (5.0, 5.0)


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def _p0_test(conn):
    pairs = [A, B, A, B, A, B, A]
    schedule = [{"at": datetime.combine(FIRST + timedelta(days=i), time(0)).isoformat(timespec="minutes"),
                 "value": p[0], "stop": p[1]} for i, p in enumerate(pairs)]
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('p0_ab', 'w_toku', ?, ?, 1)",
                 ("2026-10-07T10:00:00", json.dumps(schedule)))
    conn.commit()
    return conn.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 1").fetchone()


def _samples(conn, d, spread, avg):
    base = int(datetime.combine(d, time(1)).replace(tzinfo=TZ).timestamp())
    for name in ("working_mode", "sensor.z1", "sensor.z2"):
        conn.execute("INSERT OR IGNORE INTO telemetry_keys (name) VALUES (?)", (name,))
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM telemetry_keys")}
    for i in range(36):                                                      # próbki co 5 min, tryb 0
        for name, v in (("working_mode", 0), ("sensor.z1", avg + spread / 2), ("sensor.z2", avg - spread / 2)):
            conn.execute("INSERT INTO telemetry (ts, key_id, value) VALUES (?, ?, ?)", (base + i * 300, ids[name], v))


def build(conn, spread_a=1.0, spread_b=0.4, avg_a=21.0, avg_b=21.0, out_a=5.0, out_b=5.0, b_samples=True):
    kwh = {}
    for i in range(6):
        d = FIRST + timedelta(days=i)
        is_a = i % 2 == 0
        if is_a or b_samples:
            _samples(conn, d, spread_a if is_a else spread_b, avg_a if is_a else avg_b)
        conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, outdoor_temp_c) "
                     "VALUES (?, 'heiko', 'komfort', 1, ?)", (datetime.combine(d, time(12)).isoformat(),
                                                                out_a if is_a else out_b))
        kwh[d] = 20.0 + i if is_a else 22.5 + i
        conn.execute("INSERT INTO daily_summary (day, topic, data) VALUES (?, 'energy', ?)",
                     (d.isoformat(), json.dumps({"kwh": kwh[d]})))
    conn.commit()
    return _p0_test(conn), (lambda c, s, d, n: kwh.get(d))


def both(conn, monkeypatch, **kw):
    test, fake = build(conn, **kw)
    monkeypatch.setattr(ex, "_day_kwh", lambda c, s, d, n: fake(c, s, d, n))
    legacy = lo._p0_result(conn, SETTINGS, test, NOW)
    return compat.p0_result(conn, SETTINGS, test, NOW, day_kwh=fake), legacy


def test_p0_golden_5_5_wins_high_confidence(conn, monkeypatch):
    new, legacy = both(conn, monkeypatch)
    assert new == legacy
    assert new["verdict"] == "5/5" and new["confidence"] == "wysoka" and new["gain_c"] == 0.6
    assert new["variants"]["1/6"]["days"] == 3 and new["variants"]["5/5"]["days"] == 3
    assert new["extra_kwh_day"] == 3.5 and new["outdoor_c"] == 5.0


def test_p0_golden_weather_far_apart_low_confidence(conn, monkeypatch):
    new, legacy = both(conn, monkeypatch, out_b=10.0)
    assert new == legacy and new["confidence"] == "niska" and new["verdict"] == "5/5"


def test_p0_golden_no_samples_one_variant_no_verdict(conn, monkeypatch):
    new, legacy = both(conn, monkeypatch, b_samples=False)
    assert new == legacy
    assert new["verdict"] is None and new["confidence"] is None and new["variants"]["5/5"]["spread_c"] is None


def test_p0_golden_average_drops_keeps_1_6(conn, monkeypatch):
    new, legacy = both(conn, monkeypatch, avg_b=20.8)
    assert new == legacy and new["verdict"] == "1/6" and new["gain_c"] == 0.6


def test_p0_default_day_kwh_uses_experiments_hook(conn, monkeypatch):
    test, fake = build(conn)
    monkeypatch.setattr(ex, "_day_kwh", lambda c, s, d, n: fake(c, s, d, n))
    assert compat.p0_result(conn, SETTINGS, test, NOW) == lo._p0_result(conn, SETTINGS, test, NOW)


# --- bezwładność i zapis ---

def _inertia_test(conn, excite):
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('bezwladnosc', 'w_toku', "
                 "'2026-10-01T10:00:00', '[]', 0)")
    for i in range(10 if excite else 4):
        conn.execute("INSERT INTO cycles (ts, loop, active_profile, write_enabled, reduced_active, water_setpoint_c, "
                     "base_curve_c, curve_shift_c) VALUES (?, 'heiko', 'komfort', 1, ?, ?, 33.0, 0.0)",
                     (datetime(2026, 10, 2, i).isoformat(), (i % 2) if excite else 0, 30.0 if excite else 33.0))
    conn.commit()
    return conn.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 1").fetchone()


def _writes(conn, test, rows):
    for ts, conf in rows:
        conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok, confirmed_at) "
                     "VALUES (?, ?, 'number.x', 0, 1, 1, ?)", (ts, test["id"], conf))
    conn.commit()


@pytest.mark.parametrize("excite", [True, False])
def test_inertia_golden(conn, monkeypatch, excite):
    test = _inertia_test(conn, excite)
    _writes(conn, test, [("2026-10-02T06:00:00", "2026-10-02T06:03:00"), ("2026-10-02T12:00:00", ex.NO_CONFIRMATION)])
    calls = []

    def fake(c, settings, now, **kw):
        calls.append(kw)
        return {"ok": excite, "reason": "ok", "rmse_c": 0.2, "tau_h": 6.0, "identified": excite, "other": 1}

    monkeypatch.setattr(fl, "refit_from_cycles", fake)
    legacy = lo._result(conn, test, NOW, SETTINGS)
    new = compat.inertia_result(conn, SETTINGS, test, NOW)
    assert new == legacy and calls == [{"excitation_since": "2026-10-01T10:00:00"}] * 2
    assert new["excitation"]["sufficient"] is excite and set(new) == {"writes", "confirmed", "max_confirm_min",
                                                                       "excitation", "refit"}


@pytest.mark.parametrize("rows", [
    [("2026-10-07T10:00:00", "2026-10-07T10:04:00"), ("2026-10-07T10:10:00", "2026-10-07T10:12:00")],
    [("2026-10-07T10:00:00", ex.NO_CONFIRMATION)],
    [("2026-10-07T10:00:00", ex.SUPERSEDED), ("2026-10-07T10:10:00", "2026-10-07T10:11:00")],
    [("2026-10-07T10:00:00", None)],
    [],
])
def test_zapis_golden(conn, rows):
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('zapis', 'w_toku', "
                 "'2026-10-07T10:00:00', '[]', 0)")
    test = conn.execute("SELECT * FROM tests").fetchone()
    _writes(conn, test, rows)
    assert compat.zapis_result(conn, test) == lo._result(conn, test, NOW, SETTINGS)
