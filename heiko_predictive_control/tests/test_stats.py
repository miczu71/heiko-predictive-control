"""Statystyki: szeregi czasowe do wykresów (agregacja po stronie serwera) i podsumowanie w jednym zdaniu danych."""
from datetime import datetime, timedelta

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import stats
from heiko_predictive_control.web import create_app

NOW = datetime(2026, 10, 12, 12, 0, 0)


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def heiko_row(conn, when, **kw):
    row = dict(ts=when.isoformat(), loop="heiko", active_profile="ekonomia", tariff_peak=0, price_pln_kwh=0.6,
               outdoor_temp_c=5.0, indoor_temp_c=21.0, min_room_c=20.0, energy_kwh=0.5, model_err_c=0.1,
               base_curve_c=30.0, setpoint_komfort=31.0, setpoint_ekonomia=29.0, water_temp_c=33.0, write_enabled=0)
    row.update(kw)
    dbm.insert_cycle(conn, row)


def attic_row(conn, when, **kw):
    row = dict(ts=when.isoformat(), loop="attic", active_profile="komfort", indoor_temp_c=22.0, ac_cmd_setpoint=22.5,
               ac_power_w=800.0, window_open=0, wrote=0, attic_energy_kwh=0.07, attic_cost_pln=0.05, write_enabled=0)
    row.update(kw)
    dbm.insert_cycle(conn, row)


def test_raw_range_keeps_every_cycle_and_columns_align(conn):
    for i in range(4):
        heiko_row(conn, NOW - timedelta(minutes=15 * i), indoor_temp_c=21.0 + i)
    out = stats.build(conn, "heiko", 24, NOW)
    assert out["bucket_s"] == 0 and len(out["t"]) == 4
    assert out["t"] == sorted(out["t"])                                              # rosnąco — tego wymaga uPlot
    assert out["series"]["indoor_c"] == [24.0, 23.0, 22.0, 21.0]
    assert all(len(v) == 4 for v in out["series"].values())


def test_rows_outside_the_window_and_other_loop_are_ignored(conn):
    heiko_row(conn, NOW - timedelta(hours=25))
    heiko_row(conn, NOW - timedelta(hours=1))
    attic_row(conn, NOW - timedelta(hours=1))
    assert len(stats.build(conn, "heiko", 24, NOW)["t"]) == 1


def test_week_range_aggregates_per_hour(conn):
    base = datetime(2026, 10, 12, 9, 0)
    for i, (indoor, room, kwh) in enumerate([(21.0, 20.4, 0.5), (22.0, 19.9, 0.25), (23.0, 20.2, 0.25)]):
        heiko_row(conn, base + timedelta(minutes=15 * i), indoor_temp_c=indoor, min_room_c=room, energy_kwh=kwh)
    heiko_row(conn, base + timedelta(hours=1), indoor_temp_c=20.0)
    out = stats.build(conn, "heiko", 168, NOW)
    assert out["bucket_s"] == 3600 and len(out["t"]) == 2
    s = out["series"]
    assert s["indoor_c"][0] == pytest.approx(22.0)                                    # średnia z 3 cykli
    assert s["min_room_c"][0] == pytest.approx(19.9)                                  # najzimniejszy pokój = minimum, nie średnia
    assert s["energy_offpeak_kwh"][0] == pytest.approx(1.0)                           # energia = suma
    assert s["cost_pln"][0] == pytest.approx(1.0 * 0.6)


def test_energy_is_split_by_tariff_and_empty_side_is_null(conn):
    heiko_row(conn, NOW - timedelta(minutes=30), tariff_peak=0, energy_kwh=0.4)
    heiko_row(conn, NOW - timedelta(minutes=15), tariff_peak=1, energy_kwh=0.3, price_pln_kwh=1.2)
    s = stats.build(conn, "heiko", 24, NOW)["series"]
    assert s["energy_offpeak_kwh"] == [0.4, None] and s["energy_peak_kwh"] == [None, 0.3]
    assert s["cost_pln"] == [pytest.approx(0.24), pytest.approx(0.36)]


def test_missing_values_stay_null_not_zero(conn):
    heiko_row(conn, NOW - timedelta(minutes=15), model_err_c=None, energy_kwh=None, min_room_c=None)
    s = stats.build(conn, "heiko", 24, NOW)["series"]
    assert s["model_err_c"] == [None] and s["min_room_c"] == [None]
    assert s["energy_offpeak_kwh"] == [None] and s["cost_pln"] == [None]


def test_heiko_summary_numbers(conn):
    for i in range(8):                                                                # 8 cykli co 15 min = 2 h
        heiko_row(conn, NOW - timedelta(minutes=15 * i), indoor_temp_c=21.0, energy_kwh=1.0,
                  min_room_c=18.0 if i < 2 else 20.0, tariff_peak=1 if i < 2 else 0,
                  price_pln_kwh=1.0 if i < 2 else 0.5, model_err_c=0.2 if i % 2 else -0.8)
    sm = stats.build(conn, "heiko", 24, NOW, meta={"target_c": 20.6, "room_min_c": 18.5})["summary"]
    assert sm["avg_indoor_c"] == 21.0 and sm["coldest_room_c"] == 18.0
    assert sm["hours_below_min"] == 0.5                                               # 2 cykle × 15 min
    assert sm["energy_kwh"] == 8.0 and sm["cost_pln"] == pytest.approx(2 * 1.0 + 6 * 0.5)
    assert sm["peak_share"] == 0.25
    assert sm["model_mean_abs_err_c"] == pytest.approx(0.5)
    assert sm["model_within_half_deg_pct"] == 50                                      # |0,2| ≤ 0,5, |−0,8| > 0,5


def test_summary_of_an_empty_window_is_none_not_a_crash(conn):
    out = stats.build(conn, "heiko", 24, NOW, meta={"target_c": 20.6, "room_min_c": 18.5})
    assert out["t"] == [] and out["summary"] is None


def test_attic_series_and_summary(conn):
    for i in range(4):
        attic_row(conn, NOW - timedelta(minutes=5 * i), ac_power_w=0.0 if i == 0 else 800.0, window_open=1 if i == 1 else 0,
                  wrote=1 if i == 2 else 0)
    out = stats.build(conn, "attic", 24, NOW)
    assert set(out["series"]) == {"indoor_c", "setpoint_c", "power_w", "window_open", "energy_kwh", "cost_pln"}
    sm = out["summary"]
    assert sm["avg_indoor_c"] == 22.0 and sm["energy_kwh"] == pytest.approx(0.28)
    assert sm["ac_on_h"] == pytest.approx(3 * 5 / 60, abs=0.01) and sm["window_open_h"] == pytest.approx(5 / 60, abs=0.01)   # godziny do 0,01
    assert sm["writes"] == 1


def test_unknown_loop_and_range_are_rejected(conn):
    with pytest.raises(ValueError):
        stats.build(conn, "nope", 24, NOW)
    with pytest.raises(ValueError):
        stats.build(conn, "heiko", 48, NOW)


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "t.db")
    c = dbm.get_conn(db_path)
    dbm.migrate(c)
    heiko_row(c, datetime.now() - timedelta(minutes=30))
    c.close()
    app = create_app(db_path, lambda: {"heiko_room_target_c": 20.6, "heiko_room_min_c": 18.5}, lambda k, v: None,
                     get_state=lambda e: None, get_numeric=lambda e: None)
    return app.test_client()


def test_stats_api_returns_series_with_comfort_meta(client):
    data = client.get("/api/stats/heiko?hours=24").get_json()
    assert data["hours"] == 24 and len(data["t"]) == 1
    assert data["meta"] == {"target_c": 20.6, "room_min_c": 18.5}


def test_stats_api_defaults_to_a_week_and_validates_input(client):
    assert client.get("/api/stats/heiko").get_json()["hours"] == 168
    assert client.get("/api/stats/heiko?hours=48").status_code == 400
    assert client.get("/api/stats/heiko?hours=abc").status_code == 400
    assert client.get("/api/stats/other").status_code == 400


def test_source_split_counts_only_grid_as_peak_and_uses_real_cost(conn):
    heiko_row(conn, NOW - timedelta(minutes=30), tariff_peak=1, energy_kwh=0.5, price_pln_kwh=1.2,
              grid_kwh=0.1, battery_kwh=0.1, pv_kwh=0.3, cost_pln=0.25)          # słoneczny szczyt
    heiko_row(conn, NOW - timedelta(minutes=15), tariff_peak=1, energy_kwh=0.5, price_pln_kwh=1.2)  # sprzed 0.15.0
    out = stats.build(conn, "heiko", 24, NOW)
    s = out["series"]
    assert s["energy_peak_kwh"] == [pytest.approx(0.1), 0.5]
    assert s["energy_own_kwh"] == [pytest.approx(0.4), None]
    assert s["cost_pln"] == [pytest.approx(0.25), pytest.approx(0.6)]
    sm = out["summary"]
    assert sm["cost_pln"] == pytest.approx(0.85)
    assert sm["own_kwh"] == pytest.approx(0.4)
    assert sm["peak_share"] == pytest.approx(0.6)                                    # (0,1 + 0,5) / 1,0
