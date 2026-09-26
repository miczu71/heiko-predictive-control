"""Telemetria pompy i dziennik zmian parametrów (D1) — WYŁĄCZNIE odczyt z HA.

Recorder HA trzyma historię 7 dni, a statystyki długoterminowe są godzinowe i tylko dla
części czujników. Co cykl (15 min) zapisujemy więc próbkę: sensory pompy z katalogu,
temperatury pokoi, temperaturę zewnętrzną i licznik energii pompy — wąska tabela `telemetry`
(surowe dane 2 lata, starsze zwijane do godzinowych agregatów). Osobno wykrywamy KAŻDĄ zmianę
parametru z katalogu (z panelu, HA albo automatyzacji) → `param_changes` (licznik zapisów do pamięci pompy)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import catalog
from . import db as dbm
from . import ha_client

logger = logging.getLogger(__name__)

KEEP_RAW_DAYS = 730
DB_WARN_BYTES = 200 * 1024 * 1024
_LAST_SEEN_KEY = "catalog_last_seen"
_BAD_STATES = ("unavailable", "unknown", "none", "")


def _entities(raw) -> list[str]:
    return [e.strip() for e in str(raw or "").split(",") if e.strip()]


def extra_entities(settings: dict) -> list[str]:
    """Encje spoza integracji logowane razem z pompą: pokoje stref dziennych, temp. zewn., licznik energii."""
    out: list[str] = []
    for eid in (_entities(settings.get("day_zone_temp_entities"))
                + _entities(settings.get("outdoor_temp_entity"))
                + _entities(settings.get("pump_energy_entity"))):
        if eid not in out:
            out.append(eid)
    return out


def collect_values(settings: dict, states: list[dict]) -> dict[str, float]:
    """{nazwa klucza: wartość} — sensory pompy pod nazwą sufiksu, reszta pod entity_id."""
    by_id = {s.get("entity_id"): s for s in states}
    values: dict[str, float] = {}
    for suffix, eid in catalog.resolve_telemetry(states).items():
        v = catalog.to_number(by_id[eid].get("state"))
        if v is not None:
            values[suffix] = v
    for eid in extra_entities(settings):
        st = by_id.get(eid)
        v = catalog.to_number(st.get("state")) if st else None
        if v is not None:
            values[eid] = v
    return values


def _key_ids(conn, names) -> dict[str, int]:
    ids: dict[str, int] = {}
    for name in names:
        conn.execute("INSERT OR IGNORE INTO telemetry_keys (name) VALUES (?)", (name,))
        ids[name] = conn.execute("SELECT id FROM telemetry_keys WHERE name = ?", (name,)).fetchone()["id"]
    return ids


def store_sample(conn, when: datetime, values: dict[str, float]) -> int:
    """Zapisuje jedną próbkę (ts = pełna minuta, sekundy UTC). Zwraca liczbę zapisanych wartości."""
    if not values:
        return 0
    ts = int(when.timestamp()) // 60 * 60
    ids = _key_ids(conn, values)
    conn.executemany("INSERT OR REPLACE INTO telemetry (ts, key_id, value) VALUES (?, ?, ?)",
                     [(ts, ids[name], v) for name, v in values.items()])
    conn.commit()
    return len(values)


def change_source(context: dict | None) -> str:
    """Best-effort: skąd zmiana. Pusty kontekst NIE oznacza pompy (panel i integracja też go nie mają)."""
    ctx = context or {}
    if ctx.get("user_id"):
        return "użytkownik HA"
    if ctx.get("parent_id"):
        return "automatyzacja/skrypt"
    return "nieznane"


def detect_changes(conn, states: list[dict]) -> list[dict]:
    """Porównuje `last_changed` parametrów katalogu z ostatnio widzianym i loguje zmiany.
    Pierwsze uruchomienie tylko zapamiętuje stan (bez wpisów). Przejścia przez unavailable/unknown pomijane."""
    by_id = {s.get("entity_id"): s for s in states}
    seen = dbm.get_setting(conn, _LAST_SEEN_KEY) or {}
    current: dict[str, dict] = {}
    logged: list[dict] = []
    for key, eid in catalog.resolve_params(states).items():
        st = by_id[eid]
        now_state, changed = str(st.get("state")), st.get("last_changed")
        current[key] = {"state": now_state, "lc": changed}
        prev = seen.get(key)
        if prev is None or prev.get("lc") == changed:
            continue
        if now_state.lower() in _BAD_STATES or str(prev.get("state")).lower() in _BAD_STATES:
            continue
        row = {"ts": changed, "key": key, "entity_id": eid, "old": prev.get("state"), "new": now_state,
               "source": change_source(st.get("context"))}
        conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) "
                     "VALUES (:ts, :key, :entity_id, :old, :new, :source)", row)
        logged.append(row)
    if logged:
        conn.commit()
        logger.info("Zmiany parametrów pompy: %s", [(r["key"], r["old"], r["new"], r["source"]) for r in logged])
    if current != seen:
        dbm.set_setting(conn, _LAST_SEEN_KEY, {**seen, **current})
    return logged


def apply_retention(conn, now: datetime, keep_days: int = KEEP_RAW_DAYS) -> int:
    """Surowe próbki starsze niż `keep_days` → agregaty godzinowe (granica na pełnej godzinie)."""
    cutoff = int((now - timedelta(days=keep_days)).timestamp()) // 3600 * 3600
    conn.execute(
        "INSERT OR REPLACE INTO telemetry_hourly (hour_ts, key_id, mean, min, max, n) "
        "SELECT ts / 3600 * 3600, key_id, AVG(value), MIN(value), MAX(value), COUNT(value) "
        "FROM telemetry WHERE ts < ? AND value IS NOT NULL GROUP BY ts / 3600, key_id", (cutoff,))
    removed = conn.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return removed


def db_size_bytes(conn) -> int:
    return conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]


def changes_summary(conn, now: datetime) -> dict:
    """Licznik zmian parametrów (dziś / ostatnie 30 dni / ogółem) — kolumna ts to ISO z HA (UTC lub lokalny)."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = conn.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0]
    return {"today": _count_since(conn, day_start), "days30": _count_since(conn, now - timedelta(days=30)),
            "total": total}


def _count_since(conn, since: datetime) -> int:
    """Porównanie po czasie (nie po napisie): strefy w `ts` mogą się różnić."""
    n = 0
    for row in conn.execute("SELECT ts FROM param_changes"):
        try:
            t = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        cmp = since if t.tzinfo else since.replace(tzinfo=None)
        if t.tzinfo and cmp.tzinfo is None:
            cmp = cmp.astimezone()
        if t >= cmp:
            n += 1
    return n


def run(conn, settings: dict, now: datetime, get_states=ha_client.get_all_states) -> dict:
    """Jeden krok telemetrii (z cyklu pętli A): próbka + zmiany parametrów + retencja raz na dobę."""
    states = get_states()
    if not states:
        return {"samples": 0, "changes": 0, "ok": False}
    stored = store_sample(conn, now, collect_values(settings, states))
    changes = detect_changes(conn, states)
    if now.hour == 3 and now.minute < 15:                         # raz na dobę, poza szczytem
        apply_retention(conn, now)
    return {"samples": stored, "changes": len(changes), "ok": True}
