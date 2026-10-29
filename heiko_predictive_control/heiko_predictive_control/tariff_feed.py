"""Taryfa z encji pv_roi: cena, strefa i „szczyt” każdej godziny pochodzą z atrybutu `schedule`
(sensor.pv_roi_tracker_pv_tariff_zone, 48 godzin od bieżącej) — zamiast G12w wpisanego na sztywno.

Wiersze trafiają do tabeli `tariff_hours` (też godziny przeszłe: historia cykli i streszczeń dób wycenia się
tym, co faktycznie obowiązywało). Brak encji, `unavailable`, pusty albo niepoprawny `schedule` oraz dane
starsze niż 3 h = `fallback`: wtedy wołający używają zapasu G12w z `tariff.py` i widać to w UI (`status()`).
Instancja jest tania i bezstanowa (stan w bazie), więc można ją tworzyć przy każdym użyciu."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from . import tariff

logger = logging.getLogger(__name__)

DEFAULT_ENTITY = "sensor.pv_roi_tracker_pv_tariff_zone"
REFRESH_MIN = 5                     # nie odpytuj encji częściej
STALE_MIN = 180                     # dane starsze niż 3 h = zapas
OFFPEAK_LOOKAHEAD_H = 36
_HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")
_BAD_STATES = ("", "unavailable", "unknown", "none")
_warned: dict[str, str] = {}        # rodzaj -> godzina ostatniego ostrzeżenia (WARNING raz na godzinę)


@dataclass(frozen=True)
class TariffHour:
    hour: str                       # 'YYYY-MM-DDTHH' (czas lokalny)
    tariff: str | None
    zone: str | None
    is_peak: bool
    level: str | None
    gross: float                    # zł/kWh brutto
    confidence: str | None


def hour_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H")


def _row(r) -> TariffHour:
    return TariffHour(r["hour"], r["tariff"], r["zone"], bool(r["is_peak"]), r["level"], float(r["gross"]),
                      r["confidence"])


def _parse_item(item) -> tuple | None:
    if not isinstance(item, dict):
        return None
    hour = item.get("hour")
    if not isinstance(hour, str) or not _HOUR_RE.match(hour):
        return None
    try:
        gross = float(item.get("gross"))
    except (TypeError, ValueError):
        return None
    if gross != gross or gross < 0 or not isinstance(item.get("is_peak"), bool):
        return None
    return (hour, item.get("tariff"), item.get("zone"), int(item["is_peak"]), item.get("level"), gross,
            item.get("confidence"))


class TariffFeed:
    """`ha_client` = moduł z `get_state(entity)`, `db` = połączenie sqlite, `entity` = encja z `schedule`,
    `is_workday(date) -> bool` tylko dla zapasu G12w (domyślnie pn–pt)."""

    def __init__(self, ha_client, db, entity: str | None, is_workday: Callable[[date], bool] | None = None):
        self.ha, self.db = ha_client, db
        self.entity = entity or DEFAULT_ENTITY
        self.is_workday = is_workday or tariff.default_is_workday

    # ── zapis ───────────────────────────────────────────────────────────────

    def refresh(self, now: datetime | None = None, force: bool = False, get_state=None) -> bool:
        """Pobierz `schedule` i zapisz w bazie. True = zapisano świeże wiersze. Nie częściej niż co 5 min."""
        now = now or datetime.now()
        latest = self._latest_fetch()
        if not force and latest is not None and 0 <= (now - latest).total_seconds() < REFRESH_MIN * 60:
            return False
        data = (get_state or self.ha.get_state)(self.entity)
        items = (data or {}).get("attributes", {}).get("schedule") if isinstance(data, dict) else None
        ok = False
        if isinstance(data, dict) and str(data.get("state", "")).strip().lower() not in _BAD_STATES \
                and isinstance(items, list):
            rows = [p for p in map(_parse_item, items) if p]
            if rows:
                stamp = now.isoformat(timespec="seconds")
                self.db.executemany(
                    "INSERT INTO tariff_hours (hour, tariff, zone, is_peak, level, gross, confidence, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(hour) DO UPDATE SET tariff = excluded.tariff, "
                    "zone = excluded.zone, is_peak = excluded.is_peak, level = excluded.level, "
                    "gross = excluded.gross, confidence = excluded.confidence, fetched_at = excluded.fetched_at",
                    [(*r, stamp) for r in rows])
                self.db.commit()
                ok = True
        if not ok:
            self._warn("refresh", now, f"encja {self.entity} bez poprawnego harmonogramu")
        return ok

    # ── odczyt ──────────────────────────────────────────────────────────────

    def at(self, dt: datetime) -> TariffHour | None:
        r = self.db.execute("SELECT * FROM tariff_hours WHERE hour = ?", (hour_key(dt),)).fetchone()
        return _row(r) if r else None

    def _fallback(self, dt: datetime) -> tariff.HourSlot:
        if self.status()["source"] == "fallback":
            self._warn("hour", datetime.now(), "zapas G12w zamiast harmonogramu z pv_roi")
        else:
            logger.debug("Taryfa: brak godziny %s w tariff_hours — zapas G12w", hour_key(dt))
        return tariff.fallback_hour(dt, self.is_workday(dt.date()))

    def is_peak(self, dt: datetime) -> bool:
        row = self.at(dt)
        return row.is_peak if row else self._fallback(dt).is_peak

    def price(self, dt: datetime) -> float:
        row = self.at(dt)
        return row.gross if row else self._fallback(dt).price_pln_kwh

    def offpeak_price(self, dt: datetime) -> float:
        """Najtańsza cena w najbliższych 36 h (referencja „poza szczytem”); bez danych — zapas G12w."""
        start = hour_key(dt.replace(minute=0, second=0, microsecond=0))
        end = hour_key(dt + timedelta(hours=OFFPEAK_LOOKAHEAD_H))
        r = self.db.execute("SELECT MIN(gross) AS m FROM tariff_hours WHERE hour >= ? AND hour < ?",
                            (start, end)).fetchone()
        if r and r["m"] is not None:
            return float(r["m"])
        return tariff.fallback_hour(dt.replace(hour=3), False).price_pln_kwh

    def day_peak_windows(self, day: date) -> list[tuple[int, int]] | None:
        """Ciągłe odcinki godzin `is_peak` [start, stop) w dobie `day`; None, gdy feed nie ma całej doby."""
        prefix = day.isoformat()
        rows = self.db.execute("SELECT hour, is_peak FROM tariff_hours WHERE hour >= ? AND hour <= ? ORDER BY hour",
                               (prefix + "T00", prefix + "T23")).fetchall()
        flags = {int(r["hour"][-2:]): bool(r["is_peak"]) for r in rows}
        if len(flags) != 24:
            return None
        out, start = [], None
        for h in range(25):
            peak = flags.get(h, False)
            if peak and start is None:
                start = h
            elif not peak and start is not None:
                out.append((start, h))
                start = None
        return out

    def status(self, now: datetime | None = None) -> dict:
        """`{source: 'feed'|'fallback', age_min, confidence, tariff}` — do `/api/live` i plakietki w UI."""
        now = now or datetime.now()
        latest = self._latest_fetch()
        age = None if latest is None else round((now - latest).total_seconds() / 60.0, 1)
        if age is None or age > STALE_MIN:
            return {"source": "fallback", "age_min": age, "confidence": None, "tariff": "G12w"}
        r = self.db.execute("SELECT tariff, confidence FROM tariff_hours WHERE hour <= ? ORDER BY hour DESC LIMIT 1",
                            (hour_key(now),)).fetchone() or \
            self.db.execute("SELECT tariff, confidence FROM tariff_hours ORDER BY hour LIMIT 1").fetchone()
        return {"source": "feed", "age_min": age, "confidence": r["confidence"], "tariff": r["tariff"]}

    # ── pomocnicze ──────────────────────────────────────────────────────────

    def _latest_fetch(self) -> datetime | None:
        r = self.db.execute("SELECT MAX(fetched_at) AS m FROM tariff_hours").fetchone()
        try:
            return datetime.fromisoformat(r["m"]) if r and r["m"] else None
        except ValueError:
            return None

    def _warn(self, kind: str, dt: datetime, msg: str) -> None:
        key = hour_key(dt)
        if _warned.get(kind) != key:
            _warned[kind] = key
            logger.warning("Taryfa: %s", msg)


def for_settings(db, settings: dict, is_workday: Callable[[date], bool] | None = None, ha_client=None) -> TariffFeed:
    """Feed z ustawień add-onu (`tariff_schedule_entity`)."""
    if ha_client is None:
        from . import ha_client as ha_client
    return TariffFeed(ha_client, db, settings.get("tariff_schedule_entity") or DEFAULT_ENTITY, is_workday)
