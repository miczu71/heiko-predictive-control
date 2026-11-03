from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import heating_runs as hr
from heiko_predictive_control import tariff
from heiko_predictive_control.tariff_feed import TariffFeed

TZ = ZoneInfo("Europe/Warsaw")


def z(h, m=0, d=27, month=10, year=2026):
    """Czas lokalny (Warszawa) -> UTC; 27.10.2026 to UTC+1."""
    return datetime(year, month, d, h, m, tzinfo=TZ).astimezone(timezone.utc)


def mode_series(start_h, start_m, minutes, code="2", before="0"):
    """Tryb pracy: `before` przed, `code` przez `minutes`, 0 po."""
    t0 = z(start_h, start_m)
    return [(t0 - timedelta(minutes=5), before), (t0, code), (t0 + timedelta(minutes=minutes), "0")]


def base(start_h=12, start_m=0, minutes=30, kwh=0.6):
    t0 = z(start_h, start_m)
    return {"mode": mode_series(start_h, start_m, minutes),
            "energy": [(t0 - timedelta(minutes=5), "5.00"), (t0 + timedelta(minutes=minutes), f"{5.0 + kwh:.2f}")]}


def run_one(s, **kw):
    runs = hr.extract_runs(s, z(0), z(23, 59), TZ, peak_price=1.0, offpeak_price=0.5, **kw)
    assert len(runs) == 1
    return runs[0]


def test_midday_with_rce_has_pv_share():
    s = {**base(),
         "grid": [(z(11, 50), "100.0"), (z(12, 40), "100.0")],
         "discharge": [(z(11, 50), "50.0"), (z(12, 40), "50.0")],
         "rce": [(z(11, 50), "0.30")],
         "water": [(z(11, 0), "30.0"), (z(12, 30), "34.0")], "outdoor": [(z(11, 0), "3.0")]}
    r = run_one(s)
    assert r["v"] == hr.DATA_VERSION and r["complete"] and r["duration_min"] == 30.0
    assert r["pv_share"] > 0 and r["split_complete"] is True
    assert r["energy_kwh"] == pytest.approx(0.6)
    assert r["water_start_c"] == 30.0 and r["water_end_c"] == 34.0 and r["outdoor_c"] == 3.0


def test_run_across_zone_boundary_has_two_zones():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    for hour, zone, gross in (("2026-10-27T12", "expensive", 2.0), ("2026-10-27T13", "cheap", 1.0)):
        conn.execute("INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                     "VALUES (?, 'G12w', ?, 0, NULL, ?, NULL, '2026-10-27T00:00')", (hour, zone, gross))
    conn.commit()
    feed = TariffFeed(None, conn, "sensor.x")
    s = {"mode": mode_series(12, 40, 40),
         "energy": [(z(12, 35), "5.00"), (z(12, 55), "5.20"), (z(13, 5), "5.40"), (z(13, 20), "5.70")]}
    r = run_one(s, feed=feed)
    assert len(r["zones"]) >= 2
    assert sum(v["kwh"] for v in r["zones"].values()) == pytest.approx(r["energy_kwh"], abs=0.001)


def test_missing_source_series_is_clock_cost():
    r = run_one(base())
    price = tariff.price_for(tariff.fallback_hour(datetime(2026, 10, 27, 12, 30), True).is_peak, 1.0, 0.5)
    assert r["split_complete"] is False and r["pv_share"] is None
    assert r["cost_pln"] == pytest.approx(0.6 * price, abs=0.01)


def test_gap_5_min_merged_gap_15_min_split():
    a = mode_series(10, 0, 20)
    merged = hr.extract_runs({"mode": a[:-1] + [(z(10, 20), "0"), (z(10, 25), "2"), (z(10, 45), "0")], "energy": [(z(9, 55), "5"), (z(10, 45), "6")]},
                             z(0), z(23, 59), TZ)
    split = hr.extract_runs({"mode": a[:-1] + [(z(10, 20), "0"), (z(10, 35), "2"), (z(10, 55), "0")],
                             "energy": [(z(9, 55), "5"), (z(10, 55), "6")]}, z(0), z(23, 59), TZ)
    assert len(merged) == 1 and merged[0]["duration_min"] == 45.0
    assert len(split) == 2


def test_incomplete_run_at_window_end_and_carried_mode():
    end_run = hr.extract_runs({"mode": [(z(22, 0), "0"), (z(23, 0), "2")], "energy": [(z(22, 0), "5.0"), (z(23, 30), "5.5")]},
                              z(0), z(23, 59), TZ)
    assert len(end_run) == 1 and end_run[0]["complete"] is False
    carried = hr.extract_runs({"mode": [(z(23, 0, d=26), "2"), (z(1, 0), "0")], "energy": [(z(23, 0, d=26), "5.0"), (z(1, 0), "5.5")]},
                              z(0), z(23, 59), TZ)
    assert len(carried) == 1 and carried[0]["complete"] is False


def test_modes_4_and_1_make_no_run():
    for code in ("4", "1"):
        assert hr.extract_runs({**base(), "mode": mode_series(12, 0, 30, code=code)}, z(0), z(23, 59), TZ) == []


def test_no_meter_gives_none_cost():
    r = run_one({"mode": mode_series(12, 0, 30)})
    assert r["energy_kwh"] == 0.0 and r["cost_pln"] is None and r["zones"] is None
    assert r["pv_share"] is None and r["split_complete"] is False


def test_store_runs_without_pump_energy_entity_does_not_query(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)

    def boom(*a):
        raise AssertionError("get_history nie powinno być wołane")

    assert hr.store_runs(conn, {"timezone": "Europe/Warsaw"}, datetime(2026, 10, 27).date(), [], get_history=boom) == 0
    assert conn.execute("SELECT COUNT(*) FROM heating_runs").fetchone()[0] == 0


def test_store_missing_days_and_load(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {"pump_energy_entity": "sensor.test_pump_energy", "timezone": "Europe/Warsaw"}
    states = [{"entity_id": "sensor.test_heiko_heat_pump_working_mode", "state": "2", "attributes": {}}]
    s = base()
    hist = {"sensor.test_heiko_heat_pump_working_mode": s["mode"], "sensor.test_pump_energy": s["energy"]}
    asked = []

    def fake_history(ents, a, b):
        asked.extend(ents)
        return hist

    day = datetime(2026, 10, 27).date()
    assert hr.store_runs(conn, settings, day, states, get_history=fake_history) == 1
    assert "sensor.test_pump_energy" in asked
    now = z(12, d=28).replace(tzinfo=None)
    assert day not in hr.missing_days(conn, now)
    assert datetime(2026, 10, 26).date() in hr.missing_days(conn, now)
    runs = hr.load_runs(conn, 36500)
    assert len(runs) == 1 and runs[0]["duration_min"] == 30.0 and runs[0]["v"] == hr.DATA_VERSION


def test_store_long_run_started_before_previous_midnight_is_complete(tmp_path):
    """Run 20:30–01:30 (start > 3 h przed północą) ma być zapisany przez job doby, w której się kończy."""
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {"pump_energy_entity": "sensor.test_pump_energy", "timezone": "Europe/Warsaw"}
    states = [{"entity_id": "sensor.test_heiko_heat_pump_working_mode", "state": "0", "attributes": {}}]
    t0, t1 = z(20, 30, d=26), z(1, 30, d=27)
    hist = {"sensor.test_heiko_heat_pump_working_mode": [(t0 - timedelta(minutes=5), "0"), (t0, "2"), (t1, "0")],
            "sensor.test_pump_energy": [(t0 - timedelta(minutes=5), "5.00"), (t1, "8.00")]}
    day = datetime(2026, 10, 27).date()
    assert hr.store_runs(conn, {**settings}, day, states, get_history=lambda e, a, b: hist) == 1
    assert hr.load_runs(conn, 36500)[0]["duration_min"] == 300.0


def test_run_has_sources_summing_to_energy_and_v2():
    s = {**base(),
         "grid": [(z(11, 50), "100.0"), (z(12, 30), "100.2")],
         "discharge": [(z(11, 50), "50.0"), (z(12, 30), "50.2")],
         "rce": [(z(11, 50), "0.30")]}
    r = run_one(s, mix_at=lambda t: {"pv": {"kwh": 1, "pln": 0.3}})
    assert hr.DATA_VERSION == 2 and r["v"] == 2
    assert sum(x["kwh"] for x in r["sources"].values()) == pytest.approx(r["energy_kwh"])
    assert r["sources"]["pv"]["battery_kwh"] == pytest.approx(0.2)


def test_run_without_meter_has_no_sources():
    r = run_one({"mode": mode_series(12, 0, 30)})
    assert r["sources"] is None
