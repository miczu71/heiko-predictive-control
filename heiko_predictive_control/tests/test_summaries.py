from datetime import date, datetime, timedelta, timezone

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import summaries as sm

TZ = "Europe/Warsaw"
DAY = date(2026, 9, 21)                       # poniedziałek, UTC+2 → doba lokalna = 20.09 22:00Z … 21.09 22:00Z


def z(h, m=0, d=21):
    """Czas lokalny (Warszawa, UTC+2) → UTC."""
    return datetime(2026, 9, d, h, m, tzinfo=timezone.utc) - timedelta(hours=2)


def series(*points):
    return [(t, s) for t, s in points]


def test_modes_dhw_cycles_and_minutes():
    mode = series((z(0, d=20), "0"), (z(6), "1"), (z(6, 40), "0"), (z(10), "2"), (z(12), "0"),
                  (z(13), "4"), (z(13, 30), "0"))
    out = sm.summarize_day(DAY, {"mode": mode}, TZ)["modes"]
    assert out["dhw_cycles"] == 2 and out["dhw_min"] == 70.0            # 06:00-06:40 + 13:00-13:30
    assert out["heating_runs"] == 2 and out["heating_min"] == 150.0     # 10-12 (tryb 2) + 13:00-13:30 (tryb 4 = CWU+grzanie)
    assert out["standby_min"] == 1440 - 70 - 120                        # tryb 4 nie jest postojem; 2 h grzania (tryb 2)


def test_compressor_starts_short_cycles_and_mean_hz():
    freq = series((z(0, d=20), "0"), (z(6), "40"), (z(6, 5), "0"),          # 5 min — krótki cykl
                  (z(9), "60"), (z(10), "0"))                                # 60 min
    out = sm.summarize_day(DAY, {"freq": freq}, TZ)["compressor"]
    assert out["starts"] == 2 and out["short_cycles"] == 1
    assert out["run_min"] == 65.0 and out["mean_run_min"] == 32.5
    assert out["mean_hz"] == round((5 * 40 + 60 * 60) / 65, 1)


def test_run_continuing_from_previous_day_counts_from_midnight():
    freq = series((z(22, d=20), "50"), (z(1), "0"))                           # start przed północą lokalną
    out = sm.summarize_day(DAY, {"freq": freq}, TZ)["compressor"]
    assert out["run_min"] == 60.0 and out["starts"] == 1


def test_backup_counters_are_minutes_with_ah_and_reset_ignored():
    """Liczniki czasu pracy grzałek w integracji są w MINUTACH; AH = grzałka pomocnicza."""
    ah = series((z(0, d=20), "40.0"), (z(19), "48.0"), (z(19, 30), "50.0"))
    hbh = series((z(0, d=20), "100.0"), (z(12), "100.5"), (z(23, d=21), "101.25"))
    hwtbh = series((z(1), "500"), (z(20), "3"))                            # reset licznika
    out = sm.summarize_day(DAY, {"mode": series((z(0, d=20), "0")), "ah": ah, "hbh": hbh, "hwtbh": hwtbh}, TZ)["backup"]
    assert out == {"ah_min": 10.0, "hbh_min": 1.2, "hwtbh_min": None}     # 50−40 min; 101,25−100 → 1,25 → 1,2 (zaokr. do 0,1)


def test_p0_pulses():
    p1 = series((z(0, d=20), "off"), (z(3), "on"), (z(3, 1), "off"), (z(4), "on"), (z(4, 2), "off"))
    out = sm.summarize_day(DAY, {"mode": series((z(0, d=20), "0")), "p1": p1}, TZ)["p0"]
    assert out["pulses"] == 2 and out["on_min"] == 3.0 and out["mean_pulse_min"] == 1.5


def test_energy_peak_split_by_local_hour_and_workday():
    energy = series((z(0, d=20), "1000.0"), (z(7), "1001.0"),                 # +1 kWh o 07:00 = szczyt (pn)
                    (z(14), "1001.5"),                                        # +0,5 kWh o 14:00 = poza szczytem
                    (z(23, d=21) - timedelta(minutes=30), "1003.5"))          # +2 kWh o 22:30 = poza szczytem
    out = sm.summarize_day(DAY, {"mode": series((z(0, d=20), "0")), "energy": energy}, TZ)["energy"]
    assert out["kwh"] == 3.5 and out["kwh_peak"] == 1.0 and out["peak_share"] == round(1 / 3.5, 4)
    weekend = sm.summarize_day(DAY, {"mode": series((z(0, d=20), "0")), "energy": energy}, TZ,
                               is_workday=lambda d: False)["energy"]
    assert weekend["kwh_peak"] == 0.0


def test_no_data_returns_empty():
    assert sm.summarize_day(DAY, {}, TZ) == {}


@pytest.fixture
def conn(tmp_path):
    c = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(c)
    yield c
    c.close()


def test_store_day_persists_and_missing_days(conn):
    states = [{"entity_id": "sensor.heiko_heat_pump_working_mode", "state": "0"},
              {"entity_id": "sensor.heiko_heat_pump_compressor_frequency", "state": "0"}]
    seen = {}

    def fake_history(entities, start, end):
        seen["entities"] = entities
        return {"sensor.heiko_heat_pump_working_mode": series((z(0, d=20), "0"), (z(6), "1"), (z(7), "0")),
                "sensor.heiko_heat_pump_compressor_frequency": series((z(0, d=20), "0"), (z(6), "40"), (z(7), "0"))}

    now = datetime(2026, 9, 22, 0, 20)
    done = sm.store_day(conn, {"timezone": TZ}, DAY, states, now, get_history=fake_history)
    assert set(done) == {"modes", "compressor"}
    assert set(seen["entities"]) == {"sensor.heiko_heat_pump_working_mode", "sensor.heiko_heat_pump_compressor_frequency"}
    loaded = sm.load_summaries(conn)
    assert loaded["2026-09-21"]["modes"]["dhw_cycles"] == 1
    missing = sm.missing_days(conn, now, back_days=3)
    assert date(2026, 9, 21) not in missing and date(2026, 9, 20) in missing and date(2026, 9, 22) not in missing


def test_store_day_without_history_stores_nothing(conn):
    assert sm.store_day(conn, {"timezone": TZ}, DAY, [], datetime(2026, 9, 22), get_history=lambda *a: None) == {}
    assert conn.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 0


def test_migration_drops_old_summaries_once(tmp_path):
    """0.8.3: streszczenia sprzed zmiany jednostek (godziny→minuty) są kasowane jednorazowo."""
    c = dbm.get_conn(str(tmp_path / "m.db"))
    c.executescript(dbm._SCHEMA)
    c.execute("INSERT INTO daily_summary (day, topic, data) VALUES ('2026-09-20', 'backup', '{\"hbh_h\": 1}')")
    c.commit()
    dbm.migrate(c)
    assert c.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 0
    assert c.execute("SELECT value FROM settings WHERE key = 'summary_schema'").fetchone()["value"] == "3"
    c.execute("INSERT INTO daily_summary (day, topic, data) VALUES ('2026-09-21', 'backup', '{\"ah_min\": 1}')")
    c.commit()
    dbm.migrate(c)                                                          # znacznik ustawiony -> drugi start nie kasuje
    assert c.execute("SELECT COUNT(*) FROM daily_summary").fetchone()[0] == 1
    c.close()


def test_counter_unchanged_all_day_is_zero_not_missing():
    """Licznik bez zmian w dobie ma w historii jeden punkt — to 0 min, a nie brak danych (inaczej średnie są zawyżone)."""
    quiet = series((z(0, d=20), "42.0"))
    out = sm.summarize_day(DAY, {"mode": series((z(0, d=20), "0")), "ah": quiet}, TZ)["backup"]
    assert out["ah_min"] == 0.0
    assert sm.summarize_day(DAY, {"mode": series((z(0, d=20), "0"))}, TZ).get("backup") is None     # brak licznika = brak danych
