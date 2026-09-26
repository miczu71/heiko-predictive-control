from datetime import datetime, timedelta, timezone

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import kpi


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _winter_stats(days=30, peak_kwh=1.0, off_kwh=0.5, temp=2.0, start=datetime(2026, 1, 5, tzinfo=timezone.utc)):
    """Godzinowe LTS: w szczycie (pn–pt 6–13, 15–22 czasu lokalnego) energia peak_kwh, poza off_kwh."""
    from zoneinfo import ZoneInfo
    from heiko_predictive_control.tariff import is_peak_hour
    tz = ZoneInfo("Europe/Warsaw")
    out, en = [], []
    for h in range(days * 24):
        t = start + timedelta(hours=h)
        local = t.astimezone(tz)
        e = peak_kwh if is_peak_hour(local.hour, local.weekday() < 5) else off_kwh
        out.append({"start": _ms(t), "mean": temp})
        en.append({"start": _ms(t), "change": e})
    return {"sensor.owm": out, "sensor.energy": en}


def test_baseline_share_matches_synthetic_schedule():
    stats = _winter_stats(days=28)                      # 4 pełne tygodnie
    b = kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy")
    # tydzień: 5 dni × 14 h szczytu × 1,0 = 70 kWh szczytu; reszta 168−70 = 98 h × 0,5 = 49 kWh
    assert b["overall"] == pytest.approx(70 / (70 + 49), abs=0.005)
    assert b["by_class"]["3"]["hours"] == 28 * 24        # 2°C → klasa 3 (zaokrąglenie do 3)
    assert b["hours"] == 28 * 24


def test_baseline_skips_summer_and_warm_hours():
    summer = _winter_stats(days=10, start=datetime(2026, 7, 5, tzinfo=timezone.utc))
    assert kpi.baseline_from_stats(summer, "sensor.owm", "sensor.energy") is None
    warm = _winter_stats(days=10, temp=20.0)
    assert kpi.baseline_from_stats(warm, "sensor.owm", "sensor.energy") is None


def _rows(peak_kwh, off_kwh, n_each=20, temp=2.0):
    rows = [{"energy_kwh": peak_kwh, "tariff_peak": 1, "outdoor_temp_c": temp} for _ in range(n_each)]
    rows += [{"energy_kwh": off_kwh, "tariff_peak": 0, "outdoor_temp_c": temp} for _ in range(n_each)]
    return rows


def test_peak_share_and_shift_against_baseline():
    baseline = kpi.baseline_from_stats(_winter_stats(days=28), "sensor.owm", "sensor.energy")
    shifted = kpi.peak_share_kpi(_rows(peak_kwh=0.2, off_kwh=1.0), baseline)
    assert shifted["share"] == pytest.approx(0.2 / 1.2, abs=1e-3)
    assert shifted["baseline_share"] == pytest.approx(baseline["overall"], abs=1e-3)
    assert shifted["delta_pp"] < -20                       # wyraźne przesunięcie poza szczyt
    same_mix = [{"energy_kwh": 70.0, "tariff_peak": 1, "outdoor_temp_c": 2.0},
                {"energy_kwh": 49.0, "tariff_peak": 0, "outdoor_temp_c": 2.0}]     # jak zimą: 70/119
    assert abs(kpi.peak_share_kpi(same_mix, baseline)["delta_pp"]) < 0.5


def test_expected_share_reweights_by_outdoor_temperature_class():
    baseline = {"overall": 0.5, "by_class": {"3": {"share": 0.2, "hours": 100},
                                             "-3": {"share": 0.8, "hours": 100}}}
    rows = ([{"energy_kwh": 1.0, "tariff_peak": 1, "outdoor_temp_c": 3.0}] * 5
            + [{"energy_kwh": 1.0, "tariff_peak": 0, "outdoor_temp_c": -3.0}] * 5)
    k = kpi.peak_share_kpi(rows, baseline)
    assert k["baseline_share"] == pytest.approx(0.5)       # 50/50 energii z klas 0,2 i 0,8
    thin = {"overall": 0.5, "by_class": {"3": {"share": 0.9, "hours": 3}}}       # za mało godzin w klasie
    k2 = kpi.peak_share_kpi([{"energy_kwh": 6.0, "tariff_peak": 1, "outdoor_temp_c": 3.0}], thin)
    assert k2["baseline_share"] == pytest.approx(0.5)      # fallback na udział ogólny


def test_kpi_needs_enough_energy_and_ignores_unknown_tariff():
    assert kpi.peak_share_kpi(_rows(0.01, 0.01, n_each=3), None) is None
    rows = [{"energy_kwh": 10.0, "tariff_peak": None, "outdoor_temp_c": 1.0}]
    assert kpi.peak_share_kpi(rows, None) is None
    assert kpi.peak_share_kpi(_rows(1.0, 1.0), None)["baseline_share"] is None


def test_ensure_peak_baseline_stores_once_and_retries_without_stats():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    settings = {"heiko_bootstrap_outdoor_entity": "sensor.owm", "pump_energy_entity": "sensor.energy"}
    now = datetime(2026, 9, 26, 12, 0)
    assert kpi.ensure_peak_baseline(conn, settings, now, get_statistics=lambda *a: None) is None
    assert dbm.get_setting(conn, "peak_baseline") is None              # spróbuje przy następnym starcie
    stats = _winter_stats(days=28, start=datetime(2025, 12, 1, tzinfo=timezone.utc))
    b = kpi.ensure_peak_baseline(conn, settings, now, get_statistics=lambda *a: stats)
    assert b["overall"] > 0 and dbm.get_setting(conn, "peak_baseline")["hours"] == 28 * 24
    calls = []
    kpi.ensure_peak_baseline(conn, settings, now, get_statistics=lambda *a: calls.append(1))
    assert calls == []                                                 # raz policzona — bez ponownego pobierania


def test_recent_kpi_reads_last_week_from_cycles():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    now = datetime(2026, 1, 14, 12, 0)
    for i in range(40):
        dbm.insert_cycle(conn, {"ts": (now - timedelta(hours=i)).isoformat(), "loop": "heiko",
                                "active_profile": "ekonomia", "write_enabled": 0, "energy_kwh": 0.5,
                                "tariff_peak": i % 2, "outdoor_temp_c": 2.0})
    dbm.insert_cycle(conn, {"ts": (now - timedelta(days=9)).isoformat(), "loop": "heiko",
                            "active_profile": "ekonomia", "write_enabled": 0, "energy_kwh": 50.0,
                            "tariff_peak": 1, "outdoor_temp_c": 2.0})     # poza oknem 7 dni
    k = kpi.recent_kpi(conn, now, None)
    assert k["kwh"] == pytest.approx(20.0) and k["share"] == pytest.approx(0.5)
