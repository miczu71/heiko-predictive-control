"""Taryfa z encji pv_roi: cena, strefa i „szczyt” każdej godziny pochodzą z atrybutu `schedule`
(sensor.pv_roi_tracker_pv_tariff_zone, 48 godzin od bieżącej) — zamiast G12w wpisanego na sztywno.

Wiersze trafiają do tabeli `tariff_hours` (też godziny przeszłe: historia cykli i streszczeń dób wycenia się
tym, co faktycznie obowiązywało). Brak encji, `unavailable`, pusty albo niepoprawny `schedule` nie kasuje zapisanych wierszy: ostatni znany
harmonogram dalej obowiązuje (UI pokazuje „nieaktualne od …” — `status()`), a godzina bez wiersza to jawne
„brak danych taryfy” (`None`) — nic nie jest zgadywane z zegara. Instancja jest tania i bezstanowa (stan w bazie), więc można ją tworzyć przy każdym użyciu."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_ENTITY = "sensor.pv_roi_tracker_pv_tariff_zone"
REFRESH_MIN = 5                     # nie odpytuj encji częściej
STALE_MIN = 180                     # dane starsze niż 3 h = „nieaktualne” (tylko informacja w UI)
NO_TARIFF_ZONE = "no_tariff"         # klucz strefy dla godziny bez wiersza w rozbiciach kosztu
OFFPEAK_LOOKAHEAD_H = 36
_HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")
_BAD_STATES = ("", "unavailable", "unknown", "none")
_warned: dict[str, str] = {}        # rodzaj -> godzina ostatniego ostrzeżenia (WARNING raz na godzinę)

# Strefy, które zna pv_roi (`tariff_catalog.zone_for`): G11 `all`, G12 `day`/`night`, G12w `peak`/`offpeak`,
# G13 `morning_peak`/`evening_peak`/`other`, G13s `{lato|zima}_{workday|dayoff}_{peak|offpeak|night}`.
_G13S_ZONES = frozenset(f"{s}_{d}_{b}" for s in ("summer", "winter") for d in ("workday", "dayoff")
                        for b in ("peak", "offpeak", "night"))
KNOWN_ZONES = frozenset({"all", "day", "night", "peak", "offpeak", "morning_peak", "evening_peak", "other"}) | _G13S_ZONES
KNOWN_PEAK_ZONES = frozenset({"day", "peak", "morning_peak", "evening_peak"}) | \
    frozenset(z for z in _G13S_ZONES if z.endswith("_peak"))


def warn_unknown_zone(zone: str | None, is_peak, dt: datetime) -> None:
    """WARNING (raz na godzinę na rodzaj) dla strefy spoza `KNOWN_ZONES` oraz dla `is_peak` = 1 w strefie,
    która nie jest szczytem popołudniowym ani znaną strefą szczytu. Nie zmienia liczenia."""
    key = hour_key(dt)
    if zone is not None and zone not in KNOWN_ZONES and _warned.get("zone") != key:
        _warned["zone"] = key
        logger.warning("Taryfa: nieznana strefa %r z pv_roi — sprawdź katalog stref", zone)
    elif is_peak and zone in KNOWN_ZONES and zone not in KNOWN_PEAK_ZONES and _warned.get("zone_peak") != key:
        _warned["zone_peak"] = key
        logger.warning("Taryfa: niespójność — is_peak=1 w strefie %r, która nie jest szczytem", zone)


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


def _source_stale(generated_at, now: datetime) -> bool:
    """True, gdy pv_roi podał `generated_at` starszy niż STALE_MIN (retained payload po awarii publikacji)."""
    try:
        return (now - datetime.fromisoformat(str(generated_at))).total_seconds() > STALE_MIN * 60
    except (TypeError, ValueError):
        return False


class TariffFeed:
    """`ha_client` = moduł z `get_state(entity)`, `db` = połączenie sqlite, `entity` = encja z `schedule`."""

    def __init__(self, ha_client, db, entity: str | None):
        self.ha, self.db = ha_client, db
        self.entity = entity or DEFAULT_ENTITY

    # ── zapis ───────────────────────────────────────────────────────────────

    def refresh(self, now: datetime | None = None, force: bool = False, get_state=None) -> bool:
        """Pobierz `schedule` i zapisz w bazie. True = zapisano świeże wiersze. Nie częściej niż co 5 min."""
        now = now or datetime.now()
        latest = self._latest_fetch()
        if not force and latest is not None and 0 <= (now - latest).total_seconds() < REFRESH_MIN * 60:
            return False
        data = (get_state or self.ha.get_state)(self.entity)
        attrs = (data or {}).get("attributes") or {} if isinstance(data, dict) else {}
        items = attrs.get("schedule")
        ok = False
        if _source_stale(attrs.get("generated_at"), now):
            self._warn("refresh", now, f"harmonogram z {self.entity} wygenerowany {attrs.get('generated_at')} — pv_roi nie publikuje")
        elif isinstance(data, dict) and str(data.get("state", "")).strip().lower() not in _BAD_STATES \
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

    def _missing(self, dt: datetime) -> None:
        self._warn("hour", datetime.now(), f"brak danych taryfy dla godziny {hour_key(dt)}")

    def _row_or_missing(self, dt: datetime):
        row = self.at(dt)
        if row is None:
            self._missing(dt)
        return row

    def is_peak(self, dt: datetime) -> bool | None:
        row = self._row_or_missing(dt)
        return None if row is None else row.is_peak

    def price(self, dt: datetime) -> float | None:
        row = self._row_or_missing(dt)
        return None if row is None else row.gross

    def offpeak_price(self, dt: datetime) -> float | None:
        """Najtańsza cena w najbliższych 36 h (referencja „poza szczytem”); None, gdy w oknie nie ma żadnego wiersza."""
        start = hour_key(dt.replace(minute=0, second=0, microsecond=0))
        end = hour_key(dt + timedelta(hours=OFFPEAK_LOOKAHEAD_H))
        r = self.db.execute("SELECT MIN(gross) AS m FROM tariff_hours WHERE hour >= ? AND hour < ?",
                            (start, end)).fetchone()
        if r and r["m"] is not None:
            return float(r["m"])
        self._missing(dt)
        return None

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
        """`{source: 'feed'|'stale'|'none', age_min, stale_since, confidence, tariff}` — do `/api/live` i plakietki
        w UI. Tylko informacja: decyzje czytają `at()` bez względu na `source` (nieaktualny harmonogram obowiązuje)."""
        now = now or datetime.now()
        latest = self._latest_fetch()
        if latest is None:
            return {"source": "none", "age_min": None, "stale_since": None, "confidence": None, "tariff": None}
        age = round((now - latest).total_seconds() / 60.0, 1)
        r = self.db.execute("SELECT tariff, confidence FROM tariff_hours WHERE hour <= ? ORDER BY hour DESC LIMIT 1",
                            (hour_key(now),)).fetchone() or \
            self.db.execute("SELECT tariff, confidence FROM tariff_hours ORDER BY hour LIMIT 1").fetchone()
        stale = age > STALE_MIN
        return {"source": "stale" if stale else "feed", "age_min": age,
                "stale_since": latest.isoformat(timespec="seconds") if stale else None,
                "confidence": r["confidence"] if r else None, "tariff": r["tariff"] if r else None}

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


def offpeak_hours_per_day(db) -> int | None:
    """Liczba godzin `is_peak = 0` w ostatniej pełnej dobie (24 wiersze) mającej ≥1 godzinę szczytu; None, gdy
    takiej doby nie ma."""
    r = db.execute("SELECT substr(hour, 1, 10) AS d, COUNT(*) AS n, SUM(is_peak) AS p FROM tariff_hours "
                   "GROUP BY d HAVING n = 24 AND p >= 1 ORDER BY d DESC LIMIT 1").fetchone()
    return None if r is None else int(r["n"] - r["p"])


def for_settings(db, settings: dict, ha_client=None) -> TariffFeed:
    """Feed z ustawień add-onu (`tariff_schedule_entity`)."""
    if ha_client is None:
        from . import ha_client as ha_client
    return TariffFeed(ha_client, db, settings.get("tariff_schedule_entity") or DEFAULT_ENTITY)
