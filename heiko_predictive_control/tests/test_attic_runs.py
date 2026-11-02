import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import attic_runs as ar
from heiko_predictive_control import db as dbm
from heiko_predictive_control import tariff
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
    runs = ar.extract_runs(s, z(0), z(23, 59), TZ, peak_price=1.0, offpeak_price=0.5)
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
    assert r["zone"] is None                      # bez feedu brak strefy


def test_gap_5_min_merged_gap_15_min_split():
    a = power_series(10, 0, 20)
    close = power_series(10, 25, 20)              # przerwa 5 min (10:20 -> 10:25)
    far = power_series(10, 35, 20)                # przerwa 15 min (10:20 -> 10:35)
    merged = ar.extract_runs({"power": a + close[1:]}, z(0), z(23, 59), TZ)
    split = ar.extract_runs({"power": a + far[1:]}, z(0), z(23, 59), TZ)
    assert len(merged) == 1 and merged[0]["duration_min"] == 45.0
    assert len(split) == 2


def test_run_at_window_end_is_incomplete():
    pts = [(z(22, 0), "0"), (z(23, 0), "1200"), (z(23, 30), "1200")]
    runs = ar.extract_runs({"power": pts}, z(0), z(23, 59), TZ)
    assert len(runs) == 1 and runs[0]["complete"] is False


def test_meter_vs_integral_mismatch_flags_energy_check_false():
    s = {"power": power_series(10, 0, 30),
         "energy": [(z(9, 55), "5.00"), (z(10, 30), "6.00")]}          # 1,0 kWh vs ~0,55 z całki
    r = ar.extract_runs(s, z(0), z(23, 59), TZ)[0]
    assert r["energy_kwh"] == pytest.approx(1.0) and r["energy_check"] is False


def test_no_meter_falls_back_to_integral():
    r = ar.extract_runs({"power": power_series(10, 0, 30)}, z(0), z(23, 59), TZ)[0]
    assert r["energy_kwh"] == pytest.approx(0.55, abs=0.06) and r["energy_check"] is None


def test_meter_reset_falls_back_to_integral():
    s = {"power": power_series(10, 0, 30), "energy": [(z(9, 55), "50.0"), (z(10, 30), "0.1")]}
    r = ar.extract_runs(s, z(0), z(23, 59), TZ)[0]
    assert r["energy_kwh"] == pytest.approx(0.55, abs=0.06) and r["energy_check"] is None


def test_no_feed_uses_g12w_fallback_price():
    # wtorek 27.10 10:00-10:30 lokalnie
    r = ar.extract_runs({"power": power_series(10, 0, 30)}, z(0), z(23, 59), TZ, peak_price=2.0, offpeak_price=1.0)[0]
    peak = tariff.fallback_hour(datetime(2026, 10, 27, 10), True).is_peak
    price = 2.0 if peak else 1.0
    assert r["cost_pln"] == pytest.approx(0.55 * price, abs=0.06 * price)


def test_feed_price_and_zone():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    conn.execute("INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                 "VALUES ('2026-10-27T10', 'x', 'cheap', 0, 'low', 0.4, 'high', '2026-10-27T00:00:00')")
    conn.commit()
    feed = TariffFeed(None, conn, "sensor.x")
    r = ar.extract_runs({"power": power_series(10, 0, 30)}, z(0), z(23, 59), TZ, feed=feed)[0]
    assert r["zone"] == "cheap"
    assert r["cost_pln"] == pytest.approx(0.55 * 0.4, abs=0.03)


def test_store_missing_days_load_and_summarize(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {"attic_power_entity": "sensor.ac_power", "timezone": "Europe/Warsaw"}
    hist = {"sensor.ac_power": power_series(10, 0, 30)}
    now = z(12, d=28).replace(tzinfo=None)
    assert ar.store_runs(conn, settings, datetime(2026, 10, 27).date(), [], now,
                         get_history=lambda ents, a, b: hist) == 1
    assert datetime(2026, 10, 27).date() not in ar.missing_days(conn, now)
    assert datetime(2026, 10, 26).date() in ar.missing_days(conn, now)
    assert ar.store_runs(conn, {}, datetime(2026, 10, 27).date(), [], now, get_history=lambda *a: hist) == 0
    runs = ar.load_runs(conn, 36500)
    assert len(runs) == 1 and runs[0]["duration_min"] == 30.0
    days = ar.summarize(runs, TZ)
    assert days[0]["day"] == "2026-10-27" and days[0]["runs"] == 1
    assert days[0]["hours"] == pytest.approx(0.5) and days[0]["kwh"] == pytest.approx(runs[0]["energy_kwh"])
    assert days[0]["pln"] == pytest.approx(runs[0]["cost_pln"])
    assert json.dumps(days)
