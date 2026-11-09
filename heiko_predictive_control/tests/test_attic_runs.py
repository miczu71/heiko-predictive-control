import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import attic_runs as ar
from heiko_predictive_control import db as dbm
from heiko_predictive_control import energy_source as es
from feed_stub import StubFeed
from heiko_predictive_control.tariff_feed import TariffFeed

TZ = ZoneInfo("Europe/Warsaw")


def z(h, m=0, d=27, month=10, year=2026):
    """Czas lokalny (Warszawa, UTC+2 w październiku do 25.10 — tu 27.10 to UTC+1) -> UTC."""
    return datetime(year, month, d, h, m, tzinfo=TZ).astimezone(timezone.utc)


def power_series(start_h, start_m, minutes, watts=1200, step=5):
    """Moc co `step` min: 0 przed, `watts` przez `minutes`, 0 po (punkt końcowy = pierwszy z zerem)."""
    t0 = z(start_h, start_m)
    pts = [(t0 - timedelta(minutes=step), "0")]
    for m in range(0, minutes, step):
        pts.append((t0 + timedelta(minutes=m), str(watts)))
    pts.append((t0 + timedelta(minutes=minutes), "0"))
    return pts


def test_single_run_energy_cost_and_delta():
    s = {"power": power_series(10, 0, 30),
         "energy": [(z(9, 55), "5.00"), (z(10, 30), "5.60")],
         "temp": [(z(9, 0), "18.0"), (z(10, 0), "18.0"), (z(10, 30), "20.0")],
         "outdoor": [(z(9, 0), "-2.0")]}
    runs = ar.extract_runs(s, z(0), z(23, 59), TZ, StubFeed(1.0, 0.5), offpeak_price=0.5)
    assert len(runs) == 1
    r = runs[0]
    assert r["complete"] and r["duration_min"] == 30.0 and r["v"] == ar.DATA_VERSION
    assert r["energy_kwh"] == pytest.approx(0.6) and r["energy_check"] is True
    assert r["peak_w"] == 1200
    assert r["temp_start_c"] == 18.0 and r["temp_end_c"] == 20.0 and r["delta_c"] == 2.0
    assert r["rate_c_h"] == pytest.approx(4.0) and r["kwh_per_c"] == pytest.approx(0.3)
    assert r["outdoor_c"] == -2.0
    # 10:00-10:30 lokalnie w dzień roboczy (wtorek) to poza szczytem G12w? Koszt = kWh z całki × cena zapasu.
    assert r["cost_pln"] > 0
    assert r["zone"] == "peak"                    # strefa z (atrapy) feedu


def test_gap_5_min_merged_gap_15_min_split():
    a = power_series(10, 0, 20)
    close = power_series(10, 25, 20)              # przerwa 5 min (10:20 -> 10:25)
    far = power_series(10, 35, 20)                # przerwa 15 min (10:20 -> 10:35)
    merged = ar.extract_runs({"power": a + close[1:]}, z(0), z(23, 59), TZ, StubFeed())
    split = ar.extract_runs({"power": a + far[1:]}, z(0), z(23, 59), TZ, StubFeed())
    assert len(merged) == 1 and merged[0]["duration_min"] == 45.0
    assert len(split) == 2


def test_run_at_window_end_is_incomplete():
    pts = [(z(22, 0), "0"), (z(23, 0), "1200"), (z(23, 30), "1200")]
    runs = ar.extract_runs({"power": pts}, z(0), z(23, 59), TZ, StubFeed())
    assert len(runs) == 1 and runs[0]["complete"] is False


def test_meter_vs_integral_mismatch_flags_energy_check_false():
    s = {"power": power_series(10, 0, 30),
         "energy": [(z(9, 55), "5.00"), (z(10, 30), "6.00")]}          # 1,0 kWh vs ~0,55 z całki
    r = ar.extract_runs(s, z(0), z(23, 59), TZ, StubFeed())[0]
    assert r["energy_kwh"] == pytest.approx(1.0) and r["energy_check"] is False


def test_no_meter_falls_back_to_integral():
    r = ar.extract_runs({"power": power_series(10, 0, 30)}, z(0), z(23, 59), TZ, StubFeed())[0]
    assert r["energy_kwh"] == pytest.approx(0.55, abs=0.06) and r["energy_check"] is None


def test_meter_reset_falls_back_to_integral():
    s = {"power": power_series(10, 0, 30), "energy": [(z(9, 55), "50.0"), (z(10, 30), "0.1")]}
    r = ar.extract_runs(s, z(0), z(23, 59), TZ, StubFeed())[0]
    assert r["energy_kwh"] == pytest.approx(0.55, abs=0.06) and r["energy_check"] is None


def test_hour_without_tariff_row_has_zero_integral_cost_and_no_zone():
    r = ar.extract_runs({"power": power_series(10, 0, 30)}, z(0), z(23, 59), TZ, StubFeed(missing=True))[0]
    assert r["cost_pln"] == 0.0 and r["zone"] is None and r["split_complete"] is False


def test_hour_without_tariff_row_with_meter_goes_to_no_tariff_zone():
    r = ar.extract_runs(_meter_base(), z(0), z(23, 59), TZ, StubFeed(missing=True))[0]
    assert r["cost_pln"] == 0.0 and r["split_complete"] is False
    assert set(r["zones"]) == {"no_tariff"} and r["zones"]["no_tariff"]["kwh"] == pytest.approx(0.6)


def test_feed_price_and_zone():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    conn.execute("INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                 "VALUES ('2026-10-27T10', 'x', 'cheap', 0, 'low', 0.4, 'high', '2026-10-27T00:00:00')")
    conn.commit()
    feed = TariffFeed(None, conn, "sensor.x")
    r = ar.extract_runs({"power": power_series(10, 0, 30)}, z(0), z(23, 59), TZ, feed)[0]
    assert r["zone"] == "cheap"
    assert r["cost_pln"] == pytest.approx(0.55 * 0.4, abs=0.03)


def test_store_missing_days_and_load(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {"attic_power_entity": "sensor.ac_power", "timezone": "Europe/Warsaw"}
    hist = {"sensor.ac_power": power_series(10, 0, 30)}
    now = z(12, d=28).replace(tzinfo=None)
    assert ar.store_runs(conn, settings, datetime(2026, 10, 27).date(), [],
                         get_history=lambda ents, a, b: hist) == 1
    assert datetime(2026, 10, 27).date() not in ar.missing_days(conn, now)
    assert datetime(2026, 10, 26).date() in ar.missing_days(conn, now)
    assert ar.store_runs(conn, {}, datetime(2026, 10, 27).date(), [], get_history=lambda *a: hist) == 0
    runs = ar.load_runs(conn, 36500)
    assert len(runs) == 1 and runs[0]["duration_min"] == 30.0


def test_store_runs_wires_source_entities_and_stores_split(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {"attic_power_entity": "sensor.ac_power", "attic_energy_entity": "sensor.ac_energy",
                "timezone": "Europe/Warsaw"}
    ent = es.entities(settings)
    hist = {"sensor.ac_power": power_series(12, 0, 30),
            "sensor.ac_energy": [(z(11, 55), "5.00"), (z(12, 30), "5.60")],
            ent["grid_import"]: [(z(11, 50), "100.0"), (z(12, 40), "100.0")],
            ent["battery_discharge"]: [(z(11, 50), "50.0"), (z(12, 40), "50.0")],
            ent["rce_price"]: [(z(11, 50), "0.30")]}
    asked = []

    def fake_history(ents, a, b):
        asked.extend(ents)
        return hist

    assert ar.store_runs(conn, settings, datetime(2026, 10, 27).date(), [], get_history=fake_history) == 1
    assert {ent["grid_import"], ent["battery_discharge"], ent["rce_price"]} <= set(asked)
    run = ar.load_runs(conn, 36500)[0]
    assert run["v"] == ar.DATA_VERSION and run["split_complete"] is True
    assert run["pv_share"] == pytest.approx(1.0)


def _meter_base():
    return {"power": power_series(12, 0, 30), "energy": [(z(11, 55), "5.00"), (z(12, 30), "5.60")]}


def test_cost_by_source_with_rce_in_midday():
    s = {**_meter_base(),
         "grid": [(z(11, 50), "100.0"), (z(12, 40), "100.0")],          # bez poboru z sieci
         "discharge": [(z(11, 50), "50.0"), (z(12, 40), "50.0")],       # bateria nie oddaje
         "rce": [(z(11, 50), "0.30")]}
    r = ar.extract_runs(s, z(0), z(23, 59), TZ, StubFeed(1.0, 0.5), offpeak_price=0.5)[0]
    clock = ar.extract_runs(_meter_base(), z(0), z(23, 59), TZ, StubFeed(1.0, 0.5), offpeak_price=0.5)[0]
    assert r["pv_share"] > 0 and r["split_complete"] is True
    assert r["cost_pln"] < clock["cost_pln"]
    assert sum(v["kwh"] for v in r["zones"].values()) == pytest.approx(r["energy_kwh"], abs=0.001)
    assert sum(v["pv_kwh"] for v in r["zones"].values()) > 0


def test_missing_discharge_series_is_not_counted_as_pv():
    s = {**_meter_base(), "grid": [(z(11, 50), "100.0"), (z(12, 40), "100.0")], "rce": [(z(11, 50), "0.30")]}
    r = ar.extract_runs(s, z(0), z(23, 59), TZ, StubFeed(1.0, 0.5), offpeak_price=0.5)[0]
    assert r["split_complete"] is False and r["pv_share"] is None


def test_missing_source_series_is_clock_cost():
    r = ar.extract_runs(_meter_base(), z(0), z(23, 59), TZ, StubFeed(1.0, 0.5), offpeak_price=0.5)[0]
    price = 1.0                                   # 12:30 w szczycie atrapy feedu
    assert r["split_complete"] is False and r["pv_share"] is None
    assert r["cost_pln"] == pytest.approx(0.6 * price, abs=0.01)


def test_no_meter_keeps_integral_cost():
    r = ar.extract_runs({"power": power_series(10, 0, 30)}, z(0), z(23, 59), TZ, StubFeed(2.0, 1.0), offpeak_price=1.0)[0]
    price = 2.0                                   # 10:00 w szczycie atrapy feedu
    assert r["pv_share"] is None and r["split_complete"] is False
    assert r["cost_pln"] == pytest.approx(0.55 * price, abs=0.06 * price)


def test_missing_days_treats_v1_record_as_missing():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    for day, v in (("2026-10-26", 1), ("2026-10-25", ar.DATA_VERSION)):
        conn.execute("INSERT INTO attic_runs (start, end, data) VALUES (?, ?, ?)",
                     (f"{day}T10:00:00+00:00", f"{day}T10:30:00+00:00", json.dumps({"v": v})))
    conn.commit()
    missing = ar.missing_days(conn, z(12, d=28).replace(tzinfo=None))
    assert datetime(2026, 10, 26).date() in missing
    assert datetime(2026, 10, 25).date() not in missing


def test_run_has_sources_summing_to_energy_and_v4():
    s = {"power": power_series(10, 0, 30),
         "energy": [(z(9, 55), "5.00"), (z(10, 30), "5.60")],
         "grid": [(z(9, 50), "100.0"), (z(10, 30), "100.2")],
         "discharge": [(z(9, 50), "50.0"), (z(10, 30), "50.2")],
         "rce": [(z(9, 50), "0.30")]}
    r = ar.extract_runs(s, z(0), z(23, 59), TZ, StubFeed(1.0, 0.5), offpeak_price=0.5,
                        mix_at=lambda t: {"pv": {"kwh": 1, "pln": 0.3}})[0]
    assert ar.DATA_VERSION == 4 and r["v"] == 4
    assert sum(x["kwh"] for x in r["sources"].values()) == pytest.approx(r["energy_kwh"])
    assert r["sources"]["pv"]["battery_kwh"] == pytest.approx(0.2)
