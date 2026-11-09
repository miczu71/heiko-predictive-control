from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from heiko_predictive_control import battery_replay as br
from heiko_predictive_control import db as dbm
from heiko_predictive_control import energy_source as es

NOW = datetime(2026, 9, 21, 12, 0)          # czas lokalny bez strefy, jak datetime.now() w main.py
START = datetime(2026, 9, 20, 12, 0)        # NOW - 1 dzień
CAP_WH = 10000.0
SOC0_KWH = 2.0


class StubFeed:
    def __init__(self, gap=None):
        self.gap = gap                       # (od, do) czasu lokalnego bez wierszy taryfy

    def _missing(self, dt):
        return self.gap is not None and self.gap[0] <= dt < self.gap[1]

    def price(self, dt):
        return None if self._missing(dt) else (0.5 if dt.hour < 6 else 1.0)

    def is_peak(self, dt):
        return None if self._missing(dt) else dt.hour >= 6

    def at(self, dt):
        return None if self._missing(dt) else SimpleNamespace(zone="peak" if self.is_peak(dt) else "offpeak")

    def offpeak_price(self, dt):
        return 0.4


def utc(t):
    return t.astimezone(timezone.utc)


def build(rates, rce=0.3, now=NOW, start=START, reset_at=None):
    """Historia (serie HA) z funkcji rates(t) -> (import, charge, discharge) kWh w kroku kończącym się w t."""
    ent = es.entities({})
    imp = chg = dis = 100.0
    soc = SOC0_KWH
    s = {k: [] for k in ent}
    s["battery_soc"].append((utc(start - timedelta(hours=1)), str(soc / CAP_WH * 100 * 1000)))
    s["battery_capacity"].append((utc(start - timedelta(hours=1)), str(CAP_WH)))
    s["rce_price"].append((utc(start - timedelta(hours=1)), str(rce)))
    for k in ("grid_import", "battery_charge", "battery_discharge"):
        s[k].append((utc(start - timedelta(hours=1)), "100.0"))
    t = start
    while t < now:
        t += timedelta(minutes=15)
        i, c, d = rates(t)
        imp, chg, dis = imp + i, chg + c, dis + d
        soc += c - d
        if reset_at is not None and t == reset_at:
            chg = 0.0
        s["grid_import"].append((utc(t), str(imp)))
        s["battery_charge"].append((utc(t), str(chg)))
        s["battery_discharge"].append((utc(t), str(dis)))
        s["battery_soc"].append((utc(t), str(soc / CAP_WH * 100 * 1000)))
    return {ent[k]: v for k, v in s.items()}


def quiet(t):
    return 0.0, 0.0, 0.0


@pytest.fixture
def conn(tmp_path):
    c = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(c)
    yield c
    c.close()


def run(conn, hist, now=NOW, feed=None):
    return br.replay(conn, {}, now, lambda ids, a, b: hist, feed or StubFeed(), back_days=1)


def rows(conn):
    return dbm.load_battery_mix(conn, "0000", "9999")


def total(mix):
    return sum(v["kwh"] for v in mix.values())


def night_rates(t):
    if datetime(2026, 9, 21, 1, 0) < t <= datetime(2026, 9, 21, 3, 0):
        return 0.5, 0.5, 0.0
    return quiet(t)


def test_night_grid_charge(conn):
    assert run(conn, build(night_rates)) > 0
    last = rows(conn)[-1][1]
    # 8 kroków po 0,5 kWh = 4 kWh naładowane z sieci w strefie offpeak
    assert last["grid:offpeak"]["kwh"] == pytest.approx(4.0)
    assert last["grid:offpeak"]["pln"] == pytest.approx(4.0 * 0.5)
    assert total(last) == pytest.approx(SOC0_KWH + 4.0, abs=1e-6)


def test_first_mix_is_unknown(conn):
    run(conn, build(night_rates))
    first = rows(conn)[0]
    assert first[0] == (START + timedelta(minutes=15)).isoformat()
    assert set(first[1]) == {"unknown"}
    assert total(first[1]) == pytest.approx(SOC0_KWH, abs=1e-6)


def day_discharge(t):
    if t.date() == datetime(2026, 9, 21).date() and t.hour >= 7:
        return 0.0, 0.0, 0.05
    return night_rates(t)


def test_day_discharge_keeps_proportions(conn):
    run(conn, build(day_discharge))
    by_ts = dict(rows(conn))
    before = by_ts[datetime(2026, 9, 21, 3, 15).isoformat()]
    after = rows(conn)[-1][1]
    assert total(after) < total(before)
    assert total(after) == pytest.approx(SOC0_KWH + 4.0 - 0.05 * 20, abs=1e-6)   # 07:15..12:00 = 20 kroków
    for origin in ("grid:offpeak", "unknown"):
        assert after[origin]["kwh"] / total(after) == pytest.approx(before[origin]["kwh"] / total(before))


def test_pv_charge_priced_by_rce(conn):
    def pv(t):                                  # import stały, bateria ładuje się z nadwyżki PV
        return (0.0, 0.4, 0.0) if t.date() == NOW.date() and 8 < t.hour < 10 else quiet(t)
    run(conn, build(pv, rce=0.25))
    last = rows(conn)[-1][1]
    assert last["pv"]["kwh"] == pytest.approx(0.4 * 4)
    assert last["pv"]["pln"] == pytest.approx(0.4 * 4 * 0.25)
    assert "grid:peak" not in last


def test_negative_rce_gives_zero_cost(conn):
    def pv(t):
        return (0.0, 0.4, 0.0) if t.date() == NOW.date() and 8 < t.hour < 10 else quiet(t)
    run(conn, build(pv, rce=-0.1))
    last = rows(conn)[-1][1]
    assert last["pv"]["kwh"] > 0 and last["pv"]["pln"] == 0


def test_idempotent(conn):
    hist = build(night_rates)
    n = run(conn, hist)
    assert n > 0
    assert dbm.get_setting(conn, br.DONE_KEY)
    assert run(conn, hist) == 0
    assert len(rows(conn)) == n


def test_history_error_then_ok(conn):
    assert run(conn, None) == 0
    assert dbm.get_setting(conn, br.DONE_KEY) is None
    assert run(conn, build(night_rates)) > 0
    assert dbm.get_setting(conn, br.DONE_KEY)


def test_missing_series(conn):
    hist = build(night_rates)
    del hist[es.entities({})["battery_soc"]]
    assert run(conn, hist) == 0
    assert dbm.get_setting(conn, br.DONE_KEY) is None
    assert rows(conn) == []


def test_live_rows_replaced_and_setting_seeded(conn):
    live_ts = datetime(2026, 9, 21, 6, 0)
    live = {"unknown": {"kwh": 9.0, "pln": 3.0}}
    dbm.insert_battery_mix(conn, live_ts.isoformat(), live)
    dbm.set_setting(conn, "battery_mix", live)
    run(conn, build(night_rates))
    got = rows(conn)
    assert live_ts.isoformat() not in dict(got) or dict(got)[live_ts.isoformat()] != live
    assert dbm.get_setting(conn, "battery_mix") == got[-1][1]
    assert dbm.get_setting(conn, "battery_mix") != live


def test_no_live_rows_sets_setting(conn):
    run(conn, build(night_rates))
    assert dbm.get_setting(conn, "battery_mix") == rows(conn)[-1][1]


def test_counter_reset_step_skipped(conn):
    reset = datetime(2026, 9, 21, 2, 0)
    hist = build(night_rates, reset_at=reset)
    full = run(conn, build(night_rates))
    n_ok = len(rows(conn))
    conn.execute("DELETE FROM battery_mix")
    conn.execute("DELETE FROM settings")
    conn.commit()
    n = run(conn, hist)
    got = [ts for ts, _ in rows(conn)]
    assert reset.isoformat() not in got
    assert 0 < n < n_ok == full


def test_step_without_tariff_row_skipped(conn):
    gap = (datetime(2026, 9, 21, 1, 0), datetime(2026, 9, 21, 2, 0))
    full = run(conn, build(night_rates))
    conn.execute("DELETE FROM battery_mix")
    conn.execute("DELETE FROM settings")
    conn.commit()
    n = run(conn, build(night_rates), feed=StubFeed(gap=gap))
    got = [ts for ts, _ in rows(conn)]
    assert n == full - 4                                  # 4 kroki po 15 min w godzinie bez ceny
    assert not any(gap[0] <= datetime.fromisoformat(ts) < gap[1] for ts in got)
