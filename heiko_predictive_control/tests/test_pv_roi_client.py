import logging
from datetime import datetime, timedelta

import pytest
import requests

from heiko_predictive_control import db as dbm
from heiko_predictive_control import pv_roi_client as client
from heiko_predictive_control import tariff_feed


class Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


def _item(hour, zone="evening_peak", gross=1.2, is_peak=True):
    return {"hour": hour, "tariff": "G13", "zone": zone, "is_peak": is_peak, "level": "mid",
            "gross": gross, "confidence": "document"}


def _hours(start, n):
    return [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H") for i in range(n)]


def _server(calls, fail_on=None, status=200):
    """Fałszywe `requests.get`: zwraca wiersze dla `hours` godzin od `start`."""
    def get(url, params=None, timeout=None):
        calls.append((url, dict(params), timeout))
        if fail_on is not None and len(calls) == fail_on:
            return Resp(500)
        if status != 200:
            return Resp(status)
        start = datetime.strptime(params["start"], "%Y-%m-%dT%H")
        return Resp(200, {"ok": True, "schedule": [_item(h) for h in _hours(start, params["hours"])]})
    return get


def _conn():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    return conn


def test_fetch_page_parses_rows_with_feed_parser():
    calls = []
    rows = client.fetch_page("http://pv:8099", "2026-01-05T00", get=_server(calls))
    assert len(rows) == 168 and rows[0][:4] == ("2026-01-05T00", "G13", "evening_peak", 1)
    url, params, timeout = calls[0]
    assert url == "http://pv:8099/api/tariff/schedule" and params == {"start": "2026-01-05T00", "hours": 168}
    assert timeout == 15


def test_fetch_page_400_means_no_data_not_error():
    assert client.fetch_page("http://pv", "2020-01-01T00", get=_server([], status=400)) == []


@pytest.mark.parametrize("resp", [Resp(500), Resp(200, bad_json=True), Resp(200, ["nie dict"])])
def test_fetch_page_server_errors_return_none(resp, caplog):
    with caplog.at_level(logging.WARNING):
        assert client.fetch_page("http://pv", "2026-01-05T00", get=lambda *a, **k: resp) is None
    assert any("pv_roi" in r.getMessage() for r in caplog.records)


def test_fetch_page_network_error_returns_none():
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    assert client.fetch_page("http://pv", "2026-01-05T00", get=boom) is None


def test_backfill_pages_by_168h_and_marks_history_rows():
    conn = _conn()
    calls = []
    start, end = datetime(2026, 1, 5, 0, 30), datetime(2026, 1, 5, 0, 0) + timedelta(hours=400)
    assert client.backfill_window(conn, "http://pv", start, end, get=_server(calls)) is True
    assert [c[1]["start"] for c in calls] == ["2026-01-05T00", "2026-01-12T00", "2026-01-19T00"]
    assert [c[1]["hours"] for c in calls] == [168, 168, 64]
    assert conn.execute("SELECT COUNT(*) FROM tariff_hours").fetchone()[0] == 400
    assert conn.execute("SELECT COUNT(*) FROM tariff_hours WHERE fetched_at != ''").fetchone()[0] == 0


def test_backfill_second_page_failure_returns_false_keeps_first_page():
    conn = _conn()
    start = datetime(2026, 1, 5)
    assert client.backfill_window(conn, "http://pv", start, start + timedelta(hours=400),
                                  get=_server([], fail_on=2)) is False
    assert conn.execute("SELECT COUNT(*) FROM tariff_hours").fetchone()[0] == 168


def test_backfill_400_pages_are_skipped_without_error():
    conn = _conn()
    start = datetime(2026, 1, 5)
    assert client.backfill_window(conn, "http://pv", start, start + timedelta(hours=10), get=_server([], status=400)) is True
    assert conn.execute("SELECT COUNT(*) FROM tariff_hours").fetchone()[0] == 0


def test_backfill_does_not_overwrite_feed_rows_but_updates_history_rows():
    conn = _conn()
    conn.execute("INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                 "VALUES ('2026-01-05T00', 'G12w', 'peak', 1, 'x', 9.0, 'feed', '2026-01-05T00:01:00')")
    conn.execute("INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                 "VALUES ('2026-01-05T01', 'G12w', 'old', 0, 'x', 9.0, 'hist', '')")
    conn.commit()
    start = datetime(2026, 1, 5)
    assert client.backfill_window(conn, "http://pv", start, start + timedelta(hours=3), get=_server([])) is True
    feed = conn.execute("SELECT * FROM tariff_hours WHERE hour = '2026-01-05T00'").fetchone()
    assert feed["zone"] == "peak" and feed["fetched_at"] == "2026-01-05T00:01:00"
    hist = conn.execute("SELECT * FROM tariff_hours WHERE hour = '2026-01-05T01'").fetchone()
    assert hist["zone"] == "evening_peak" and hist["fetched_at"] == ""


def test_latest_fetch_ignores_history_rows():
    conn = _conn()
    start = datetime(2026, 1, 5)
    client.backfill_window(conn, "http://pv", start, start + timedelta(hours=5), get=_server([]))
    feed = tariff_feed.TariffFeed(None, conn, None)
    assert feed._latest_fetch() is None
    assert feed.status(datetime(2026, 1, 5, 3))["source"] == "fallback"
