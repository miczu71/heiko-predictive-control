"""Klient historii taryfy z pv_roi: `GET /api/tariff/schedule?start=YYYY-MM-DDTHH&hours=168`.

Służy do zasilenia `tariff_hours` godzinami sprzed feedu (okno sezonu grzewczego dla bazy KPI).
Wiersze z historii mają `fetched_at = ''` — nie psują świeżości feedu (`TariffFeed._latest_fetch`)
i nigdy nie nadpisują wierszy zapisanych przez feed."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests

from .tariff_feed import _parse_item

logger = logging.getLogger(__name__)

PAGE_HOURS = 168
TIMEOUT_S = 15


def fetch_page(base_url: str, start: str, hours: int = PAGE_HOURS, get=requests.get) -> list[tuple] | None:
    """Jedna strona harmonogramu. `None` = błąd (sieć, 5xx, zły JSON); `[]` = HTTP 400 (sprzed katalogu)."""
    url = f"{base_url.rstrip('/')}/api/tariff/schedule"
    try:
        resp = get(url, params={"start": start, "hours": hours}, timeout=TIMEOUT_S)
        if resp.status_code == 400:
            return []
        if resp.status_code != 200:
            logger.warning("pv_roi %s (start=%s): HTTP %s", url, start, resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:                                    # sieć, timeout, zły JSON
        logger.warning("pv_roi %s (start=%s): %s", url, start, exc)
        return None
    items = data.get("schedule") if isinstance(data, dict) else None
    if not isinstance(items, list):
        logger.warning("pv_roi %s (start=%s): odpowiedź bez listy `schedule`", url, start)
        return None
    return [p for p in map(_parse_item, items) if p]


def backfill_window(conn, base_url: str, start_local: datetime, end_local: datetime, get=requests.get) -> bool:
    """Pobierz godziny [start_local, end_local) stronami po 168 h do `tariff_hours`. False przy pierwszym błędzie."""
    cursor = start_local.replace(minute=0, second=0, microsecond=0)
    while cursor < end_local:
        hours = min(PAGE_HOURS, int((end_local - cursor).total_seconds() // 3600) or 1)
        rows = fetch_page(base_url, cursor.strftime("%Y-%m-%dT%H"), hours, get)
        if rows is None:
            return False
        if rows:
            conn.executemany(
                "INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '') ON CONFLICT(hour) DO UPDATE SET tariff = excluded.tariff, "
                "zone = excluded.zone, is_peak = excluded.is_peak, level = excluded.level, "
                "gross = excluded.gross, confidence = excluded.confidence, fetched_at = '' "
                "WHERE tariff_hours.fetched_at = ''", rows)
            conn.commit()
        cursor += timedelta(hours=PAGE_HOURS)
    return True
