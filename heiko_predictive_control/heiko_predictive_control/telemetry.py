"""Telemetria pompy i dziennik zmian parametrów (D1) — WYŁĄCZNIE odczyt z HA.

Recorder HA trzyma historię 7 dni, a statystyki długoterminowe są godzinowe i tylko dla
części czujników. Co cykl (15 min) zapisujemy więc próbkę: sensory pompy z katalogu,
temperatury pokoi, temperaturę zewnętrzną i licznik energii pompy — wąska tabela `telemetry`
(surowe dane 2 lata, starsze zwijane do godzinowych agregatów). Osobno wykrywamy KAŻDĄ zmianę
parametru z katalogu (z panelu, HA albo automatyzacji) → `param_changes` (licznik zapisów do pamięci pompy)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import catalog
from . import db as dbm
from . import ha_client

logger = logging.getLogger(__name__)

KEEP_RAW_DAYS = 730
DB_WARN_BYTES = 200 * 1024 * 1024
_LAST_SEEN_KEY = "catalog_last_seen"
_CHANGES_SCHEMA_KEY = "param_changes_schema"
_CHANGES_SCHEMA = 2
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


def source_from_logbook(entries: list[dict] | None, changed_iso: str, tolerance_s: float = 5.0) -> str | None:
    """Źródło zmiany z wpisu logbooka o czasie zgodnym ze zmianą stanu. Logbook zachowuje kontekst,
    który stan encji traci przy następnej ramce z pompy: użytkownik (UI/API) ma `context_user_id`,
    automatyzacja/skrypt — `context_entity_id` albo zdarzenie `automation_triggered`/`script_started`.
    None = brak wpisu lub brak informacji (panel pompy i integracja nie zostawiają kontekstu)."""
    try:
        target = datetime.fromisoformat(changed_iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    best = None
    for e in entries or []:
        try:
            when = datetime.fromisoformat(str(e.get("when")).replace("Z", "+00:00"))
        except ValueError:
            continue
        gap = abs((when - target).total_seconds())
        if gap <= tolerance_s and (best is None or gap < best[0]):
            best = (gap, e)
    if best is None:
        return None
    e = best[1]
    if e.get("context_user_id"):
        return "użytkownik HA"
    if e.get("context_entity_id") or e.get("context_event_type") in ("automation_triggered", "script_started"):
        return "automatyzacja/skrypt"
    return None


def _logbook_source(get_logbook, entity_id: str, changed_iso: str) -> str | None:
    try:
        moment = datetime.fromisoformat(changed_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    entries = get_logbook(entity_id, (moment - timedelta(minutes=1)).isoformat(), (moment + timedelta(minutes=1)).isoformat())
    return source_from_logbook(entries, changed_iso)


def reattribute_unknown(conn, get_logbook=ha_client.get_logbook, limit: int = 20) -> int:
    """Uzupełnia źródło dla wpisów oznaczonych „nieznane” (logbook sięga kilka dni wstecz)."""
    fixed = 0
    for row in conn.execute("SELECT id, ts, entity_id FROM param_changes WHERE source = 'nieznane' "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        found = _logbook_source(get_logbook, row["entity_id"], row["ts"])
        if found:
            conn.execute("UPDATE param_changes SET source = ? WHERE id = ?", (found, row["id"]))
            fixed += 1
    if fixed:
        conn.commit()
    return fixed


def _parse_ts(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def transitions(history: list[tuple[datetime, str]], prev_state: str | None,
                since: datetime | None = None) -> list[tuple[datetime, str, str]]:
    """Realne zmiany wartości z historii recordera: [(czas UTC, stara, nowa)].
    Wpisy `unavailable`/`unknown` pomijamy (wartość „sprzed” przechodzi przez nie bez zmian), a powtórzenie tej samej
    wartości (przeładowanie HA zmienia `last_changed`, nie wartość) niczego nie zwraca. `prev_state` None/nieznany =
    pierwsza znana wartość jest tylko punktem odniesienia. Wiersze starsze niż `since` (−1 s) są pomijane."""
    cur = None if prev_state is None or str(prev_state).lower() in _BAD_STATES else str(prev_state)
    floor = since - timedelta(seconds=1) if since else None
    out: list[tuple[datetime, str, str]] = []
    for when, state in sorted(history, key=lambda r: r[0]):
        if floor is not None and when < floor:
            continue
        if str(state).lower() in _BAD_STATES:
            continue
        if cur is None:
            cur = str(state)
        elif str(state) != cur:
            out.append((when, cur, str(state)))
            cur = str(state)
    return out


def _fetch_history(get_history, entity_ids: list[str], start: datetime) -> dict | None:
    if not entity_ids:
        return {}
    try:
        return get_history(entity_ids, start.astimezone(timezone.utc).isoformat(),
                           datetime.now(timezone.utc).isoformat())
    except Exception:                                    # historia to dodatek — dziennik ma działać bez niej
        logger.warning("Historia parametrów niedostępna — zmiany liczone ze stanu", exc_info=True)
        return None


def detect_changes(conn, states: list[dict], get_logbook=ha_client.get_logbook,
                   get_history=ha_client.get_history) -> list[dict]:
    """Loguje zmiany WARTOŚCI parametrów katalogu. `last_changed` nie wystarcza: przeładowanie HA odnawia go dla
    wszystkich encji bez zmiany wartości, a dwa szybkie przełączenia w jednym cyklu dają ten sam stan końcowy.
    Dlatego dla encji z nowym `last_changed` bierzemy historię recordera od poprzedniego odczytu i logujemy każde
    przejście wartości (A→B, B→A); brak historii = ostrożny zapas: jedna zmiana, tylko gdy stan faktycznie inny.
    Pierwsze uruchomienie tylko zapamiętuje stan. Encja niedostępna zostaje przy ostatniej znanej wartości, więc
    zmiana przez okres niedostępności też się złapie."""
    by_id = {s.get("entity_id"): s for s in states}
    seen = dbm.get_setting(conn, _LAST_SEEN_KEY) or {}
    current: dict[str, dict] = {}
    pending: list[tuple[str, str, dict, dict]] = []      # (klucz, entity_id, stan HA, poprzednio widziane)
    for key, eid in catalog.resolve_params(states).items():
        st = by_id[eid]
        now_state, changed = str(st.get("state")), st.get("last_changed")
        prev = seen.get(key)
        if now_state.lower() in _BAD_STATES:
            if prev is not None:
                current[key] = prev
            continue
        current[key] = {"state": now_state, "lc": changed}
        if prev is None or prev.get("lc") == changed:
            continue
        pending.append((key, eid, st, prev))

    starts = [t for _, _, _, prev in pending if (t := _parse_ts(prev.get("lc"))) is not None]
    hist = _fetch_history(get_history, [eid for _, eid, _, _ in pending], min(starts)) if starts else None

    logged: list[dict] = []
    for key, eid, st, prev in pending:
        now_state, changed = str(st.get("state")), st.get("last_changed")
        since = _parse_ts(prev.get("lc"))
        series = (hist or {}).get(eid)
        if series is not None and since is not None:
            found = [(w.isoformat(), o, n) for w, o, n in transitions(series, prev.get("state"), since)]
        elif str(prev.get("state")).lower() in _BAD_STATES or str(prev.get("state")) == now_state:
            found = []
        else:
            found = [(changed, prev.get("state"), now_state)]
        for i, (ts, old, new) in enumerate(found):
            # Kontekst stanu jest nadpisywany przy kolejnej ramce z pompy (co ~3 min), więc źródło bierzemy
            # z logbooka; kontekst stanu to zapasowy trop tylko dla ostatniego przejścia (to jego stan).
            fallback = change_source(st.get("context")) if i == len(found) - 1 else "nieznane"
            source = _logbook_source(get_logbook, eid, ts)
            source = source or fallback
            row = {"ts": ts, "key": key, "entity_id": eid, "old": old, "new": new, "source": source}
            conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) "
                         "VALUES (:ts, :key, :entity_id, :old, :new, :source)", row)
            logged.append(row)
    if logged:
        conn.commit()
        logger.info("Zmiany parametrów pompy: %s", [(r["key"], r["old"], r["new"], r["source"]) for r in logged])
    if current != seen:
        dbm.set_setting(conn, _LAST_SEEN_KEY, {**seen, **current})
    return logged


def migrate_fake_changes(conn, states: list[dict], get_history=ha_client.get_history) -> bool:
    """Jednorazowo (0.9.3): wpisy `old == new` z dziennika (przeładowania HA, dwa przełączenia w jednym cyklu) trafiają do
    `param_changes_legacy`, a realne przejścia wartości tych parametrów są odtwarzane z historii recordera (~7 dni).
    Zwraca True, gdy migracja jest zakończona; brak historii = spróbuje w następnym cyklu, niczego nie ruszając."""
    if dbm.get_setting(conn, _CHANGES_SCHEMA_KEY) == _CHANGES_SCHEMA:
        return True
    fakes = conn.execute("SELECT * FROM param_changes WHERE old IS NOT NULL AND old = new ORDER BY id").fetchall()
    resolved = catalog.resolve_params(states)
    keys = sorted({r["key"] for r in fakes if r["key"] in resolved})
    hist: dict = {}
    if keys:
        starts = [t for r in fakes if r["key"] in resolved and (t := _parse_ts(r["ts"])) is not None]
        hist = _fetch_history(get_history, [resolved[k] for k in keys], min(starts) - timedelta(hours=1)) if starts else {}
        if hist is None:
            return False
    conn.execute("INSERT INTO param_changes_legacy (id, ts, key, entity_id, old, new, source) "
                 "SELECT id, ts, key, entity_id, old, new, source FROM param_changes WHERE old IS NOT NULL AND old = new")
    conn.execute("DELETE FROM param_changes WHERE old IS NOT NULL AND old = new")
    restored = 0
    for key in keys:
        eid = resolved[key]
        have = [t for r in conn.execute("SELECT ts FROM param_changes WHERE key = ?", (key,))
                if (t := _parse_ts(r["ts"])) is not None]
        for when, old, new in transitions(hist.get(eid) or [], None):
            if any(abs((when - t).total_seconds()) <= 2 for t in have):
                continue                                  # ta zmiana jest już w dzienniku (prawdziwy wpis)
            source = _logbook_source(ha_client.get_logbook, eid, when.isoformat()) or "nieznane"
            conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) VALUES (?, ?, ?, ?, ?, ?)",
                         (when.isoformat(), key, eid, old, new, source))
            restored += 1
    dbm.set_setting(conn, _CHANGES_SCHEMA_KEY, _CHANGES_SCHEMA)
    conn.commit()
    logger.info("Dziennik zmian: %d fałszywych wpisów (old = new) przeniesiono do param_changes_legacy, odtworzono %d realnych",
                len(fakes), restored)
    return True


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


def run(conn, settings: dict, now: datetime, get_states=ha_client.get_all_states,
        get_history=ha_client.get_history) -> dict:
    """Jeden krok telemetrii (z cyklu pętli A): próbka + zmiany parametrów + retencja raz na dobę."""
    states = get_states()
    if not states:
        return {"samples": 0, "changes": 0, "ok": False}
    stored = store_sample(conn, now, collect_values(settings, states))
    migrate_fake_changes(conn, states, get_history)
    changes = detect_changes(conn, states, get_history=get_history)
    reattribute_unknown(conn)
    if now.hour == 3 and now.minute < 15:                         # raz na dobę, poza szczytem
        apply_retention(conn, now)
    return {"samples": stored, "changes": len(changes), "ok": True}
