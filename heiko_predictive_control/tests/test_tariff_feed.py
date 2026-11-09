"""Taryfa z encji pv_roi: feed (bez zgadywania z zegara), status feed/stale/none, migracja tabeli, okna testu Bezwładność."""
import inspect
import logging
import re
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import floor_plan as fp
from heiko_predictive_control import tariff_feed as tf

NOW = datetime(2026, 1, 14, 10, 7)          # środa
ENTITY = "sensor.pv_roi_tracker_pv_tariff_zone"


class FakeHA:
    def __init__(self, data=None):
        self.data = data

    def get_state(self, entity):
        return self.data


def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


def schedule(start, hours=48, peak=lambda dt: 6 <= dt.hour < 13 or 15 <= dt.hour < 22, tariff="G12w"):
    out = []
    for i in range(hours):
        dt = start + timedelta(hours=i)
        p = peak(dt)
        out.append({"hour": dt.strftime("%Y-%m-%dT%H"), "tariff": tariff, "zone": "peak" if p else "offpeak",
                    "is_peak": p, "level": "expensive" if p else "cheap", "gross": 1.5 if p else 0.5,
                    "net": 1.0, "confidence": "invoice"})
    return out


def state(items, st="peak"):
    return {"state": st, "attributes": {"schedule": items}}


def feed(c, data):
    return tf.TariffFeed(FakeHA(data), c, ENTITY)


def test_fresh_g12w_feed():
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 10))))
    assert f.refresh(NOW)
    assert f.is_peak(NOW) and f.price(NOW) == 1.5
    assert not f.is_peak(datetime(2026, 1, 14, 13)) and f.price(datetime(2026, 1, 14, 13)) == 0.5
    st = f.status(NOW)
    assert st["source"] == "feed" and st["tariff"] == "G12w" and st["confidence"] == "invoice"


def test_g13_feed_uses_its_zones():
    c = conn()
    items = schedule(datetime(2026, 1, 14, 0), peak=lambda dt: 19 <= dt.hour < 22, tariff="G13")
    f = feed(c, state(items))
    f.refresh(NOW)
    assert not f.is_peak(datetime(2026, 1, 14, 10)) and f.is_peak(datetime(2026, 1, 14, 20))
    assert f.status(NOW)["tariff"] == "G13"


def test_missing_entity_and_unavailable_and_bad_schedule_mean_no_data():
    for data in (None, state(schedule(NOW), st="unavailable"), state([]), state("zle"),
                 state([{"hour": "x", "gross": 1, "is_peak": True}])):
        c = conn()
        f = feed(c, data)
        assert f.refresh(NOW) is False
        st = f.status(NOW)
        assert st["source"] == "none" and st["age_min"] is None and st["stale_since"] is None
        assert st["tariff"] is None and st["confidence"] is None
        assert f.at(NOW) is None
        # nic z zegara: środa 10:00 i sobota to tak samo „brak danych taryfy”
        for dt in (NOW, datetime(2026, 1, 17, 10)):
            assert f.is_peak(dt) is None and f.price(dt) is None
        assert f.offpeak_price(NOW) is None


def test_stale_feed_keeps_last_known_schedule_and_reports_since_when():
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 10))))
    f.refresh(NOW)
    fresh = f.status(NOW + timedelta(hours=2))
    assert fresh["source"] == "feed" and fresh["stale_since"] is None
    later = NOW + timedelta(hours=4)
    st = f.status(later)
    assert st["source"] == "stale" and st["tariff"] == "G12w" and st["confidence"] == "invoice"
    assert st["stale_since"] == NOW.isoformat(timespec="seconds") and st["age_min"] == 240.0
    # decyzje czytają at() bez względu na source: ostatni znany harmonogram obowiązuje
    assert f.is_peak(NOW + timedelta(hours=1)) is True and f.price(NOW + timedelta(hours=1)) == 1.5
    assert f.is_peak(datetime(2026, 1, 14, 13)) is False


def test_hour_without_row_is_none_and_warned_once_per_hour(caplog):
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 10), hours=2)))
    f.refresh(NOW)
    tf._warned.clear()
    missing = datetime(2026, 1, 20, 8)
    with caplog.at_level(logging.WARNING):
        assert f.is_peak(missing) is None and f.price(missing) is None and f.at(missing) is None
    assert sum("brak danych taryfy dla godziny 2026-01-20T08" in r.message for r in caplog.records) == 1


def test_offpeak_price_is_cheapest_in_window_or_none():
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 10))))
    f.refresh(NOW)
    assert f.offpeak_price(NOW) == 0.5
    assert f.offpeak_price(datetime(2026, 3, 1, 10)) is None            # brak jakiegokolwiek wiersza w oknie 36 h


def test_no_tariff_zone_constant():
    assert tf.NO_TARIFF_ZONE == "no_tariff"


def test_offpeak_hours_per_day_uses_last_full_day_with_a_peak():
    c = conn()
    assert tf.offpeak_hours_per_day(c) is None                           # pusta tabela
    f = feed(c, state(schedule(datetime(2026, 1, 14, 0), hours=48)))
    f.refresh(NOW)
    assert tf.offpeak_hours_per_day(c) == 24 - 7 - 7                     # G12w: szczyt 6–13 i 15–22 = 14 h
    c2 = conn()
    feed(c2, state(schedule(datetime(2026, 1, 14, 0), hours=24, peak=lambda dt: False))).refresh(NOW)
    assert tf.offpeak_hours_per_day(c2) is None                          # pełna doba bez szczytu nie liczy się
    c3 = conn()
    feed(c3, state(schedule(datetime(2026, 1, 14, 10), hours=30))).refresh(NOW)
    assert tf.offpeak_hours_per_day(c3) is None                          # żadna doba nie ma 24 wierszy


def test_for_settings_and_ctor_have_no_workday_parameter():
    assert list(inspect.signature(tf.TariffFeed.__init__).parameters) == ["self", "ha_client", "db", "entity"]
    assert list(inspect.signature(tf.for_settings).parameters) == ["db", "settings", "ha_client"]
    src = inspect.getsource(tf)
    assert "from . import tariff\n" not in src and "fallback_hour" not in src and "_fallback" not in src


def test_past_hour_comes_from_db_after_feed_moves_on():
    c = conn()
    feed(c, state(schedule(datetime(2026, 1, 14, 8), peak=lambda dt: True))).refresh(NOW)
    later = NOW + timedelta(hours=3)
    f = feed(c, state(schedule(datetime(2026, 1, 14, 13), peak=lambda dt: False)))
    assert f.refresh(later)
    assert f.at(datetime(2026, 1, 14, 9)).is_peak is True          # przeszła godzina zostaje w tariff_hours
    assert f.at(datetime(2026, 1, 14, 14)).is_peak is False


def test_holiday_in_feed_is_not_peak_even_on_weekday():
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 0), peak=lambda dt: dt.day != 14 and 6 <= dt.hour < 22)))
    f.refresh(NOW)
    assert not f.is_peak(datetime(2026, 1, 14, 10))                 # święto z harmonogramu, choć środa
    assert f.is_peak(datetime(2026, 1, 15, 10))


def test_refresh_is_throttled_to_five_minutes():
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 10))))
    assert f.refresh(NOW)
    assert not f.refresh(NOW + timedelta(minutes=3))
    assert f.refresh(NOW + timedelta(minutes=6))


def test_tariff_hours_table_is_added_to_existing_database():
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.executescript("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    dbm.migrate(raw)
    cols = {r["name"] for r in raw.execute("PRAGMA table_info(tariff_hours)")}
    assert {"hour", "tariff", "zone", "is_peak", "level", "gross", "confidence", "fetched_at"} <= cols
    dbm.migrate(raw)                                                # idempotentnie


def test_build_blocks_split_by_feed_price():
    t0 = datetime(2026, 1, 14, 6)
    blocks = fp.build_blocks(t0, 24, 6, 22, lambda t: (False, 0.4 if t.hour < 10 else 0.9))
    assert [b.price_pln_kwh for b in blocks][0] == 0.4 and 0.9 in [b.price_pln_kwh for b in blocks]


def test_inertia_windows_follow_feed_and_default_without_it():
    c = conn()
    f = feed(c, state(schedule(datetime(2026, 1, 14, 0), peak=lambda dt: 19 <= dt.hour < 22 or 7 <= dt.hour < 9)))
    f.refresh(NOW)
    steps = ex.build_schedule("bezwladnosc", 0.0, datetime(2026, 1, 14, 5), lambda d: True, f)
    assert [s["at"][11:16] for s in steps[:4]] == ["07:00", "08:00", "19:00", "21:00"]
    default = ex.build_schedule("bezwladnosc", 0.0, datetime(2026, 1, 16, 5), lambda d: d.weekday() < 5, f)
    assert [s["at"][11:16] for s in default[:4]] == ["06:00", "12:00", "15:00", "21:00"]   # feed nie zna doby


def test_g12w_constants_only_in_workday_py():
    root = Path(__file__).resolve().parents[1] / "heiko_predictive_control"
    pattern = re.compile(r"1\.2304|0\.6306|PEAK_WINDOWS|DEFAULT_PEAK_PRICE|DEFAULT_OFFPEAK_PRICE|weekday\(\) *< *5")
    hits = [f"{p.name}:{i}" for p in root.glob("*.py") if p.name != "workday.py"
            for i, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert hits == []


def test_refresh_rejects_schedule_with_stale_generated_at():
    c = conn()
    old = (NOW - timedelta(hours=4)).isoformat(timespec="seconds")
    data = {"state": "peak", "attributes": {"schedule": schedule(NOW.replace(minute=0)), "generated_at": old}}
    assert tf.TariffFeed(FakeHA(data), c, ENTITY).refresh(NOW, force=True) is False
    assert c.execute("SELECT COUNT(*) FROM tariff_hours").fetchone()[0] == 0
    data["attributes"]["generated_at"] = (NOW - timedelta(minutes=30)).isoformat(timespec="seconds")
    assert tf.TariffFeed(FakeHA(data), c, ENTITY).refresh(NOW, force=True) is True
