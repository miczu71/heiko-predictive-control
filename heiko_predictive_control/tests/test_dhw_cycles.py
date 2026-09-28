from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import dhw_cycles as dc

TZ = ZoneInfo("Europe/Warsaw")


def z(h, m=0, d=21, month=9, year=2026):
    """Czas lokalny (Warszawa, UTC+2 we wrześniu) -> UTC, jak w test_summaries.py."""
    return datetime(year, month, d, h, m, tzinfo=timezone.utc) - timedelta(hours=2)


def series(*points):
    return [(t, s) for t, s in points]


def test_cycle_without_ah():
    mode = series((z(0), "0"), (z(9, 0), "1"), (z(9, 30), "0"))
    water = series((z(9, 0), "42.0"), (z(9, 19), "46.0"), (z(9, 30), "48.5"))
    target = series((z(0), "48"))
    ah = series((z(0), "40.0"))
    s = {"mode": mode, "water": water, "target": target, "ah": ah}
    cycles = dc.extract_cycles(s, z(0), z(23, 59), TZ)
    assert len(cycles) == 1
    c = cycles[0]
    assert c["complete"] and c["duration_min"] == 30.0
    assert c["type"] == "zapotrzebowanie"
    assert c["target_c"] == 48.0 and c["water_start_c"] == 42.0
    assert c["water_min19_c"] == 46.0 and c["deficit_c"] == 2.0
    assert c["ah_tick_min"] is None and c["ah_min"] == 0.0
    assert c["ah_kwh"] is None and c["ah_pln"] is None       # brak encji energii = brak pomiaru, nie zero


def test_cycle_with_ah_cost_split_across_peak_boundary():
    """AH tyka po 19 min; energia rośnie w dwóch krokach — jeden w godzinie szczytu (12), drugi poza (13)."""
    mode = series((z(0), "0"), (z(12, 50), "1"), (z(13, 20), "0"))
    water = series((z(12, 50), "42.0"), (z(13, 9), "44.6"), (z(13, 20), "45.0"))
    target = series((z(0), "48"))
    ah = series((z(12, 50), "40.0"), (z(13, 9), "41.0"), (z(13, 14), "42.0"))
    energy = series((z(12, 50), "10.00"), (z(12, 55), "10.20"), (z(13, 2), "10.50"))
    s = {"mode": mode, "water": water, "target": target, "ah": ah, "energy_c": energy}
    cycles = dc.extract_cycles(s, z(0), z(23, 59), TZ, peak_price=2.0, offpeak_price=1.0)
    c = cycles[0]
    assert c["ah_tick_min"] == 19.0 and c["ah_min"] == 2.0
    assert c["ah_kwh"] == pytest.approx(0.5)
    # 12:50->12:55 (0,20 kWh, godz. 12 = szczyt) @2.0 + 12:55->13:02 (0,30 kWh, godz. 13 = poza szczytem) @1.0
    assert c["ah_pln"] == pytest.approx(round(0.20 * 2.0 + 0.30 * 1.0, 2))


def test_short_cycle_has_no_deficit():
    mode = series((z(0), "0"), (z(7, 0), "1"), (z(7, 15), "0"))       # 15 min < 19
    water = series((z(7, 0), "43.0"))
    target = series((z(0), "48"))
    s = {"mode": mode, "water": water, "target": target}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ)[0]
    assert c["duration_min"] == 15.0
    assert c["water_min19_c"] is None and c["deficit_c"] is None


def test_cycle_running_past_window_end_is_incomplete():
    mode = series((z(0), "0"), (z(23, 0), "1"))                       # trwa na granicy okna
    s = {"mode": mode}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ)[0]
    assert c["complete"] is False


def test_classify_clock_target_change_and_demand():
    mode_clock = series((z(0), "0"), (z(4, 0), "1"), (z(4, 20), "0"))
    target_flat = series((z(0), "48"))
    assert dc.extract_cycles({"mode": mode_clock, "target": target_flat}, z(0), z(23, 59), TZ)[0]["type"] == "zegar"

    mode_bath = series((z(0), "0"), (z(10, 30), "1"), (z(11, 0), "0"))
    target_change = series((z(0), "48"), (z(10, 28), "58"))            # cel zmieniony 2 min przed startem
    assert dc.extract_cycles({"mode": mode_bath, "target": target_change}, z(0), z(23, 59), TZ)[0]["type"] == "zmiana celu"

    mode_demand = series((z(0), "0"), (z(10, 30), "1"), (z(11, 0), "0"))
    assert dc.extract_cycles({"mode": mode_demand, "target": target_flat}, z(0), z(23, 59), TZ)[0]["type"] == "zapotrzebowanie"


@pytest.fixture
def conn(tmp_path):
    c = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(c)
    yield c
    c.close()


def test_config_at_unknown_before_first_change_then_tracks_changes(conn):
    when_before = z(0)
    when_after = z(12)
    assert dc.config_at(conn, when_before) == {"49": None, "50": None, "52": None}
    conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) VALUES (?, ?, ?, ?, ?, ?)",
                 (z(6).isoformat(), "backup_dhw_enabled", "switch.x", "on", "off", "użytkownik HA"))
    conn.commit()
    assert dc.config_at(conn, when_before)["49"] is None          # przed zmianą — nadal nieznane
    assert dc.config_at(conn, when_after)["49"] == "off"           # po zmianie — śledzi wartość


def test_group_by_config_counts_and_small_sample():
    def cyc(start, cfg50, ah_tick, duration=25.0, kwh=0.1, pln=0.12):
        return {"start": start, "duration_min": duration, "ah_tick_min": ah_tick,
                "ah_kwh": kwh, "ah_pln": pln, "cfg_49": "off", "cfg_50": cfg50, "cfg_52": "20"}

    cycles = [cyc("2026-09-19T00:00:00+00:00", "on", 19.0),
              cyc("2026-09-20T00:00:00+00:00", "on", None),
              cyc("2026-09-21T00:00:00+00:00", "off", None)]
    groups = dc.group_by_config(cycles)
    on_group = next(g for g in groups if g["cfg_50"] == "on")
    off_group = next(g for g in groups if g["cfg_50"] == "off")
    assert on_group["cycles"] == 2 and on_group["cycles_with_ah"] == 1 and on_group["median_ah_tick_min"] == 19.0
    assert on_group["small_sample"] is True                       # n = 2 < 5
    assert off_group["cycles"] == 1 and off_group["cycles_with_ah"] == 0


def test_store_cycles_persists_complete_cycles_only_and_is_idempotent(conn):
    states = [{"entity_id": "sensor.heiko_heat_pump_working_mode", "state": "0"},
              {"entity_id": "select.heiko_heat_pump_backup_priority_dhw", "state": "on"}]

    def fake_history(entities, start, end):
        return {"sensor.heiko_heat_pump_working_mode": series((z(0), "0"), (z(9), "1"), (z(9, 25), "0"),
                                                               (z(23), "1"))}     # drugi cykl trwa na granicy doby

    day = date(2026, 9, 21)
    now = datetime(2026, 9, 22, 0, 20)
    stored = dc.store_cycles(conn, {"timezone": "Europe/Warsaw"}, day, states, now, get_history=fake_history)
    assert stored == 1
    loaded = dc.load_cycles(conn)
    assert len(loaded) == 1 and loaded[0]["duration_min"] == 25.0

    stored_again = dc.store_cycles(conn, {"timezone": "Europe/Warsaw"}, day, states, now, get_history=fake_history)
    assert stored_again == 1
    assert len(dc.load_cycles(conn)) == 1                          # INSERT OR REPLACE — bez duplikatu


def test_missing_days(conn):
    now = datetime(2026, 9, 22, 0, 20)
    assert date(2026, 9, 21) in dc.missing_days(conn, now, back_days=3)
    conn.execute("INSERT INTO dhw_cycles (start, end, data) VALUES (?, ?, ?)",
                 ("2026-09-21T09:00:00+00:00", "2026-09-21T09:25:00+00:00", "{}"))
    conn.commit()
    assert date(2026, 9, 21) not in dc.missing_days(conn, now, back_days=3)
