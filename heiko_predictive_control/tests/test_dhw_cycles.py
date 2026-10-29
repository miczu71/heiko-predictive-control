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
    on_group = next(g for g in groups if g["cfg_50"].startswith("Niższe"))
    off_group = next(g for g in groups if g["cfg_50"].startswith("Wyższe"))
    assert on_group["cycles"] == 2 and on_group["cycles_with_ah"] == 1 and on_group["median_ah_tick_min"] == 19.0
    assert on_group["small_sample"] is True                       # n = 2 < 5
    assert off_group["cycles"] == 1 and off_group["cycles_with_ah"] == 0


def test_group_by_config_merges_switch_and_select_forms_of_slot_50():
    def cyc(start, cfg50):
        return {"start": start, "duration_min": 25.0, "ah_tick_min": None, "ah_kwh": None, "ah_pln": None,
                "cfg_49": "off", "cfg_50": cfg50, "cfg_52": "30"}

    cycles = [cyc("2026-09-19T00:00:00+00:00", "off"),
              cyc("2026-10-07T00:00:00+00:00", "Wyższe dla grzałki wewnętrznej AH"),
              cyc("2026-10-08T00:00:00+00:00", "on"),
              cyc("2026-10-09T00:00:00+00:00", None)]
    groups = dc.group_by_config(cycles)
    by50 = {g["cfg_50"]: g for g in groups}
    assert set(by50) == {"Wyższe dla grzałki wewnętrznej AH", "Niższe dla grzałki wewnętrznej AH", None}
    assert by50["Wyższe dla grzałki wewnętrznej AH"]["cycles"] == 2     # „off” i select to jedna konfiguracja
    assert by50["Wyższe dla grzałki wewnętrznej AH"]["first"] == "2026-09-19T00:00:00+00:00"
    assert by50["Wyższe dla grzałki wewnętrznej AH"]["last"] == "2026-10-07T00:00:00+00:00"


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
    assert date(2026, 9, 21) in dc.missing_days(conn, now, back_days=3)     # rekord sprzed M1 (bez `v`) — do przeliczenia
    conn.execute("UPDATE dhw_cycles SET data = ?", ('{"v": %d}' % dc.DATA_VERSION,))
    conn.commit()
    assert date(2026, 9, 21) not in dc.missing_days(conn, now, back_days=3)


def test_segments_split_compressor_and_ah_like_cycle_of_12_oct():
    """Wzorzec z cyklu 12.10 04:00: sprężarka ~0,66 °C/min i ~1,9 kW, po wejściu AH ~0,85 °C/min i ~5,4 kW."""
    d = dict(d=12, month=10)
    mode = series((z(0, **d), "0"), (z(4, **d), "1"), (z(4, 25, **d), "0"))
    water = series((z(4, **d), "42.0"), (z(4, 5, **d), "33.7"), (z(4, 18, **d), "42.3"),
                   (z(4, 19, **d), "42.9"), (z(4, 25, **d), "48.0"))
    target = series((z(0, **d), "58"))
    ah = series((z(0, **d), "61.0"), (z(4, 19, **d), "62.0"))
    total = series((z(4, **d), "100.00"), (z(4, 5, **d), "100.20"), (z(4, 18, **d), "100.61"),
                   (z(4, 19, **d), "100.64"), (z(4, 25, **d), "101.18"))
    outdoor = series((z(0, **d), "10.8"))
    s = {"mode": mode, "water": water, "target": target, "ah": ah, "energy_total": total, "outdoor": outdoor}
    c = dc.extract_cycles(s, z(0, **d), z(23, 59, **d), TZ)[0]
    assert c["v"] == dc.DATA_VERSION
    assert c["ah_tick_min"] == 19.0
    assert c["hp_seg_min"] == 13.0 and c["hp_rate_c_min"] == pytest.approx(0.662, abs=0.001)
    assert c["hp_kw"] == pytest.approx(1.89, abs=0.01) and c["hp_water_mid_c"] == 38.0
    assert c["ah_seg_min"] == 6.0 and c["ah_rate_c_min"] == pytest.approx(0.85)
    assert c["ah_water_mid_c"] == pytest.approx(45.5, abs=0.05)         # (42,9 + 48,0)/2 zaokrąglone do 0,1
    assert c["ah_total_kw"] == pytest.approx(5.4) and c["kwh_total"] == pytest.approx(1.18)
    assert c["water_end_c"] == 48.0 and c["outdoor_c"] == 10.8


def test_segment_of_cycle_without_ah_runs_to_the_end_and_short_ones_are_skipped():
    mode = series((z(0), "0"), (z(9, 0), "1"), (z(9, 30), "0"), (z(11, 0), "1"), (z(11, 8), "0"))
    water = series((z(9, 5), "34.0"), (z(9, 30), "46.0"), (z(11, 5), "36.0"), (z(11, 8), "37.0"))
    total = series((z(9, 5), "10.00"), (z(9, 30), "10.80"))
    s = {"mode": mode, "water": water, "target": series((z(0), "48")), "ah": series((z(0), "40.0")),
         "energy_total": total}
    long_c, short_c = dc.extract_cycles(s, z(0), z(23, 59), TZ)
    assert long_c["hp_seg_min"] == 25.0 and long_c["hp_rate_c_min"] == pytest.approx(0.48)
    assert long_c["hp_kw"] == pytest.approx(1.92)
    assert long_c["ah_rate_c_min"] is None and long_c["ah_total_kw"] is None and long_c["ah_seg_min"] is None
    assert short_c["duration_min"] == 8.0 and short_c["hp_rate_c_min"] is None   # po 5 min pominięcia zostają 3 < 5


def test_cycle_record_has_minute_trace_and_v4():
    mode = series((z(0), "0"), (z(9, 0), "1"), (z(9, 5), "0"))
    water = series((z(9, 0), "40.0"), (z(9, 2), "41.0"), (z(9, 5), "44.0"))
    s = {"mode": mode, "water": water, "target": series((z(0), "48")), "ah": series((z(0), "40.0"))}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ)[0]
    assert dc.DATA_VERSION == 4 and c["v"] == 4
    assert c["trace"] == [[0, 40.0], [1, 40.0], [2, 41.0], [3, 41.0], [4, 41.0], [5, 44.0]]


def test_group_period_uses_time_order_across_dst_offsets():
    def cyc(start):
        return {"start": start, "duration_min": 25.0, "ah_tick_min": None, "ah_kwh": None, "ah_pln": None,
                "cfg_49": "off", "cfg_50": "off", "cfg_52": "30"}

    # 2026-10-25 02:30+02:00 (= 00:30 UTC) jest wcześniej niż 02:10+01:00 (= 01:10 UTC), choć tekstowo większe.
    g = dc.group_by_config([cyc("2026-10-25T02:10:00+01:00"), cyc("2026-10-25T02:30:00+02:00")])[0]
    assert g["first"] == "2026-10-25T02:30:00+02:00" and g["last"] == "2026-10-25T02:10:00+01:00"


def test_trace_is_capped_and_skips_missing_water():
    mode = series((z(0), "0"), (z(9, 0), "1"), (z(12, 0), "0"))          # 180 min
    s = {"mode": mode, "water": [], "target": series((z(0), "48")), "ah": series((z(0), "40.0"))}
    assert dc.extract_cycles(s, z(0), z(23, 59), TZ)[0]["trace"] == []
    s["water"] = series((z(9, 0), "40.0"))
    assert len(dc.extract_cycles(s, z(0), z(23, 59), TZ)[0]["trace"]) == dc.TRACE_MAX_POINTS


def test_v2_records_are_recomputed_by_backfill(conn):
    now = datetime(2026, 9, 22, 12, 0)
    conn.execute("INSERT INTO dhw_cycles (start, end, data) VALUES (?, ?, ?)",
                 ("2026-09-21T09:00:00+00:00", "2026-09-21T09:25:00+00:00", '{"v": 2}'))
    conn.commit()
    assert date(2026, 9, 21) in dc.missing_days(conn, now, back_days=3)


def test_ah_cost_prices_grid_battery_and_pv_parts_separately():
    """W szczycie (12:00) 0,4 kWh grzałki: 0,1 z sieci, 0,1 z baterii, 0,2 z PV."""
    mode = series((z(0), "0"), (z(12, 0), "1"), (z(12, 30), "0"))
    water = series((z(12, 0), "42.0"), (z(12, 30), "45.0"))
    energy = series((z(12, 0), "10.0"), (z(12, 10), "10.4"))
    grid = series((z(11), "500.0"), (z(12, 10), "500.1"))
    discharge = series((z(11), "40.0"), (z(12, 10), "40.1"))
    rce = series((z(11), "0.25"))
    s = {"mode": mode, "water": water, "target": series((z(0), "48")), "energy_c": energy,
         "grid": grid, "discharge": discharge, "rce": rce}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ, peak_price=2.0, offpeak_price=1.0,
                          battery_cost_at=lambda t: 0.5)[0]
    assert c["ah_kwh"] == pytest.approx(0.4)
    assert c["ah_pln"] == pytest.approx(round(0.1 * 2.0 + 0.1 * 0.5 + 0.2 * 0.25, 2))


def test_store_cycles_prices_battery_part_with_battery_cost_from_pump_cycles(conn):
    dbm.insert_cycle(conn, {"ts": "2026-09-21T11:45:00", "loop": "heiko", "active_profile": "ekonomia",
                            "write_enabled": 0, "battery_cost_pln_kwh": 0.5})          # czas lokalny, jak w cyklach
    states = [{"entity_id": "sensor.heiko_heat_pump_working_mode", "state": "0"}]
    asked = []

    def fake_history(entities, start, end):
        asked.extend(entities)
        return {"sensor.heiko_heat_pump_working_mode": series((z(0), "0"), (z(12), "1"), (z(12, 30), "0")),
                "sensor.ah": series((z(12), "10.0"), (z(12, 10), "10.4")),
                "sensor.power_meter_consumption": series((z(11), "500.0"), (z(12, 10), "500.0")),
                "sensor.battery_total_discharge": series((z(11), "40.0"), (z(12, 10), "40.4")),
                "sensor.current_rce_price_kwh": series((z(11), "0.25"))}

    settings = {"timezone": "Europe/Warsaw", "ah_energy_entity": "sensor.ah"}
    assert dc.store_cycles(conn, settings, date(2026, 9, 21), states, datetime(2026, 9, 22, 0, 20),
                           get_history=fake_history) == 1
    assert {"sensor.power_meter_consumption", "sensor.battery_total_discharge",
            "sensor.current_rce_price_kwh"} <= set(asked)
    assert dc.load_cycles(conn)[0]["ah_pln"] == pytest.approx(0.2)                     # 0,4 kWh z baterii × 0,5


def test_ah_cost_without_battery_history_still_splits_grid_and_pv():
    mode = series((z(0), "0"), (z(12, 0), "1"), (z(12, 30), "0"))
    s = {"mode": mode, "water": series((z(12, 0), "42.0")), "target": series((z(0), "48")),
         "energy_c": series((z(12, 0), "10.0"), (z(12, 10), "10.4")),
         "grid": series((z(11), "500.0"), (z(12, 10), "500.1")), "rce": series((z(11), "0.25"))}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ, peak_price=2.0, offpeak_price=1.0)[0]
    assert c["ah_pln"] == pytest.approx(round(0.1 * 2.0 + 0.3 * 0.25, 2))


def test_total_cycle_cost_prices_grid_battery_and_pv_parts_separately():
    """Ten sam przyrost co w teście AH, ale z licznika pompy (`energy_total`): cały cykl = ten 0,4 kWh."""
    mode = series((z(0), "0"), (z(12, 0), "1"), (z(12, 30), "0"))
    total = series((z(12, 0), "10.0"), (z(12, 10), "10.4"))
    grid = series((z(11), "500.0"), (z(12, 10), "500.1"))
    discharge = series((z(11), "40.0"), (z(12, 10), "40.1"))
    rce = series((z(11), "0.25"))
    s = {"mode": mode, "water": series((z(12, 0), "42.0")), "target": series((z(0), "48")), "energy_total": total,
         "grid": grid, "discharge": discharge, "rce": rce}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ, peak_price=2.0, offpeak_price=1.0,
                          battery_cost_at=lambda t: 0.5)[0]
    assert c["ah_kwh"] is None and c["ah_pln"] is None                  # brak energy_c — AH nietknięte
    assert c["cost_pln"] == pytest.approx(round(0.1 * 2.0 + 0.1 * 0.5 + 0.2 * 0.25, 2))
    assert c["pv_share"] == pytest.approx(0.5)                          # 0,2 z 0,4 kWh


def test_total_cycle_without_energy_total_has_no_cost_and_no_pv_share():
    mode = series((z(0), "0"), (z(12, 0), "1"), (z(12, 30), "0"))
    s = {"mode": mode, "water": series((z(12, 0), "42.0")), "target": series((z(0), "48")),
         "grid": series((z(11), "500.0"), (z(12, 10), "500.1")), "rce": series((z(11), "0.25"))}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ)[0]
    assert c["cost_pln"] is None and c["pv_share"] is None              # brak pomiaru ≠ 0


def test_total_cycle_without_grid_history_is_priced_by_tariff_and_has_no_pv_share():
    mode = series((z(0), "0"), (z(12, 0), "1"), (z(12, 30), "0"))
    s = {"mode": mode, "water": series((z(12, 0), "42.0")), "target": series((z(0), "48")),
         "energy_total": series((z(12, 0), "10.0"), (z(12, 10), "10.4"))}
    c = dc.extract_cycles(s, z(0), z(23, 59), TZ, peak_price=2.0, offpeak_price=1.0)[0]
    assert c["cost_pln"] == pytest.approx(0.8)                          # 0,4 kWh × 2,0 (szczyt), bez podziału
    assert c["pv_share"] is None


def test_cycle_zone_comes_from_tariff_feed_at_start(conn):
    class FakeHA:
        def get_state(self, entity):
            hours = [{"hour": (datetime(2026, 9, 21) + timedelta(hours=i)).strftime("%Y-%m-%dT%H"), "tariff": "G12w",
                      "zone": "peak" if 6 <= i < 22 else "offpeak", "is_peak": 6 <= i < 22,
                      "level": "expensive" if 6 <= i < 22 else "cheap", "gross": 1.5 if 6 <= i < 22 else 0.5,
                      "net": 1.0, "confidence": "invoice"} for i in range(48)]
            return {"state": "peak", "attributes": {"schedule": hours}}

    feed = dc.tariff_feed.TariffFeed(FakeHA(), conn, "sensor.tariff", None)
    assert feed.refresh(datetime(2026, 9, 21, 0, 0))
    mode = series((z(0), "0"), (z(9, 0), "1"), (z(9, 5), "0"))
    s = {"mode": mode, "water": series((z(9, 0), "40.0")), "target": series((z(0), "48"))}
    assert dc.extract_cycles(s, z(0), z(23, 59), TZ, feed=feed)[0]["zone"] == "peak"
    assert dc.extract_cycles(s, z(0), z(23, 59), TZ)[0]["zone"] is None  # bez feedu zona nieznana
