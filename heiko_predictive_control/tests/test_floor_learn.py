import math
import random
from datetime import datetime, timedelta, timezone

from heiko_predictive_control import db as dbm
from heiko_predictive_control import floor_learn as fl
from heiko_predictive_control import floor_model as fm
from heiko_predictive_control import floor_plan as fp

AMB = [-13, -7, 0, 7, 13]
WAT = [27, 26, 25, 24, 23]
CURVE = {**{f"number.a{i}": v for i, v in enumerate(AMB)}, **{f"number.w{i}": v for i, v in enumerate(WAT)}}
SETTINGS = {
    "day_zone_temp_entities": "sensor.z1,sensor.z2", "outdoor_temp_entity": "sensor.out",
    "heiko_bootstrap_outdoor_entity": "sensor.owm", "pump_energy_entity": "sensor.energy",
    "heiko_curve_ambient_entities": ",".join(f"number.a{i}" for i in range(5)),
    "heiko_curve_water_entities": ",".join(f"number.w{i}" for i in range(5)),
}


def _conn():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    return conn


def _world(hours, dt_h, tau=4.0, g=0.3, c=0.06, start=datetime(2026, 1, 5, tzinfo=timezone.utc),
           seed=3):
    """Symulowany dom pod krzywą natywną z zakłóconą pogodą: (czas, Tr, To, q_kw)."""
    rng = random.Random(seed)
    a = fm.alpha(dt_h, tau)
    tr, qf = 20.5, 3.0
    out = []
    n = int(hours / dt_h)
    for i in range(n):
        h = i * dt_h
        to = 1.0 + 4.0 * math.sin(2 * math.pi * h / 24) + 3.0 * math.sin(2 * math.pi * h / 130)
        water = fp.curve_setpoint(AMB, WAT, to)
        # moc: regulacja natywna + pulsowanie (CWU/harmonogram), żeby był sygnał do dopasowania
        q = max(0.0, 0.5 * (water - tr)) * (0.3 if (h % 24) < 5 else 1.4)
        out.append((start + timedelta(hours=h), tr + rng.gauss(0, 0.01), to, q))
        tr += dt_h * (g * qf - c * (tr - to))
        qf += a * (q - qf)
    return out


def _stats(world):
    zone = [{"start": int(t.timestamp() * 1000), "mean": tr} for t, tr, _, _ in world]
    zone2 = [{"start": int(t.timestamp() * 1000), "mean": tr + 0.4} for t, tr, _, _ in world]
    outd = [{"start": int(t.timestamp() * 1000), "mean": to} for t, _, to, _ in world]
    energy = []
    for t, _, to, q in world:
        water = fp.curve_setpoint(AMB, WAT, to)
        energy.append({"start": int(t.timestamp() * 1000), "change": q / fm.cop(to, max(water, to + 3.0))})
    return {"sensor.z1": zone, "sensor.z2": zone2, "sensor.owm": outd, "sensor.energy": energy}


def test_bootstrap_recovers_model_from_winter_statistics():
    conn = _conn()
    stats = _stats(_world(24 * 30, 1.0))
    now = datetime(2026, 9, 26, 12, 0)
    info = fl.bootstrap(conn, SETTINGS, now, get_statistics=lambda ids, s, e: stats,
                        get_numeric=lambda e: CURVE.get(e))
    assert info["ok"], info
    model = fm.FloorModel.from_dict(dbm.get_setting(conn, "floor_model_state"))
    assert model.source == fm.SOURCE_BOOTSTRAP
    assert abs(model.c - 0.06) < 0.02 and abs(model.g - 0.3) < 0.1
    assert model.rmse_c < 0.5
    assert dbm.get_setting(conn, "floor_bootstrap")["ok"] is True


def test_bootstrap_without_statistics_access_leaves_no_marker_so_it_retries():
    conn = _conn()
    info = fl.bootstrap(conn, SETTINGS, datetime(2026, 9, 26), get_statistics=lambda *a: None,
                        get_numeric=lambda e: CURVE.get(e))
    assert not info["ok"]
    assert dbm.get_setting(conn, "floor_bootstrap") is None
    assert dbm.get_setting(conn, "floor_model_state") is None


def test_bootstrap_rejects_garbage_and_keeps_defaults():
    conn = _conn()
    rng = random.Random(9)
    world = [(t, 20.0 + rng.gauss(0, 1.5), to, q) for t, _, to, q in _world(24 * 30, 1.0)]
    info = fl.bootstrap(conn, SETTINGS, datetime(2026, 9, 26), get_statistics=lambda *a: _stats(world),
                        get_numeric=lambda e: CURVE.get(e))
    assert not info["ok"]
    assert dbm.get_setting(conn, "floor_model_state") is None
    assert dbm.get_setting(conn, "floor_bootstrap")["ok"] is False


def test_heating_runs_split_on_gaps_and_skip_summer():
    def row(day, hour, to=5.0):
        t = datetime(2026, 1, day, hour, tzinfo=timezone.utc)
        return (int(t.timestamp() * 1000), 20.0, to, 0.5)
    rows = [row(5, h) for h in range(24)] + [row(6, h) for h in range(3, 20)]      # przerwa 00–02
    runs = fl.heating_runs(rows)
    assert [len(r) for r in runs] == [24, 17]
    july = [(int(datetime(2026, 7, 5, h, tzinfo=timezone.utc).timestamp() * 1000), 20.0, 5.0, 0.5)
            for h in range(24)]
    assert fl.heating_runs(july) == []
    warm = [row(5, h, to=18.0) for h in range(24)]
    assert fl.heating_runs(warm) == []


def _cycle_rows(conn, world, base_ts):
    """Wstaw cykle 15-min jak robi to add-on: heat_kw wiersza = moc z poprzedniego kroku."""
    prev_q = None
    for i, (_, tr, to, q) in enumerate(world):
        dbm.insert_cycle(conn, {
            "ts": (base_ts + timedelta(minutes=15 * i)).isoformat(), "loop": "heiko",
            "active_profile": "ekonomia", "write_enabled": 0, "indoor_temp_c": tr,
            "outdoor_temp_c": to, "heat_kw": prev_q, "water_temp_c": fp.curve_setpoint(AMB, WAT, to)})
        prev_q = q


def test_cycle_segments_shift_heat_by_one_step_and_split_on_gap():
    conn = _conn()
    world = _world(24, 0.25)
    base = datetime(2026, 1, 5)
    _cycle_rows(conn, world[:40], base)
    _cycle_rows(conn, world[40:60], base + timedelta(hours=20))             # przerwa
    rows = conn.execute("SELECT * FROM cycles WHERE loop='heiko' ORDER BY id").fetchall()
    segs = fl.cycle_segments(rows)
    assert len(segs) == 2
    assert segs[0].q_kw[0] == world[0][3]          # heat_kw wiersza 1 = moc kroku 0
    assert len(segs[0].tr) == len(segs[0].q_kw) == 40


def test_refit_from_cycles_learns_and_marks_shadow_source():
    conn = _conn()
    world = _world(24 * 6, 0.25)
    base = datetime(2026, 1, 5)
    _cycle_rows(conn, world, base)
    info = fl.refit_from_cycles(conn, SETTINGS, base + timedelta(days=6, hours=1))
    assert info["ok"], info
    model = fm.FloorModel.from_dict(dbm.get_setting(conn, "floor_model_state"))
    assert model.source == fm.SOURCE_SHADOW and abs(model.c - 0.06) < 0.02
    assert dbm.get_setting(conn, "floor_refit")["ok"] is True


def test_refit_with_too_little_data_keeps_model():
    conn = _conn()
    _cycle_rows(conn, _world(6, 0.25), datetime(2026, 1, 5))
    info = fl.refit_from_cycles(conn, SETTINGS, datetime(2026, 1, 5, 8))
    assert not info["ok"] and dbm.get_setting(conn, "floor_model_state") is None


def test_season_window_is_last_complete_heating_season():
    assert fl.season_window(datetime(2026, 9, 26))[0] == datetime(2025, 10, 15, tzinfo=timezone.utc)
    assert fl.season_window(datetime(2026, 9, 26))[1] == datetime(2026, 4, 15, tzinfo=timezone.utc)
    assert fl.season_window(datetime(2027, 2, 1))[1] == datetime(2026, 4, 15, tzinfo=timezone.utc)
    assert fl.season_window(datetime(2027, 5, 1))[0] == datetime(2026, 10, 15, tzinfo=timezone.utc)
