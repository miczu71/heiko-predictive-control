"""E3: katalog miar testkit (`measures`): doby wariantów, miary arm/test, `evaluate`. Bez HA (atrapy)."""
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import floor_learn as fl
from heiko_predictive_control.testkit import measures as ms
from heiko_predictive_control.testkit import params
from heiko_predictive_control.testkit.measures import ArmDay, Ctx

TZ = ZoneInfo("Europe/Warsaw")
SETTINGS = {"timezone": "Europe/Warsaw", "day_zone_temp_entities": "sensor.z1,sensor.z2"}
NOW = datetime(2026, 10, 20, 12, 0)
FRI = date(2026, 10, 9)


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def ctx(conn, test=None, day_kwh=None, settings=SETTINGS):
    return ms.Ctx(conn, settings, NOW, test, day_kwh or (lambda *a: None))


def day(d, arm="A", start="00:00", hours=24):
    s = datetime.combine(d, time.fromisoformat(start))
    return ArmDay(arm, s, s + timedelta(hours=hours))


def cycle(conn, ts, outdoor=None, min_room=None, loop="heiko", **cols):
    keys = ["ts", "loop", "active_profile", "write_enabled", "outdoor_temp_c", "min_room_c", *cols]
    vals = [ts.isoformat(), loop, "komfort", 1, outdoor, min_room, *cols.values()]
    conn.execute(f"INSERT INTO cycles ({','.join(keys)}) VALUES ({','.join('?' * len(vals))})", vals)
    conn.commit()


def run(conn, start_utc, cost, complete=True):
    data = {"start": start_utc.isoformat(), "end": (start_utc + timedelta(hours=1)).isoformat(),
            "complete": complete, "cost_pln": cost}
    conn.execute("INSERT INTO heating_runs (start, end, data) VALUES (?, ?, ?)",
                 (start_utc.isoformat(), data["end"], json.dumps(data)))
    conn.commit()


def energy_row(conn, d, kwh=1.0):
    conn.execute("INSERT INTO daily_summary (day, topic, data) VALUES (?, 'energy', ?)",
                 (d.isoformat(), json.dumps({"kwh": kwh})))
    conn.commit()


def local_to_utc(naive):
    from datetime import timezone
    return naive.replace(tzinfo=TZ).astimezone(timezone.utc)


# --- katalog ---

def test_catalog_keys_match_params_and_scopes():
    assert set(ms.CATALOG) == set(params.MEASURES)
    assert all(m.scope in ("arm", "test") for m in ms.CATALOG.values())
    assert {n for n, m in ms.CATALOG.items() if m.scope == "test"} == {"excitation", "inertia_refit", "writes_confirmed"}


# --- doby ---

def test_arm_days_from_steps_last_step_is_not_a_day():
    steps = [{"at": "2026-10-08T00:00", "value": 1.0, "stop": 6.0}, {"at": "2026-10-09T00:00", "value": 5.0, "stop": 5.0},
             {"at": "2026-10-10T00:00", "value": 1.0, "stop": 6.0}]
    days = ms.arm_days_from_steps(steps, lambda s: ex._fmt((s["value"], s["stop"])))
    assert [(d.arm, d.start, d.end) for d in days] == [
        ("1/6", datetime(2026, 10, 8), datetime(2026, 10, 9)), ("5/5", datetime(2026, 10, 9), datetime(2026, 10, 10))]


def test_arm_days_from_plan_friday_ends_saturday_and_skipped_days_make_no_days():
    days = ms.arm_days_from_plan({"2026-10-09": "A", "2026-10-12": "B"}, "13:00→13:00")
    assert [(d.arm, d.start, d.end) for d in days] == [
        ("A", datetime(2026, 10, 9, 13), datetime(2026, 10, 10, 13)),
        ("B", datetime(2026, 10, 12, 13), datetime(2026, 10, 13, 13))]
    assert ms.arm_days_from_plan({}, "13:00→13:00") == []


# --- kowariata, kwh_day ---

def test_outdoor_covariate_mean_of_daily_means_and_none(conn):
    d1, d2 = day(date(2026, 10, 8)), day(date(2026, 10, 9))
    assert ms.outdoor_c(ctx(conn), [d1, d2]) is None
    cycle(conn, datetime(2026, 10, 8, 6), outdoor=4.0)
    cycle(conn, datetime(2026, 10, 8, 18), outdoor=6.0)
    cycle(conn, datetime(2026, 10, 9, 12), outdoor=9.0)
    cycle(conn, datetime(2026, 10, 9, 13), outdoor=99.0, loop="attic")                  # inna pętla nie liczy się
    cycle(conn, datetime(2026, 10, 10, 0), outdoor=50.0)                                 # [start, end): koniec wyłączony
    assert ms.outdoor_c(ctx(conn), [d1, d2]) == 7.0


def test_kwh_day_averages_days_with_value():
    values = {date(2026, 10, 8): 10.0, date(2026, 10, 9): 11.0, date(2026, 10, 10): None}
    seen = []

    def fake(conn, settings, d, now):
        seen.append((d, now))
        return values[d]

    out = ms.kwh_day(ms.Ctx(None, SETTINGS, NOW, None, fake), [day(d) for d in values])
    assert out == {"value": 10.5, "n": 2} and seen[0] == (date(2026, 10, 8), NOW)
    assert ms.kwh_day(ms.Ctx(None, SETTINGS, NOW, None, lambda *a: None), [day(FRI)]) == {"value": None, "n": 0}


# --- zone_spread ---

def _samples(conn, d, spread, avg=21.0, n=12):
    base = int(datetime.combine(d, time(1)).replace(tzinfo=TZ).timestamp())
    for name in ("working_mode", "sensor.z1", "sensor.z2"):
        conn.execute("INSERT OR IGNORE INTO telemetry_keys (name) VALUES (?)", (name,))
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM telemetry_keys")}
    for i in range(n):
        for name, v in (("working_mode", 0), ("sensor.z1", avg + spread / 2), ("sensor.z2", avg - spread / 2)):
            conn.execute("INSERT INTO telemetry (ts, key_id, value) VALUES (?, ?, ?)", (base + i * 300, ids[name], v))
    conn.commit()


def test_zone_spread_pools_days_of_variant(conn):
    _samples(conn, date(2026, 10, 8), 1.0, n=6)               # 6 próbek to za mało osobno (min. 8) — pula wariantu wystarcza
    _samples(conn, date(2026, 10, 10), 1.0, n=6)
    out = ms.zone_spread(ctx(conn), [day(date(2026, 10, 8)), day(date(2026, 10, 10))])
    assert out == {"value": 1.0, "spread_c": 1.0, "zones_avg_c": 21.0, "samples": 12, "n": 2}


def test_zone_spread_no_samples_or_no_zones(conn):
    assert ms.zone_spread(ctx(conn), [day(FRI)]) == {"value": None, "spread_c": None, "zones_avg_c": None,
                                                     "samples": 0, "n": 1}
    out = ms.zone_spread(ctx(conn, settings={"timezone": "Europe/Warsaw"}), [day(FRI)])
    assert out["value"] is None


# --- floor_cost ---

def friday_window_day():
    return ArmDay("A", datetime.combine(FRI, time(13)), datetime.combine(FRI, time(13)) + timedelta(days=1))


def test_floor_cost_friday_window_runs_into_saturday(conn):
    run(conn, local_to_utc(datetime(2026, 10, 10, 7)), 2.5)             # sobota 07:00 -> doba piątku
    run(conn, local_to_utc(datetime(2026, 10, 9, 14)), 1.25)
    run(conn, local_to_utc(datetime(2026, 10, 10, 13)), 9.0)            # sobota 13:00 -> już następna doba
    run(conn, local_to_utc(datetime(2026, 10, 9, 12)), 9.0)             # piątek 12:00 -> poprzednia doba
    out = ms.floor_cost(ctx(conn), [friday_window_day()], "13:00→13:00")
    assert out == {"value": 3.75, "n": 1, "per_day": [{"day": "2026-10-09", "value": 3.75}]}


def test_floor_cost_skips_day_without_data_but_counts_energy_row_as_zero(conn):
    d1, d2, d3 = (day(date(2026, 10, n), start="13:00") for n in (5, 6, 7))
    run(conn, local_to_utc(datetime(2026, 10, 5, 14)), 3.0)
    energy_row(conn, d3.start.date())                                    # brak runów, ale telemetria dnia jest = koszt 0
    out = ms.floor_cost(ctx(conn), [d1, d2, d3], "13:00→13:00")
    assert out["n"] == 2 and out["value"] == 1.5
    assert [p["day"] for p in out["per_day"]] == ["2026-10-05", "2026-10-07"]
    assert ms.floor_cost(ctx(conn), [d2], "13:00→13:00") == {"value": None, "n": 0, "per_day": []}


def test_floor_cost_skips_day_with_unknown_cost_and_counts_incomplete_run(conn):
    d1, d2 = day(date(2026, 10, 5)), day(date(2026, 10, 6))
    run(conn, local_to_utc(datetime(2026, 10, 5, 14)), 1.0)
    run(conn, local_to_utc(datetime(2026, 10, 5, 15)), None)
    run(conn, local_to_utc(datetime(2026, 10, 6, 14)), 2.0, complete=False)
    out = ms.floor_cost(ctx(conn), [d1, d2], "13:00→13:00")
    assert out["n"] == 1 and out["value"] == 2.0


def test_floor_cost_per_hdd_and_rounding(conn):
    d1, d2, d3, d4 = (day(date(2026, 10, n)) for n in (5, 6, 7, 8))
    for d, out, cost in ((d1, 4.0, 10.0), (d2, 13.5, 5.0), (d3, None, 5.0), (d4, 4.0, 1.0)):
        run(conn, local_to_utc(d.start.replace(hour=14)), cost)
        if out is not None:
            cycle(conn, d.start.replace(hour=15), outdoor=out)
    res = ms.floor_cost(ctx(conn), [d1, d2, d3, d4], "13:00→13:00", per="hdd")
    # d2: hdd 0,5 < 1 -> pominięta; d3: brak temp. zewn. -> pominięta; d1: 10/10 = 1,0; d4: 1/10 = 0,1
    assert res["n"] == 2 and res["value"] == 0.55
    assert res["per_day"] == [{"day": "2026-10-05", "value": 1.0}, {"day": "2026-10-08", "value": 0.1}]
    cycle(conn, datetime(2026, 10, 8, 16), outdoor=5.0)                  # d4: średnia 4,5 -> hdd 9,5
    assert ms.floor_cost(ctx(conn), [d4], "13:00→13:00", per="hdd")["per_day"] == [{"day": "2026-10-08", "value": 0.105}]
    assert ms.floor_cost(ctx(conn), [d4], "13:00→13:00", per="hdd")["value"] == 0.105


# --- min_room_at, safety_trips ---

def test_min_room_at_takes_worst_day_within_half_hour_and_ignores_null(conn):
    d1, d2, d3 = (day(date(2026, 10, n), start="13:00") for n in (5, 6, 7))
    cycle(conn, datetime(2026, 10, 5, 20, 30), min_room=21.34)
    cycle(conn, datetime(2026, 10, 5, 21, 30), min_room=20.9)
    cycle(conn, datetime(2026, 10, 5, 21, 31), min_room=10.0)                  # poza oknem
    cycle(conn, datetime(2026, 10, 5, 21, 0), min_room=None)
    cycle(conn, datetime(2026, 10, 6, 21, 0), min_room=22.0)
    cycle(conn, datetime(2026, 10, 7, 21, 0), min_room=None)                    # tylko NULL -> doba pominięta
    res = ms.min_room_at(ctx(conn), [d1, d2, d3], "21:00")
    assert res == {"value": 20.9, "n": 2, "per_day": [{"day": "2026-10-05", "value": 20.9},
                                                     {"day": "2026-10-06", "value": 22.0}]}
    assert ms.min_room_at(ctx(conn), [day(date(2026, 10, 20))], "21:00") == {"value": None, "n": 0, "per_day": []}


def test_min_room_at_first_occurrence_not_before_start(conn):
    d = day(date(2026, 10, 5), start="22:00")                                  # 21:00 wypada już po północy
    cycle(conn, datetime(2026, 10, 5, 21, 0), min_room=15.0)
    cycle(conn, datetime(2026, 10, 6, 21, 0), min_room=19.0)
    assert ms.min_room_at(ctx(conn), [d], "21:00")["value"] == 19.0


def test_safety_trips_counts_variant_days_in_state(conn):
    days = [day(date(2026, 10, n)) for n in (5, 6, 7)]
    assert ms.safety_trips(ctx(conn), days) == {"value": 0, "n": 3}              # brak stanu
    dbm.set_setting(conn, "g13_peak_state", {"trips": ["2026-10-06", "2026-10-07", "2026-09-01"]})
    assert ms.safety_trips(ctx(conn), days) == {"value": 2, "n": 3}


# --- miary testowe ---

def _test_row(conn, kind="bezwladnosc", started="2026-10-01T10:00:00"):
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES (?, 'w_toku', ?, '[]', 0)",
                 (kind, started))
    conn.commit()
    return conn.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 1").fetchone()


def test_excitation_measure_wraps_floor_learn(conn):
    t = _test_row(conn)
    for i in range(8):
        cycle(conn, datetime(2026, 10, 2, i), reduced_active=i % 2, water_setpoint_c=30.0, base_curve_c=33.0,
              curve_shift_c=0.0)
    cycle(conn, datetime(2026, 9, 1), reduced_active=0, water_setpoint_c=30.0, base_curve_c=33.0, curve_shift_c=0.0)
    out = ms.CATALOG["excitation"].fn(ctx(conn, t))
    assert out["value"] is True and out["detail"] == {"transitions": 7, "mean_drop_c": 3.0}
    assert ms.CATALOG["excitation"].fn(ctx(conn, _test_row(conn, started="2026-12-01T00:00:00"))) == {
        "value": False, "detail": {"transitions": 0, "mean_drop_c": 0.0}}


def test_inertia_refit_measure_calls_refit_with_window(conn, monkeypatch):
    calls = []

    def fake(c, settings, now, **kw):
        calls.append((now, kw))
        return {"ok": True, "reason": "ok", "rmse_c": 0.1, "tau_h": 7.0, "identified": True, "extra": 1}

    monkeypatch.setattr(fl, "refit_from_cycles", fake)
    out = ms.CATALOG["inertia_refit"].fn(ctx(conn, _test_row(conn)))
    assert out == {"ok": True, "reason": "ok", "rmse_c": 0.1, "tau_h": 7.0, "identified": True}
    assert calls == [(NOW, {"excitation_since": "2026-10-01T10:00:00"})]


def test_writes_confirmed_counts_like_legacy(conn):
    t = _test_row(conn, kind="zapis")
    for ts, conf in (("2026-10-07T10:00:00", "2026-10-07T10:03:00"), ("2026-10-07T10:10:00", "2026-10-07T10:14:30"),
                     ("2026-10-07T10:20:00", ex.NO_CONFIRMATION), ("2026-10-07T10:30:00", ex.SUPERSEDED),
                     ("2026-10-07T10:40:00", None)):
        conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok, confirmed_at) "
                     "VALUES (?, ?, 'number.x', 0, 1, 1, ?)", (ts, t["id"], conf))
    conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok) VALUES ('2026-10-07T11:00:00', 99, 'n', 0, 1, 1)")
    out = ms.CATALOG["writes_confirmed"].fn(ctx(conn, t))
    assert out == {"writes": 5, "confirmed": 2, "max_confirm_min": 4.5, "value": 2}


# --- evaluate ---

PREHEAT = {
    "measures": [{"name": "floor_cost", "window": "13:00→13:00", "per": "hdd"}, {"name": "min_room_at", "at": "21:00"},
                 "safety_trips", "kwh_day"],
    "verdict": {"primary": "floor_cost", "better": "lower", "guards": [{"measure": "min_room_at", "op": ">=", "value": 20.5}],
                "weather_band_c": 3, "min_n": 6},
}


def test_evaluate_preheat_ab_weekend_gives_low_confidence(conn):
    days = {"2026-10-05": "B", "2026-10-06": "A", "2026-10-09": "B"}                  # n < min_n
    plan_days = ms.arm_days_from_plan(days, "13:00→13:00")
    for iso, arm in days.items():
        d = date.fromisoformat(iso)
        run(conn, local_to_utc(datetime.combine(d, time(14))), 10.0 if arm == "B" else 20.0)
        cycle(conn, datetime.combine(d, time(15)), outdoor=4.0)
        cycle(conn, datetime.combine(d, time(21)), min_room=21.0)
    res = ms.evaluate(PREHEAT, ctx(conn, day_kwh=lambda c, s, d, n: 12.0), plan_days)
    assert set(res["arms"]) == {"A", "B"} and res["test"] == {}
    b = res["arms"]["B"]
    assert (b["days"], b["outdoor_c"], b["value"]) == (2, 4.0, 1.0)
    assert b["measures"]["floor_cost"]["n"] == 2 and b["measures"]["kwh_day"]["value"] == 12.0
    assert b["measures"]["min_room_at"]["value"] == 21.0 and b["measures"]["safety_trips"]["value"] == 0
    v = res["verdict"]
    assert (v["winner"], v["confidence"]) == ("B", "niska")


def test_evaluate_no_primary_arm_means_no_verdict(conn):
    t = _test_row(conn, kind="zapis")
    defn = {"measures": ["writes_confirmed"], "verdict": {"primary": "writes_confirmed", "better": "higher"}}
    res = ms.evaluate(defn, ctx(conn, t), [])
    assert res["verdict"] is None and res["arms"] == {} and res["test"]["writes_confirmed"]["writes"] == 0


def test_evaluate_unknown_measure_raises(conn):
    with pytest.raises(ValueError):
        ms.evaluate({"measures": ["nope"]}, ctx(conn), [])
