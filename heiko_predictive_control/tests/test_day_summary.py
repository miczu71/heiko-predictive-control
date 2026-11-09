import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import day_summary as ds
from heiko_predictive_control import db as dbm
from heiko_predictive_control import energy_source as es
from heiko_predictive_control.web import create_app

TZ = ZoneInfo("Europe/Warsaw")
SETTINGS = {"pump_energy_entity": "sensor.test_pump_energy", "ah_energy_entity": "sensor.test_ah_energy",
            "attic_power_entity": "sensor.test_ac_power", "attic_energy_entity": "sensor.test_ac_energy",
            "timezone": "Europe/Warsaw"}
STATES = [{"entity_id": "sensor.test_heiko_heat_pump_working_mode", "state": "2", "attributes": {}}]
MODE = "sensor.test_heiko_heat_pump_working_mode"


def z(h, m=0, d=27, month=10, year=2026):
    """Czas lokalny (Warszawa) -> UTC; 27.10.2026 to UTC+1."""
    return datetime(year, month, d, h, m, tzinfo=TZ).astimezone(timezone.utc)


def rec(kwh, pln, zones, split=True, key="energy_kwh", **extra):
    return {key: kwh, "cost_pln": pln, "zones": zones, "split_complete": split, **extra}


def zone(kwh, pln, pv):
    return {"kwh": kwh, "pln": pln, "pv_kwh": pv}


# ── a) aggregate ──

def test_aggregate_sums_zones_and_weights_pv_share_by_kwh():
    a = rec(1.0, 0.5, {"cheap": zone(1.0, 0.5, 1.0)})                 # 100% PV
    b = rec(3.0, 3.0, {"cheap": zone(1.0, 0.9, 0.0), "expensive": zone(2.0, 2.1, 0.0)})   # 0% PV
    r = ds.public(ds.aggregate([a, b], "energy_kwh"))
    assert r["kwh"] == 4.0 and r["pln"] == 3.5 and r["runs"] == 2
    assert r["zones"]["cheap"] == {"kwh": 2.0, "pln": 1.4, "pv_kwh": 1.0}
    assert r["zones"]["expensive"]["kwh"] == 2.0
    assert r["pv_share"] == 0.25            # 1/4 (średnia z procentów dałaby 0.5)
    assert r["approx"] is False
    assert not any(k.startswith("_") for k in r)


def test_aggregate_incomplete_split_is_approx_and_excluded_from_pv_share():
    a = rec(1.0, 0.5, {"cheap": zone(1.0, 0.5, 1.0)})
    b = rec(3.0, 3.0, {"cheap": zone(3.0, 3.0, 3.0)}, split=False)
    r = ds.public(ds.aggregate([a, b], "energy_kwh"))
    assert r["approx"] is True and r["pv_share"] == 1.0 and r["kwh"] == 4.0
    only_bad = ds.public(ds.aggregate([b], "energy_kwh"))
    assert only_bad["pv_share"] is None and only_bad["approx"] is True


def test_aggregate_missing_cost_contributes_only_kwh():
    r = ds.public(ds.aggregate([rec(0.8, None, None, split=False)], "energy_kwh"))
    assert r["kwh"] == 0.8 and r["pln"] == 0.0 and r["zones"] == {} and r["approx"] is True


def test_aggregate_empty_and_dhw_key_without_split_flag():
    e = ds.public(ds.aggregate([], "energy_kwh"))
    assert e["kwh"] == 0 and e["pln"] == 0 and e["runs"] == 0 and e["pv_share"] is None and e["approx"] is False
    cyc = {"kwh_total": 2.0, "cost_pln": 1.0, "pv_share": 0.5, "ah_pln": 99.0, "zones": {"cheap": zone(2.0, 1.0, 1.0)}}
    r = ds.public(ds.aggregate([cyc], "kwh_total"))
    assert r["kwh"] == 2.0 and r["pln"] == 1.0 and r["pv_share"] == 0.5 and r["approx"] is False


# ── b) combine ──

def test_combine_total_is_sum_of_parts():
    p1 = ds.aggregate([rec(1.0, 0.5, {"cheap": zone(1.0, 0.5, 1.0)})], "energy_kwh")
    p2 = ds.aggregate([rec(2.0, 1.5, {"cheap": zone(0.5, 0.3, 0.0), "expensive": zone(1.5, 1.2, 0.0)}, key="kwh_total")], "kwh_total")
    p3 = ds.aggregate([rec(1.0, 0.7, {"expensive": zone(1.0, 0.7, 0.0)}, split=False)], "energy_kwh")
    t = ds.public(ds.combine([p1, p2, p3]))
    assert t["kwh"] == 4.0 and t["pln"] == 2.7 and t["runs"] == 3 and t["approx"] is True
    assert sum(v["kwh"] for v in t["zones"].values()) == pytest.approx(t["kwh"])
    assert t["pv_share"] == 0.333           # 1 kWh PV z 3 kWh o znanym podziale (p3 bez podziału pominięty)


# ── c) build_yesterday ──

def _insert(conn, table, start, kwh_key, kwh, **extra):
    row = {"v": 1, "start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(), "complete": True,
           kwh_key: kwh, "cost_pln": kwh * 0.5, "split_complete": True,
           "zones": {"cheap": zone(kwh, kwh * 0.5, 0.0)}, **extra}
    conn.execute(f"INSERT INTO {table} (start, end, data) VALUES (?, ?, ?)", (row["start"], row["end"], json.dumps(row)))


def test_build_yesterday_filters_by_local_start_date(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    now = datetime.now(TZ)
    today = now.date()

    def at(day_offset, h, m=0):
        d = today + timedelta(days=day_offset)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(timezone.utc)

    _insert(conn, "heating_runs", at(-1, 12), "energy_kwh", 1.0)          # wczoraj
    _insert(conn, "heating_runs", at(-2, 12), "energy_kwh", 10.0)         # przedwczoraj
    _insert(conn, "heating_runs", at(0, 0, 20), "energy_kwh", 20.0)       # dziś 00:20 lokalnie (wczoraj w UTC)
    _insert(conn, "dhw_cycles", at(-1, 23, 50), "kwh_total", 2.0)         # start 23:50 lokalnie -> wczoraj
    _insert(conn, "dhw_cycles", at(-1, 0, 20), "kwh_total", 3.0)          # wczoraj 00:20 lokalnie (przedwczoraj w UTC)
    _insert(conn, "attic_runs", at(-1, 15), "energy_kwh", 0.5)
    _insert(conn, "attic_runs", at(0, 9), "energy_kwh", 7.0)
    conn.commit()
    r = ds.build_yesterday(conn, SETTINGS, now)
    assert r["day"] == (today - timedelta(days=1)).isoformat() and r["in_progress"] is False
    assert r["floor"]["kwh"] == 1.0 and r["floor"]["runs"] == 1
    assert r["dhw"]["kwh"] == 5.0 and r["dhw"]["runs"] == 2
    assert r["ac"]["kwh"] == 0.5
    assert r["total"]["kwh"] == 6.5 and r["total"]["runs"] == 4
    assert "generated_at" in r and "error" not in r


# ── d) build_today ──

NOW = z(12, 30)


def fake_history_factory(calls=None, empty=False):
    ids = es.entities({})
    full = {
        MODE: [(z(11, 55), "0"), (z(12, 0), "2")],
        "sensor.test_pump_energy": [(z(11, 55), "5.00"), (z(12, 30), "5.60")],
        ids["grid_import"]: [(z(11, 50), "100.0"), (z(12, 20), "100.0")],
        ids["battery_discharge"]: [(z(11, 50), "50.0"), (z(12, 20), "50.0")],
        ids["rce_price"]: [(z(11, 50), "0.30")],
    }

    def get_history(entities, start, end):
        if calls is not None:
            calls.append(list(entities))
        if empty:
            return {}
        return {e: full.get(e, []) for e in entities}

    return get_history


def test_build_today_run_in_progress_with_pv_share(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    r = ds.build_today(conn, SETTINGS, STATES, NOW, get_history=fake_history_factory())
    assert r["day"] == "2026-10-27" and r["in_progress"] is True
    assert r["floor"]["approx"] is True and r["floor"]["kwh"] > 0 and r["floor"]["runs"] == 1
    assert r["floor"]["pv_share"] > 0
    assert r["total"]["kwh"] == pytest.approx(r["floor"]["kwh"] + r["dhw"]["kwh"] + r["ac"]["kwh"])
    assert r["ac"]["configured"] is True
    # `pump` (kafelka „Pompa dziś”) = podłoga + CWU, bez klimatyzacji.
    assert r["pump"]["configured"] is True
    assert r["pump"]["kwh"] == pytest.approx(r["floor"]["kwh"] + r["dhw"]["kwh"], abs=0.002)
    assert r["pump"]["pln"] == pytest.approx(r["floor"]["pln"] + r["dhw"]["pln"], abs=0.011)


def test_pump_not_configured_when_no_pump_entities(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {"attic_power_entity": "sensor.test_attic_power", "timezone": "Europe/Warsaw"}
    r = ds.build_today(conn, settings, STATES, NOW, get_history=fake_history_factory())
    assert r["pump"]["configured"] is False and r["pump"]["kwh"] == 0 and r["pump"]["pln"] == 0


def test_build_today_unconfigured_component_hidden(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {"pump_energy_entity": "sensor.test_pump_energy", "timezone": "Europe/Warsaw"}
    r = ds.build_today(conn, settings, STATES, NOW, get_history=fake_history_factory())
    assert r["ac"]["configured"] is False and r["ac"]["approx"] is False and r["ac"]["kwh"] == 0
    assert r["floor"]["configured"] is True


# ── e) cache ──

def test_today_cache_ttl(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    calls, cache = [], {}
    gh = fake_history_factory(calls)
    ds.build_today(conn, SETTINGS, STATES, NOW, get_history=gh, cache=cache)
    ds.build_today(conn, SETTINGS, STATES, NOW + timedelta(seconds=100), get_history=gh, cache=cache)
    assert len(calls) == 1
    ds.build_today(conn, SETTINGS, STATES, NOW + timedelta(seconds=301), get_history=gh, cache=cache)
    assert len(calls) == 2


# ── f) brak historii ──

def test_empty_history_sets_error_and_zeroes(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    r = ds.build_today(conn, SETTINGS, STATES, NOW, get_history=fake_history_factory(empty=True))
    assert r["error"] == "brak historii z HA"
    assert r["floor"]["kwh"] == 0 and r["floor"]["approx"] is True and r["total"]["kwh"] == 0

    def boom(*a):
        raise RuntimeError("x")

    r = ds.build_today(conn, SETTINGS, STATES, NOW, get_history=boom)
    assert r["error"] == "brak historii z HA"


# ── g) web ──

def test_api_day_summary(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = dbm.get_conn(db_path)
    dbm.migrate(conn)
    conn.close()
    app = create_app(db_path, lambda: dict(SETTINGS), lambda k, v: None,
                     get_state=lambda e: None, get_numeric=lambda e: None, get_states=lambda: [])
    client = app.test_client()
    assert client.get("/api/day_summary?day=bogus").status_code == 400
    resp = client.get("/api/day_summary?day=yesterday")
    assert resp.status_code == 200
    data = resp.get_json()
    assert {"floor", "dhw", "ac", "total"} <= set(data)


def test_aggregate_combine_public_merge_sources():
    a = ds.aggregate([{"energy_kwh": 1.0, "cost_pln": 0.5, "zones": {}, "split_complete": True,
                       "sources": {"grid:cheap": {"kwh": 0.6004, "pln": 0.3, "battery_kwh": 0.0},
                                   "pv": {"kwh": 0.4, "pln": 0.1, "battery_kwh": 0.4}}},
                      {"energy_kwh": 1.0, "cost_pln": 0.5, "zones": {}, "split_complete": True,
                       "sources": {"grid:cheap": {"kwh": 0.4003, "pln": 0.2, "battery_kwh": 0.0}}}], "energy_kwh")
    b = ds.aggregate([{"energy_kwh": 1.0, "cost_pln": 0.5, "zones": {}, "split_complete": True,
                       "sources": {"unknown": {"kwh": 1.0, "pln": 0.4, "battery_kwh": 1.0}}}], "energy_kwh")
    assert a["sources"]["grid:cheap"]["kwh"] == pytest.approx(1.0007)
    out = ds.public(ds.combine([a, b]))
    assert out["sources"]["grid:cheap"] == {"kwh": 1.001, "pln": 0.5, "battery_kwh": 0.0}
    assert out["sources"]["pv"] == {"kwh": 0.4, "pln": 0.1, "battery_kwh": 0.4}
    assert out["sources"]["unknown"] == {"kwh": 1.0, "pln": 0.4, "battery_kwh": 1.0}


def test_aggregate_without_sources_gives_empty():
    assert ds.public(ds.aggregate([{"energy_kwh": 1.0, "cost_pln": 0.5, "zones": {}}], "energy_kwh"))["sources"] == {}


# ── h) build_day: doba od północy do północy + postój (0.23.0) ──

def _midnight_history(calls=None):
    """Run podłogi 26.10 22:00 → 27.10 02:00 (przez północ); licznik pompy 10.0 → 13.0 → 13.6 (postój)."""
    ids = es.entities({})
    full = {
        MODE: [(z(21, 0, d=26), "0"), (z(22, 0, d=26), "2"), (z(2, 0), "0")],
        "sensor.test_pump_energy": [(z(21, 55, d=26), "10.00"), (z(22, 0, d=26), "10.00"), (z(2, 0), "12.00"),
                                    (z(11, 0), "12.50"), (z(12, 30), "12.60")],
        ids["grid_import"]: [(z(21, 0, d=26), "100.0"), (z(12, 30), "101.0")],
        ids["battery_discharge"]: [(z(21, 0, d=26), "50.0"), (z(12, 30), "50.0")],
        ids["rce_price"]: [(z(21, 0, d=26), "0.30")],
    }

    def get_history(entities, start, end):
        if calls is not None:
            calls.append((start, end))
        return {e: full.get(e, []) for e in entities}
    return get_history


SETTINGS_PUMP = {"pump_energy_entity": "sensor.test_pump_energy", "timezone": "Europe/Warsaw"}


def _db(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    return conn


def test_build_day_splits_run_across_midnight_and_adds_standby(tmp_path):
    conn = _db(tmp_path)
    gh = _midnight_history()
    d26 = ds.build_day(conn, SETTINGS_PUMP, STATES, datetime(2026, 10, 26).date(), NOW, get_history=gh)
    d27 = ds.build_day(conn, SETTINGS_PUMP, STATES, datetime(2026, 10, 27).date(), NOW, get_history=gh)
    # 26.10: 22:00–24:00 = ~1 kWh (połowa przyrostu 2 kWh w 4 h nie jest znana — licznik skacze o 02:00)
    assert d26["floor"]["runs"] == 1 and d27["floor"]["runs"] == 1
    assert d26["in_progress"] is False and d27["in_progress"] is False      # cięcie o północy ≠ „w toku”
    assert d26["floor"]["approx"] is False and d27["floor"]["approx"] is False
    # suma pompy dobowej = przyrost licznika na dobie
    assert d26["pump"]["kwh"] + d27["pump"]["kwh"] == pytest.approx(2.60, abs=0.01)
    assert d27["standby"]["kwh"] >= 0 and d27["pump"]["kwh"] == pytest.approx(
        d27["floor"]["kwh"] + d27["dhw"]["kwh"] + d27["standby"]["kwh"], abs=0.002)
    assert d27["standby"]["kwh"] == pytest.approx(0.6, abs=0.01)           # 00:00–12:30: 12.0→12.6 minus run do 02:00
    assert d27["total"]["kwh"] == pytest.approx(d27["pump"]["kwh"] + d27["ac"]["kwh"], abs=0.002)


def test_build_day_today_run_still_running_is_in_progress(tmp_path):
    conn = _db(tmp_path)
    r = ds.build_day(conn, SETTINGS_PUMP, STATES, datetime(2026, 10, 27).date(), NOW,
                     get_history=fake_history_factory())
    assert r["in_progress"] is True and r["floor"]["approx"] is True


def test_standby_never_negative_and_zero_without_counter():
    parts = {"floor": ds.aggregate([rec(3.0, 1.0, {"cheap": zone(3.0, 1.0, 0.0)})], "energy_kwh"),
             "dhw": ds.aggregate([], "kwh_total")}
    pump = {"kwh": 2.0, "pln": 0.5, "pv_kwh": 0.0, "split_complete": True,
            "zones": {"cheap": zone(2.0, 0.5, 0.0)}, "sources": {}}
    s = ds.public(ds._standby(pump, parts))
    assert s["kwh"] == 0 and s["pln"] == 0 and s["zones"]["cheap"]["kwh"] == 0
    none = ds.public(ds._standby(None, parts))
    assert none["kwh"] == 0 and none["approx"] is True


def test_build_day_cache_per_day_with_long_ttl_for_yesterday(tmp_path):
    conn = _db(tmp_path)
    calls, cache = [], {}
    gh = _midnight_history(calls)
    y, t = datetime(2026, 10, 26).date(), datetime(2026, 10, 27).date()
    ds.build_day(conn, SETTINGS_PUMP, STATES, y, NOW, get_history=gh, cache=cache)
    ds.build_day(conn, SETTINGS_PUMP, STATES, t, NOW, get_history=gh, cache=cache)
    assert len(calls) == 2 and len(cache) == 2
    later = NOW + timedelta(seconds=400)
    ds.build_day(conn, SETTINGS_PUMP, STATES, y, later, get_history=gh, cache=cache)     # wczoraj z cache
    ds.build_day(conn, SETTINGS_PUMP, STATES, t, later, get_history=gh, cache=cache)     # dziś po TTL 300 s
    assert len(calls) == 3


def test_build_day_yesterday_without_history_falls_back_to_records(tmp_path):
    conn = _db(tmp_path)
    now = datetime.now(TZ)
    y = now.date() - timedelta(days=1)
    start = datetime(y.year, y.month, y.day, 12, tzinfo=TZ).astimezone(timezone.utc)
    _insert(conn, "heating_runs", start, "energy_kwh", 1.0)
    conn.commit()

    def boom(*a):
        raise RuntimeError("x")

    r = ds.build_day(conn, SETTINGS, STATES, y, now, get_history=boom)
    assert r["floor"]["kwh"] == 1.0 and r["floor"]["approx"] is True and "zapisanych runów" in r["error"]
