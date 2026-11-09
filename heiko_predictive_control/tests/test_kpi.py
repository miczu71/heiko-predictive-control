import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import kpi, tariff_feed


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


def _zone(conn, ts, zone):
    conn.execute("INSERT OR REPLACE INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                 "VALUES (?, 'G13', ?, ?, 'mid', 1.0, 'document', 'x')", (ts.strftime("%Y-%m-%dT%H"), zone, int(zone != "other")))


def test_recent_kpi_reads_last_week_from_cycles():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    now = datetime(2026, 1, 14, 12, 0)
    for i in range(40):
        _zone(conn, now - timedelta(hours=i), "evening_peak" if i % 2 else "other")
        dbm.insert_cycle(conn, {"ts": (now - timedelta(hours=i)).isoformat(), "loop": "heiko",
                                "active_profile": "ekonomia", "write_enabled": 0, "energy_kwh": 0.5,
                                "tariff_peak": i % 2, "outdoor_temp_c": 2.0})
    dbm.insert_cycle(conn, {"ts": (now - timedelta(days=9)).isoformat(), "loop": "heiko",
                            "active_profile": "ekonomia", "write_enabled": 0, "energy_kwh": 50.0,
                            "tariff_peak": 1, "outdoor_temp_c": 2.0})     # poza oknem 7 dni
    k = kpi.recent_kpi(conn, now, None)
    assert k["kwh"] == pytest.approx(20.0) and k["share"] == pytest.approx(0.5)


def test_peak_share_counts_only_grid_energy_in_peak():
    rows = ([{"energy_kwh": 1.0, "tariff_peak": 1, "outdoor_temp_c": 2.0, "grid_kwh": 0.25}] * 5    # słońce
            + [{"energy_kwh": 1.0, "tariff_peak": 0, "outdoor_temp_c": 2.0, "grid_kwh": 1.0}] * 5
            + [{"energy_kwh": 1.0, "tariff_peak": 1, "outdoor_temp_c": 2.0, "grid_kwh": None}] * 2)   # sprzed 0.15.0
    k = kpi.peak_share_kpi(rows, None)
    assert k["share"] == pytest.approx((5 * 0.25 + 2 * 1.0) / 12, abs=1e-4)


def test_baseline_counts_peak_hours_covered_by_own_energy_as_not_peak():
    stats = _winter_stats(days=28)
    stats["sensor.grid"] = [{"start": r["start"], "change": 0.0} for r in stats["sensor.energy"]]  # zawsze PV/bateria
    b = kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy", grid="sensor.grid")
    assert b["overall"] == 0.0


def _cycle(conn, ts, **kw):
    dbm.insert_cycle(conn, {"ts": ts.isoformat(), "loop": "heiko", "active_profile": "ekonomia",
                            "write_enabled": 0, "outdoor_temp_c": 2.0, **kw})


def test_recent_kpi_takes_grid_share_per_hour_like_the_lts_baseline():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    now = datetime(2026, 1, 14, 12, 0)                               # środa
    hour = datetime(2026, 1, 14, 10, 0)                              # szczyt (strefa popołudniowa z feedu)
    _zone(conn, hour, "evening_peak")
    for i, (imp, grid) in enumerate([(2.0, 0.5), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)]):
        _cycle(conn, hour + timedelta(minutes=15 * i), energy_kwh=0.5, tariff_peak=1,
               grid_kwh=grid, grid_import_kwh=imp)
    for i in range(12):                                              # noc, poza szczytem
        _cycle(conn, datetime(2026, 1, 14, 1, 0) + timedelta(minutes=15 * i), energy_kwh=0.5, tariff_peak=0,
               grid_kwh=0.5, grid_import_kwh=0.5)
    k = kpi.recent_kpi(conn, now, None)
    assert k["kwh"] == pytest.approx(8.0)
    assert k["share"] == pytest.approx(2.0 / 8.0, abs=1e-4)          # min(2,0 kWh pompy, 2,0 kWh poboru) w godzinie


def test_baseline_evening_only_counts_16_21_workdays():
    stats = _winter_stats(days=28)
    b = kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy", evening_only=True)
    # tydzień: 5 dni × 5 h popołudniowego szczytu × 1,0 = 25 kWh z 70 + 49 kWh łącznie
    assert b["overall"] == pytest.approx(25 / (70 + 49), abs=0.005)


def test_recent_kpi_uses_evening_zone_only():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    # środa 14.01.2026: 12:00 = poranny szczyt, 17:00 = popołudniowy; oba mają zapisane tariff_peak = 1
    _zone(conn, datetime(2026, 1, 14, 12), "morning_peak")
    _zone(conn, datetime(2026, 1, 14, 17), "evening_peak")
    for h in (12, 17):
        _cycle(conn, datetime(2026, 1, 14, h), energy_kwh=4.0, tariff_peak=1)
    for h in (3, 4):
        _cycle(conn, datetime(2026, 1, 14, h), energy_kwh=4.0, tariff_peak=0)
    k = kpi.recent_kpi(conn, datetime(2026, 1, 15, 8, 0), None)
    assert k["share"] == pytest.approx(4.0 / 16.0, abs=0.001)    # tylko 17:00; poranny szczyt nie liczy się


def test_recent_kpi_fallback_window_without_feed_rows():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    # bez wierszy tariff_hours: tylko środa 17:00 to szczyt popołudniowy (stary poranny szczyt i sobota — nie)
    _cycle(conn, datetime(2026, 1, 14, 17), energy_kwh=4.0, tariff_peak=1)     # środa
    _cycle(conn, datetime(2026, 1, 14, 12), energy_kwh=4.0, tariff_peak=1)     # poranny szczyt zapisany jako 1
    _cycle(conn, datetime(2026, 1, 10, 17), energy_kwh=4.0, tariff_peak=1)     # sobota
    _cycle(conn, datetime(2026, 1, 14, 3), energy_kwh=4.0, tariff_peak=0)
    k = kpi.recent_kpi(conn, datetime(2026, 1, 15, 8, 0), None)
    assert k["share"] == pytest.approx(4.0 / 16.0, abs=0.001)




# ── S3: strefy z pv_roi, sygnatura, baza v4 ─────────────────────────────────────

WARSAW = ZoneInfo("Europe/Warsaw")


def _hkey(utc_dt):
    return utc_dt.astimezone(WARSAW).strftime("%Y-%m-%dT%H")


def _zones_for(stats, zone_fn):
    """`{godzina lokalna: (strefa, is_peak)}` dla każdej godziny statystyk."""
    out = {}
    for r in stats["sensor.energy"]:
        utc = datetime.fromtimestamp(r["start"] / 1000, tz=timezone.utc)
        zone = zone_fn(utc.astimezone(WARSAW))
        out[_hkey(utc)] = (zone, zone != "other")
    return out


def _evening_16_21(local):
    return "evening_peak" if local.weekday() < 5 and 16 <= local.hour < 21 else "other"


def test_baseline_with_zones_counts_evening_zone_hours_as_peak():
    stats = _winter_stats(days=28)
    zones = _zones_for(stats, lambda l: "evening_peak" if 10 <= l.hour < 12 else "other")   # inne okno niż zegar
    b = kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy", zones=zones)
    # doba: 2 h szczytu × 1,0 (energia w tych godzinach zależy od zegara G12w, więc liczymy z danych)
    peak = sum(r["change"] for r in stats["sensor.energy"]
               if zones[_hkey(datetime.fromtimestamp(r["start"] / 1000, tz=timezone.utc))][0] == "evening_peak")
    total = sum(r["change"] for r in stats["sensor.energy"])
    assert b["overall"] == pytest.approx(peak / total, abs=0.001) and b["hours"] == 28 * 24


def test_baseline_with_zones_matches_clock_window_when_zones_say_16_21():
    stats = _winter_stats(days=28)
    b = kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy", zones=_zones_for(stats, _evening_16_21))
    assert b["overall"] == pytest.approx(25 / (70 + 49), abs=0.005)


def test_baseline_with_zones_skips_hours_without_entry():
    stats = _winter_stats(days=28)
    zones = _zones_for(stats, _evening_16_21)
    dropped = [k for k in zones if k.endswith("T17")]
    for k in dropped:
        del zones[k]
    b = kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy", zones=zones)
    assert b["hours"] == 28 * 24 - len(dropped)
    total = sum(r["change"] for r in stats["sensor.energy"]
                if _hkey(datetime.fromtimestamp(r["start"] / 1000, tz=timezone.utc)) in zones)
    assert b["kwh"] == pytest.approx(total, abs=0.1)


def test_baseline_with_zones_still_skips_summer_and_warm():
    summer = _winter_stats(days=10, start=datetime(2026, 7, 5, tzinfo=timezone.utc))
    assert kpi.baseline_from_stats(summer, "sensor.owm", "sensor.energy",
                                   zones=_zones_for(summer, _evening_16_21)) is None
    warm = _winter_stats(days=10, temp=20.0)
    assert kpi.baseline_from_stats(warm, "sensor.owm", "sensor.energy", zones=_zones_for(warm, _evening_16_21)) is None


def _put_hours(conn, start_local, hours, zone_fn, fetched_at=""):
    for i in range(hours):
        t = start_local + timedelta(hours=i)
        zone = zone_fn(t)
        conn.execute("INSERT OR REPLACE INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                     "VALUES (?, 'G13', ?, ?, 'mid', 1.0, 'document', ?)",
                     (t.strftime("%Y-%m-%dT%H"), zone, int(zone != "other"), fetched_at))
    conn.commit()


def test_tariff_signature_changes_with_zone():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    start = datetime(2025, 12, 1)
    _put_hours(conn, start, 48, _evening_16_21)
    sig = kpi.tariff_signature(conn, start, start + timedelta(hours=48))
    assert len(sig) == 12 and sig == kpi.tariff_signature(conn, start, start + timedelta(hours=48))
    conn.execute("UPDATE tariff_hours SET zone = 'morning_peak' WHERE hour = '2025-12-01T17'")
    assert kpi.tariff_signature(conn, start, start + timedelta(hours=48)) != sig


SETTINGS = {"heiko_bootstrap_outdoor_entity": "sensor.owm", "pump_energy_entity": "sensor.energy",
            "pv_roi_url": "http://pv:8099"}
NOW = datetime(2026, 9, 26, 12, 0)
STATS = _winter_stats(days=28, start=datetime(2025, 12, 1, tzinfo=timezone.utc))


class Hooks:
    """Fałszywe `fetch_history` i `get_statistics` z licznikami wywołań."""
    def __init__(self, conn, zone_fn=_evening_16_21, history_ok=True, stats=STATS):
        self.conn, self.zone_fn, self.history_ok, self.stats = conn, zone_fn, history_ok, stats
        self.history_calls, self.stat_calls = [], []

    def fetch_history(self, conn, base_url, start, end):
        self.history_calls.append((base_url, start, end))
        if self.history_ok:
            _put_hours(conn, datetime(2025, 12, 1), 29 * 24, self.zone_fn)
        return self.history_ok

    def get_statistics(self, ids, *a):
        self.stat_calls.append(ids)
        return self.stats

    def run(self, now=NOW):
        return kpi.ensure_peak_baseline(self.conn, SETTINGS, now, get_statistics=self.get_statistics,
                                        fetch_history=self.fetch_history)


def _fresh():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    return conn


def test_ensure_first_baseline_is_v4_with_signature():
    conn = _fresh()
    h = Hooks(conn)
    b = h.run()
    assert b["v"] == 4 and len(b["sig"]) == 12 and b["overall"] == pytest.approx(25 / 119, abs=0.005)
    assert h.history_calls[0][0] == "http://pv:8099" and "sensor.power_meter_consumption" in h.stat_calls[0]
    assert dbm.get_setting(conn, "peak_baseline")["sig"] == b["sig"]
    assert dbm.get_setting(conn, "peak_baseline_checked")


def test_ensure_second_call_within_24h_calls_nothing():
    conn = _fresh()
    h = Hooks(conn)
    first = h.run()
    h.history_calls.clear()
    h.stat_calls.clear()
    assert h.run(NOW + timedelta(hours=23)) == first
    assert h.history_calls == [] and h.stat_calls == []


def test_ensure_history_failure_keeps_old_v3_baseline_untouched():
    conn = _fresh()
    old = {"overall": 0.2, "kwh": 1.0, "hours": 1, "v": 3}
    dbm.set_setting(conn, "peak_baseline", old)
    h = Hooks(conn, history_ok=False)
    assert h.run() == old
    assert dbm.get_setting(conn, "peak_baseline") == old
    assert dbm.get_setting(conn, "peak_baseline_checked") is None
    assert h.stat_calls == []


def test_ensure_statistics_none_keeps_old_baseline():
    conn = _fresh()
    old = {"overall": 0.2, "kwh": 1.0, "hours": 1, "v": 3}
    dbm.set_setting(conn, "peak_baseline", old)
    h = Hooks(conn, stats=None)
    assert h.run() == old
    assert dbm.get_setting(conn, "peak_baseline") == old and dbm.get_setting(conn, "peak_baseline_checked") is None


def test_ensure_after_24h_same_signature_skips_statistics():
    conn = _fresh()
    h = Hooks(conn)
    first = h.run()
    h.stat_calls.clear()
    later = h.run(NOW + timedelta(hours=25))
    assert later["sig"] == first["sig"] and h.stat_calls == [] and len(h.history_calls) == 2
    assert dbm.get_setting(conn, "peak_baseline_checked") == (NOW + timedelta(hours=25)).isoformat(timespec="minutes")


def test_ensure_zone_change_triggers_recompute():
    conn = _fresh()
    h = Hooks(conn)
    first = h.run()
    h.zone_fn = lambda l: "evening_peak" if l.weekday() < 5 and 18 <= l.hour < 21 else "other"
    second = h.run(NOW + timedelta(hours=25))
    assert second["sig"] != first["sig"] and second["overall"] != first["overall"] and len(h.stat_calls) == 2


def test_ensure_old_baseline_stays_until_v4_is_written():
    conn = _fresh()
    dbm.set_setting(conn, "peak_baseline", {"overall": 0.4, "kwh": 1.0, "hours": 1, "v": 2})
    h = Hooks(conn)
    b = h.run()
    assert b["v"] == 4 and dbm.get_setting(conn, "peak_baseline")["v"] == 4


def test_unknown_zone_warns_once_per_hour(caplog):
    tariff_feed._warned.clear()
    stats = _winter_stats(days=28)
    zones = _zones_for(stats, lambda l: "mystery_zone")
    with caplog.at_level(logging.WARNING):
        kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy", zones=zones)
        kpi.baseline_from_stats(stats, "sensor.owm", "sensor.energy", zones=zones)
    assert len([r for r in caplog.records if "mystery_zone" in r.getMessage()]) == 1


def test_peak_flag_with_non_peak_zone_warns_inconsistency(caplog):
    tariff_feed._warned.clear()
    with caplog.at_level(logging.WARNING):
        tariff_feed.warn_unknown_zone("offpeak", True, datetime(2026, 1, 5, 10))
        tariff_feed.warn_unknown_zone("evening_peak", True, datetime(2026, 1, 5, 10))
        tariff_feed.warn_unknown_zone("evening_peak", False, datetime(2026, 1, 5, 10))
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1 and "offpeak" in msgs[0]


def test_evening_rows_warn_on_unknown_zone(caplog):
    tariff_feed._warned.clear()
    conn = _fresh()
    _put_hours(conn, datetime(2026, 1, 14, 10), 1, lambda t: "mystery_zone", fetched_at="x")
    row = {"ts": "2026-01-14T10:00:00", "tariff_peak": 1, "energy_kwh": 1.0}
    with caplog.at_level(logging.WARNING):
        kpi._evening_rows(conn, [row])
    assert any("mystery_zone" in r.getMessage() for r in caplog.records)
