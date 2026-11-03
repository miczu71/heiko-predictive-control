"""Koszty (K2): agregacja zapisanych runów w koszyki dzień/tydzień/miesiąc, porównanie z poprzednim okresem."""
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import costs
from heiko_predictive_control import db as dbm
from heiko_predictive_control import telemetry

TZ = ZoneInfo("Europe/Warsaw")
SETTINGS = {"pump_energy_entity": "sensor.test_pump_energy", "attic_power_entity": "sensor.test_ac_power",
            "timezone": "Europe/Warsaw"}
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)
KWH_KEY = {"heating_runs": "energy_kwh", "dhw_cycles": "kwh_total", "attic_runs": "energy_kwh"}


def loc(month, day, h=12, m=0):
    """Czas lokalny (Warszawa) -> UTC."""
    return datetime(2026, month, day, h, m, tzinfo=TZ).astimezone(timezone.utc)


def zone(kwh, pln, pv):
    return {"kwh": kwh, "pln": pln, "pv_kwh": pv}


@pytest.fixture
def conn(tmp_path):
    c = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(c)
    return c


def sources_of(zones):
    """`sources` spójne ze strefami: sieć per strefa (bez PV) + osobny origin `pv`."""
    if zones is None:
        return None
    out = {}
    for name, zn in zones.items():
        if zn["kwh"] - zn["pv_kwh"] > 0:
            out[f"grid:{name}"] = {"kwh": zn["kwh"] - zn["pv_kwh"], "pln": zn["pln"], "battery_kwh": 0.0}
        if zn["pv_kwh"] > 0:
            pv = out.setdefault("pv", {"kwh": 0.0, "pln": 0.0, "battery_kwh": 0.0})
            pv["kwh"] += zn["pv_kwh"]
    return out


def insert(conn, table, start, kwh, pln=None, pv=0.0, zones="auto", minutes=30, **extra):
    pln = kwh * 0.5 if pln is None else pln
    if zones == "auto":
        zones = {"cheap": zone(kwh, pln, pv)}
    row = {"v": 7, "start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat(),
           "complete": True, KWH_KEY[table]: kwh, "cost_pln": pln, "zones": zones, "sources": sources_of(zones),
           "split_complete": True, **extra}
    conn.execute(f"INSERT INTO {table} (start, end, data) VALUES (?, ?, ?)", (row["start"], row["end"], json.dumps(row)))
    conn.commit()
    return row


def build(conn, start, end, **kw):
    kw.setdefault("now", NOW)
    return costs.build(conn, SETTINGS, start, end, **kw)


def keys(res):
    return [b["key"] for b in res["buckets"]]


# ── a) suma koszyków == suma rekordów ──

def test_bucket_sums_equal_record_sums(conn):
    recs = {"heating_runs": [], "dhw_cycles": [], "attic_runs": []}
    for d, (f, w, a) in {20: (1.5, 2.0, 0.4), 21: (2.5, 1.0, 0.0), 22: (0.75, 3.25, 1.1)}.items():
        recs["heating_runs"].append(insert(conn, "heating_runs", loc(10, d, 8), f, pv=f / 2))
        recs["dhw_cycles"].append(insert(conn, "dhw_cycles", loc(10, d, 14), w, pv=0.0, pv_share=0.0))
        recs["attic_runs"].append(insert(conn, "attic_runs", loc(10, d, 16), a, pv=a))
    res = build(conn, date(2026, 10, 20), date(2026, 10, 22))
    assert res["group"] == "day" and res["days"] == 3
    assert keys(res) == ["2026-10-20", "2026-10-21", "2026-10-22"]
    for comp, table in (("floor", "heating_runs"), ("dhw", "dhw_cycles"), ("ac", "attic_runs")):
        want_kwh = sum(r[KWH_KEY[table]] for r in recs[table])
        want_pln = sum(r["cost_pln"] for r in recs[table])
        assert sum(b[comp]["kwh"] for b in res["buckets"]) == pytest.approx(want_kwh)
        assert sum(b[comp]["pln"] for b in res["buckets"]) == pytest.approx(want_pln, abs=0.02)
        assert res["totals"][comp]["kwh"] == pytest.approx(want_kwh)
        assert res["totals"][comp]["pln"] == pytest.approx(want_pln, abs=0.01)
        assert res["totals"][comp]["runs"] == 3
    tot = res["totals"]["total"]
    assert tot["kwh"] == pytest.approx(sum(r[KWH_KEY[t]] for t, rs in recs.items() for r in rs))
    assert tot["pln"] == pytest.approx(sum(r["cost_pln"] for rs in recs.values() for r in rs), abs=0.02)
    assert res["totals"]["floor"]["pv_share"] == 0.5 and res["totals"]["ac"]["pv_share"] == 1.0
    assert res["totals"]["floor"]["unsplit_kwh"] == 0


# ── b) run przez północ ──

def test_run_across_midnight_counts_to_start_day(conn):
    insert(conn, "heating_runs", loc(10, 20, 23, 40), 1.0, minutes=60)          # 20.10 23:40 -> 21.10 00:40
    insert(conn, "heating_runs", loc(10, 19, 23, 50), 7.0, minutes=60)          # poprzednia doba, poza zakresem
    res = build(conn, date(2026, 10, 20), date(2026, 10, 21))
    by_key = {b["key"]: b for b in res["buckets"]}
    assert by_key["2026-10-20"]["floor"]["kwh"] == 1.0
    assert by_key["2026-10-21"]["floor"]["kwh"] == 0.0
    assert res["totals"]["floor"]["kwh"] == 1.0


# ── c) grupowanie ──

def test_group_week_uses_monday_keys_and_all_buckets_present(conn):
    insert(conn, "heating_runs", loc(10, 25, 12), 1.0)       # niedziela 25.10 -> tydzień od 19.10
    insert(conn, "heating_runs", loc(10, 26, 12), 2.0)       # poniedziałek 26.10 -> nowy tydzień
    res = build(conn, date(2026, 10, 19), date(2026, 11, 8), group="week")
    assert keys(res) == ["2026-10-19", "2026-10-26", "2026-11-02"]
    assert [b["floor"]["kwh"] for b in res["buckets"]] == [1.0, 2.0, 0.0]


def test_group_week_range_starting_midweek_keeps_monday_key(conn):
    res = build(conn, date(2026, 10, 22), date(2026, 10, 27), group="week")
    assert keys(res) == ["2026-10-19", "2026-10-26"]


def test_group_month_boundary(conn):
    insert(conn, "heating_runs", loc(10, 31, 23, 0), 1.0)
    insert(conn, "heating_runs", loc(11, 1, 0, 30), 2.0)
    res = build(conn, date(2026, 10, 30), date(2026, 11, 1), group="month")
    assert keys(res) == ["2026-10-01", "2026-11-01"]
    assert [b["floor"]["kwh"] for b in res["buckets"]] == [1.0, 2.0]


def test_auto_group_thresholds(conn):
    end = date(2026, 10, 31)
    for days, want in ((62, "day"), (63, "week"), (182, "week"), (183, "month")):
        res = build(conn, end - timedelta(days=days - 1), end)
        assert res["group"] == want, days
        assert res["days"] == days


def test_invalid_params_raise_value_error(conn):
    with pytest.raises(ValueError):
        build(conn, date(2026, 10, 20), date(2026, 10, 22), group="year")
    with pytest.raises(ValueError):
        build(conn, date(2026, 10, 22), date(2026, 10, 20))


# ── d) DST ──

def test_dst_25h_day_record_at_0230_belongs_to_that_day(conn):
    insert(conn, "heating_runs", datetime(2026, 10, 25, 2, 30, tzinfo=TZ).astimezone(timezone.utc), 1.0)   # pierwsze 02:30 (UTC+2)
    insert(conn, "heating_runs", datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=TZ).astimezone(timezone.utc), 2.0)  # drugie (UTC+1)
    insert(conn, "heating_runs", loc(10, 24, 23, 30), 4.0)
    insert(conn, "heating_runs", loc(10, 26, 0, 10), 8.0)
    res = build(conn, date(2026, 10, 24), date(2026, 10, 26))
    assert [b["floor"]["kwh"] for b in res["buckets"]] == [4.0, 3.0, 8.0]
    assert res["totals"]["floor"]["kwh"] == 15.0


# ── e) previous + delta ──

def test_previous_and_delta(conn):
    insert(conn, "heating_runs", loc(10, 20, 8), 4.0, pln=4.0, pv=1.0)       # poprzedni okres (20-21)
    insert(conn, "dhw_cycles", loc(10, 21, 9), 2.0, pln=3.0, pv=0.0, pv_share=0.0)
    insert(conn, "heating_runs", loc(10, 22, 8), 3.0, pln=2.0, pv=3.0)       # bieżący (22-23)
    insert(conn, "dhw_cycles", loc(10, 23, 9), 1.0, pln=1.0, pv=0.5, pv_share=0.5)
    res = build(conn, date(2026, 10, 22), date(2026, 10, 23))
    prev = res["previous"]
    assert prev["from"] == "2026-10-20" and prev["to"] == "2026-10-21"
    assert prev["totals"]["floor"]["kwh"] == 4.0 and prev["totals"]["total"]["pln"] == 7.0
    d = res["delta"]
    assert d["floor"] == {"pln": -2.0, "kwh": -1.0, "pv_share_pp": 75.0}        # 100% - 25%
    assert d["dhw"]["pv_share_pp"] == 50.0 and d["dhw"]["kwh"] == -1.0
    assert d["ac"]["pv_share_pp"] is None and d["ac"]["pln"] == 0 and d["ac"]["kwh"] == 0
    assert d["total"]["pln"] == pytest.approx(-4.0) and d["total"]["kwh"] == pytest.approx(-2.0)


def test_previous_null_when_no_records(conn):
    insert(conn, "heating_runs", loc(10, 22, 8), 3.0)
    res = build(conn, date(2026, 10, 22), date(2026, 10, 23))
    assert res["previous"] is None and res["delta"] is None


def test_delta_pv_share_pp_null_when_a_share_is_null(conn):
    insert(conn, "heating_runs", loc(10, 20, 8), 4.0, zones=None, split_complete=False)       # brak podziału -> pv_share None
    insert(conn, "heating_runs", loc(10, 22, 8), 3.0, pv=1.0)
    res = build(conn, date(2026, 10, 22), date(2026, 10, 23))
    assert res["previous"]["totals"]["floor"]["pv_share"] is None
    assert res["delta"]["floor"]["pv_share_pp"] is None
    assert res["delta"]["floor"]["kwh"] == -1.0


# ── f) unsplit ──

def test_dhw_without_zones_has_unsplit_and_approx(conn):
    insert(conn, "dhw_cycles", loc(10, 20, 9), 2.0, pln=None, zones=None, pv_share=None, v=6)
    insert(conn, "dhw_cycles", loc(10, 20, 15), 1.0, zones={"cheap": zone(0.4, 0.2, 0.0)}, pv_share=0.0)
    res = build(conn, date(2026, 10, 20), date(2026, 10, 20))
    dhw = res["buckets"][0]["dhw"]
    assert dhw["kwh"] == 3.0 and dhw["unsplit_kwh"] == 2.6 and dhw["approx"] is True
    assert res["buckets"][0]["total"]["unsplit_kwh"] == 2.6
    assert res["totals"]["dhw"]["unsplit_kwh"] == 2.6 and res["totals"]["floor"]["unsplit_kwh"] == 0


def test_unsplit_is_kwh_minus_sources_and_record_without_sources_is_all_unsplit(conn):
    insert(conn, "dhw_cycles", loc(10, 20, 9), 2.0, pv_share=0.0, sources={"pv": {"kwh": 1.5, "pln": 0.3, "battery_kwh": 0.5}})
    insert(conn, "dhw_cycles", loc(10, 20, 15), 1.0, pv_share=0.0, sources=None)
    res = build(conn, date(2026, 10, 20), date(2026, 10, 20))
    assert res["totals"]["dhw"]["unsplit_kwh"] == 1.5


def test_sources_merged_in_week_bucket_and_day_total(conn):
    insert(conn, "dhw_cycles", loc(10, 20, 9), 2.0, pv=0.5, pv_share=0.25)     # grid:cheap 1.5, pv 0.5
    insert(conn, "dhw_cycles", loc(10, 21, 9), 1.0, pv=0.25, pv_share=0.25)    # grid:cheap 0.75, pv 0.25
    insert(conn, "heating_runs", loc(10, 21, 8), 1.0, pv=0.0)                  # grid:cheap 1.0
    res = build(conn, date(2026, 10, 19), date(2026, 10, 25), group="week")
    dhw = res["buckets"][0]["dhw"]["sources"]
    assert dhw["grid:cheap"]["kwh"] == pytest.approx(2.25) and dhw["pv"]["kwh"] == pytest.approx(0.75)
    assert res["totals"]["total"]["sources"]["grid:cheap"]["kwh"] == pytest.approx(3.25)
    assert res["totals"]["dhw"]["unsplit_kwh"] == 0
    day = build(conn, date(2026, 10, 21), date(2026, 10, 21))
    assert day["buckets"][0]["total"]["sources"]["pv"]["kwh"] == pytest.approx(0.25)


# ── g) pusty zakres ──

def test_empty_range_gives_zero_buckets(conn):
    res = build(conn, date(2026, 10, 20), date(2026, 10, 22))
    assert len(res["buckets"]) == 3
    for b in res["buckets"]:
        for comp in ("floor", "dhw", "ac", "total"):
            assert b[comp]["kwh"] == 0 and b[comp]["pln"] == 0 and b[comp]["runs"] == 0
            assert b[comp]["unsplit_kwh"] == 0
        assert b["outdoor_c"] is None and b["in_progress"] is False
    assert res["totals"]["total"]["kwh"] == 0 and res["previous"] is None
    assert res["coverage"] == {"floor": {"oldest": None},
                               "dhw": {"oldest": None, "zones_from": None, "sources_from": None},
                               "ac": {"oldest": None}}
    assert res["from"] == "2026-10-20" and res["to"] == "2026-10-22" and res["generated_at"] == NOW.isoformat()


# ── h) dziś ──

def _today_result(floor_kwh=1.0):
    def part(kwh, pln, share):
        return {"kwh": kwh, "pln": pln, "zones": {"cheap": {"kwh": kwh, "pln": pln, "pv_kwh": kwh * (share or 0)}},
                "runs": 1 if kwh else 0, "approx": False, "pv_share": share, "configured": True}
    return {"day": "2026-11-02", "floor": part(floor_kwh, 0.5, 1.0), "dhw": part(0, 0, None), "ac": part(0, 0, None),
            "total": {}, "in_progress": True, "generated_at": NOW.isoformat()}


def test_today_injected_and_marked_in_progress(conn):
    insert(conn, "heating_runs", loc(11, 1, 8), 2.0)
    insert(conn, "heating_runs", loc(11, 2, 6), 50.0)               # rekord z dzisiejszą datą — ignorowany
    res = build(conn, date(2026, 11, 1), date(2026, 11, 2), today_fn=lambda: _today_result(1.0))
    assert [b["in_progress"] for b in res["buckets"]] == [False, True]
    assert res["buckets"][1]["floor"]["kwh"] == 1.0 and res["buckets"][1]["floor"]["pv_share"] == 1.0
    assert res["totals"]["floor"]["kwh"] == 3.0 and res["totals"]["total"]["runs"] == 2


def test_today_fn_failure_falls_back_to_stored_records(conn):
    insert(conn, "heating_runs", loc(11, 2, 6), 4.0)

    def boom():
        raise RuntimeError("HA niedostępne")
    res = build(conn, date(2026, 11, 1), date(2026, 11, 2), today_fn=boom)
    assert not any(b["in_progress"] for b in res["buckets"])
    assert res["buckets"][1]["floor"]["kwh"] == 4.0


def test_today_fn_not_called_when_range_ends_before_today(conn):
    def boom():
        raise AssertionError("today_fn nie powinno być wołane")
    res = build(conn, date(2026, 10, 20), date(2026, 11, 1), today_fn=boom)
    assert not any(b["in_progress"] for b in res["buckets"])


# ── i) outdoor_c ──

def put_tel(conn, ts_utc, value, name="ambient_air_temperature"):
    telemetry.store_sample(conn, ts_utc, {name: value})


def test_outdoor_from_raw_and_hourly_telemetry(conn):
    put_tel(conn, loc(10, 20, 10), 8.0)
    put_tel(conn, loc(10, 20, 11), 10.0)
    put_tel(conn, loc(10, 20, 12), 99.0, name="inny_klucz")
    kid = conn.execute("SELECT id FROM telemetry_keys WHERE name = 'ambient_air_temperature'").fetchone()["id"]
    conn.execute("INSERT INTO telemetry_hourly (hour_ts, key_id, mean, min, max, n) VALUES (?, ?, ?, ?, ?, ?)",
                 (int(loc(10, 20, 13).timestamp()) // 3600 * 3600, kid, 16.0, 15.0, 17.0, 4))
    res = build(conn, date(2026, 10, 20), date(2026, 10, 20))
    assert res["buckets"][0]["outdoor_c"] == pytest.approx((8 + 10 + 16 * 4) / 6, abs=0.05)


def test_outdoor_fallback_from_runs_then_null(conn):
    insert(conn, "heating_runs", loc(10, 20, 8), 1.0, outdoor_c=4.0)
    insert(conn, "dhw_cycles", loc(10, 20, 9), 1.0, outdoor_c=6.0, pv_share=0.0)
    insert(conn, "attic_runs", loc(10, 20, 10), 1.0, outdoor_c=None)
    res = build(conn, date(2026, 10, 20), date(2026, 10, 21))
    assert res["buckets"][0]["outdoor_c"] == 5.0
    assert res["buckets"][1]["outdoor_c"] is None


def test_outdoor_telemetry_preferred_over_runs(conn):
    insert(conn, "heating_runs", loc(10, 20, 8), 1.0, outdoor_c=4.0)
    put_tel(conn, loc(10, 20, 10), 9.0)
    assert build(conn, date(2026, 10, 20), date(2026, 10, 20))["buckets"][0]["outdoor_c"] == 9.0


# ── j) coverage ──

def test_coverage_oldest_and_zones_from(conn):
    insert(conn, "heating_runs", loc(9, 1, 8), 1.0)
    insert(conn, "heating_runs", loc(10, 1, 8), 1.0)
    insert(conn, "dhw_cycles", loc(9, 5, 23, 30), 1.0, v=5, pv_share=0.0)
    insert(conn, "dhw_cycles", loc(9, 20, 9), 1.0, v=6, pv_share=0.0)
    insert(conn, "dhw_cycles", loc(10, 3, 9), 1.0, v=7, pv_share=0.0)
    insert(conn, "dhw_cycles", loc(10, 4, 9), 1.0, v=7, pv_share=0.0)
    insert(conn, "dhw_cycles", loc(10, 6, 9), 1.0, v=8, pv_share=0.0)
    insert(conn, "attic_runs", loc(10, 15, 9), 1.0)
    res = build(conn, date(2026, 10, 20), date(2026, 10, 21))      # zakres nie obejmuje najstarszych rekordów
    assert res["coverage"] == {"floor": {"oldest": "2026-09-01"},
                               "dhw": {"oldest": "2026-09-05", "zones_from": "2026-10-03",
                                       "sources_from": "2026-10-06"},
                               "ac": {"oldest": "2026-10-15"}}


# ── loadery: granice półotwarte [start, end) ──

@pytest.mark.parametrize("module_name,table", [("heating_runs", "heating_runs"), ("attic_runs", "attic_runs"),
                                               ("dhw_cycles", "dhw_cycles")])
def test_load_range_is_half_open(conn, module_name, table):
    import importlib
    module = importlib.import_module(f"heiko_predictive_control.{module_name}")
    lo, hi = loc(10, 20, 0, 0), loc(10, 21, 0, 0)
    for start, kwh in ((lo - timedelta(minutes=1), 1.0), (lo, 2.0), (hi - timedelta(minutes=1), 4.0), (hi, 8.0)):
        insert(conn, table, start, kwh)
    got = module.load_range(conn, lo, hi)
    assert [r[KWH_KEY[table]] for r in got] == [2.0, 4.0]
