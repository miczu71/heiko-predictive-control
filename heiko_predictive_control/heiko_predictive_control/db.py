"""SQLite: ustawienia edytowalne w UI (przeżywają restart, niezależne od
options.json Supervisora) + historia cykli (dry-run, do zakładki Statystyki
i do dostrajania współczynnika EWMA)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    loop TEXT NOT NULL,              -- 'heiko' | 'attic'
    active_profile TEXT NOT NULL,    -- 'komfort' | 'ekonomia'
    tariff_peak INTEGER,             -- 0/1/NULL
    price_pln_kwh REAL,
    outdoor_temp_c REAL,
    indoor_temp_c REAL,
    setpoint_komfort REAL,
    setpoint_ekonomia REAL,
    coefficient REAL,                -- współczynnik strat cieplnych (loop='heiko')
    baseline_cost_today_pln REAL,
    sim_cost_today_komfort_pln REAL,
    sim_cost_today_ekonomia_pln REAL,
    write_enabled INTEGER NOT NULL   -- czy pętla miała wtedy włączony zapis
);
CREATE INDEX IF NOT EXISTS idx_cycles_loop_ts ON cycles(loop, ts);

-- Doradca D1 (0.8.0): telemetria pompy co cykl. Wąska tabela (ts w sekundach UTC, klucz -> id),
-- surowe dane 2 lata, starsze zwijane do agregatu godzinowego (telemetry.apply_retention).
CREATE TABLE IF NOT EXISTS telemetry_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS telemetry (
    ts INTEGER NOT NULL,
    key_id INTEGER NOT NULL,
    value REAL,
    PRIMARY KEY (ts, key_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS telemetry_hourly (
    hour_ts INTEGER NOT NULL,
    key_id INTEGER NOT NULL,
    mean REAL, min REAL, max REAL,
    n INTEGER NOT NULL,
    PRIMARY KEY (hour_ts, key_id)
) WITHOUT ROWID;

-- Każda wykryta zmiana parametru z katalogu (dowolne źródło: panel, HA, automatyzacja) — licznik zapisów.
CREATE TABLE IF NOT EXISTS param_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                -- czas zmiany wg HA (ISO)
    key TEXT NOT NULL,               -- klucz katalogu
    entity_id TEXT NOT NULL,
    old TEXT,
    new TEXT,
    source TEXT NOT NULL             -- 'użytkownik HA' | 'automatyzacja/skrypt' | 'nieznane'
);
CREATE INDEX IF NOT EXISTS idx_param_changes_ts ON param_changes(ts);

-- Wpisy dziennika z `old = new` sprzed 0.9.3 (przeładowania HA liczone jako zmiany) — kopia przed migracją, nie liczy się nigdzie.
CREATE TABLE IF NOT EXISTS param_changes_legacy (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    key TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    old TEXT,
    new TEXT,
    source TEXT NOT NULL
);

-- Dobowe streszczenia (summaries.py): temat -> JSON.
CREATE TABLE IF NOT EXISTS daily_summary (
    day TEXT NOT NULL,
    topic TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (day, topic)
);

-- Ostatni policzony raport analizy (analysis.py), do pokazania bez ponownego liczenia LTS.
CREATE TABLE IF NOT EXISTS report_cache (
    name TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    data TEXT NOT NULL
);

-- Doradca D2 (0.9.0): propozycje analizatorów. W D2 „zatwierdź/odrzuć” zapisuje tylko decyzję (trial=1) —
-- nic nie trafia do pompy. Ta sama para (analyzer, dedupe_key) w stanie 'oczekuje' jest aktualizowana, nie dublowana.
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    analyzer TEXT NOT NULL,          -- curve | dhw | anomaly
    dedupe_key TEXT NOT NULL,
    lens TEXT NOT NULL,              -- komfort | ekonomia | obie
    kind TEXT NOT NULL,              -- zmiana | eksperyment | cofnięcie | alert
    param_key TEXT,                  -- klucz katalogu (catalog.py) albo NULL
    from_value REAL,
    to_value REAL,                   -- NULL = sam tekst (alert / parametr prowadzony przez automatyzację)
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL,          -- JSON: liczby, okno danych
    effects TEXT NOT NULL,           -- JSON: przewidywane skutki (komfort, zł/dobę, kWh)
    confidence TEXT NOT NULL,        -- niska | średnia | wysoka
    expires TEXT NOT NULL,
    status TEXT NOT NULL,            -- oczekuje | zatwierdzona | odrzucona | wygasła | zastąpiona
    decided_at TEXT,
    trial INTEGER NOT NULL DEFAULT 1,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, analyzer, dedupe_key);
"""


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Kolumny dodane po 0.3.0 (pętla B steruje AC). Dokładane do istniejącej tabeli
# `cycles` bez utraty historii — SQLite nie ma `ADD COLUMN IF NOT EXISTS`.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("phase", "TEXT"),                # faza pętli B (patrz attic.py)
    ("ac_cmd_setpoint", "REAL"),      # ostatnia nastawa zlecona AC
    ("offset_c", "REAL"),             # uczony offset nastawy względem celu
    ("ac_power_w", "REAL"),
    ("window_open", "INTEGER"),
    ("door_open", "INTEGER"),
    ("wrote", "INTEGER"),             # czy w tym cyklu zapisano do AC
    ("attic_energy_kwh", "REAL"),     # energia AC w cyklu (całkowanie mocy)
    ("attic_cost_pln", "REAL"),       # koszt tej energii wg bieżącej ceny
    ("planned_start", "TEXT"),        # planowany start dogrzewania (ISO)
    ("presence", "INTEGER"),          # czujnik obecności: 1 jest / 0 brak / NULL nieznane
    ("vacant_min", "REAL"),           # minuty pustki liczone od początku okna pracy
    # Pętla A (0.6.0) — dane fazy cienia: z nich uczy się model podłogówki.
    ("water_temp_c", "REAL"),         # temperatura wody grzewczej (skraplacz)
    ("heating_active", "INTEGER"),    # pompa w trybie grzania (0/1/NULL)
    ("dhw_active", "INTEGER"),        # pompa robi CWU w tym kroku (0/1/NULL)
    ("energy_kwh", "REAL"),           # energia pompy w kroku wg licznika
    ("heat_kw", "REAL"),              # moc cieplna do domu (COP × moc), 0 poza grzaniem
    ("base_curve_c", "REAL"),         # równoważnik krzywej grzewczej teraz
    ("plan_setpoint_c", "REAL"),      # nastawa z planu aktywnego profilu (blok bieżący)
    ("model_err_c", "REAL"),          # błąd prognozy 1 kroku: zmierzone − przewidziane
    ("min_room_c", "REAL"),           # najzimniejszy pokój w strefach dziennych
    ("min_room_name", "TEXT"),
    # 0.7.0 — obserwacja natywnego mechanizmu (krzywa + ograniczona nastawa w pompie).
    ("water_setpoint_c", "REAL"),     # aktualny cel wody wg pompy (krzywa + przesunięcie + ograniczenie)
    ("curve_on", "INTEGER"),          # krzywa grzewcza włączona (0/1/NULL)
    ("reduced_active", "INTEGER"),    # wnioskowane: cel wody poniżej krzywej (0/1/NULL = nie wiadomo)
)


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(cycles)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE cycles ADD COLUMN {name} {decl}")
    # Streszczenia dobowe: 0.8.3 zmieniło jednostki backup (godziny -> minuty), 0.8.4 poprawiło doby bez zmiany licznika.
    # Przy podniesieniu wersji schematu stare streszczenia są kasowane, a backfill (ostatnie 7 dób) je odtwarza.
    row = conn.execute("SELECT value FROM settings WHERE key = 'summary_schema'").fetchone()
    if row is None or row["value"] != "3":
        conn.execute("DELETE FROM daily_summary")
        conn.execute("INSERT INTO settings (key, value) VALUES ('summary_schema', '3') "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    _migrate_catalog_0_9_3(conn)
    conn.commit()


# Klucze katalogu usunięte w 0.9.3 (anty-legionella poza add-onem) albo przepięte na inną encję (`backup_heater`:
# alias switch → select slotu 50). Ich „ostatnio widziany stan” nie ma sensu dla nowej encji.
_DROPPED_KEYS = ("anti_leg_program", "anti_leg_setpoint", "anti_leg_duration", "anti_leg_finish")
_REPOINTED_KEYS = ("backup_heater",)


def _migrate_catalog_0_9_3(conn: sqlite3.Connection) -> None:
    if get_setting(conn, "catalog_schema") == 2:
        return
    managed = get_setting(conn, "advisor_managed_keys")
    if isinstance(managed, str):
        kept = [k.strip() for k in managed.split(",") if k.strip() and k.strip() not in _DROPPED_KEYS]
        set_setting(conn, "advisor_managed_keys", ",".join(kept))
    seen = get_setting(conn, "catalog_last_seen")
    if isinstance(seen, dict):
        set_setting(conn, "catalog_last_seen",
                    {k: v for k, v in seen.items() if k not in _DROPPED_KEYS + _REPOINTED_KEYS})
    set_setting(conn, "catalog_schema", 2)


def seed_from_options(conn: sqlite3.Connection, options: dict[str, Any]) -> None:
    """Zasila `settings` z options.json TYLKO dla kluczy, które jeszcze nie
    istnieją w bazie — kolejne starty add-onu nie nadpisują tego, co user
    zmienił w UI (ten sam wzorzec co fuel_tracker/settingsm.seed_from_options)."""
    cur = conn.cursor()
    for key, value in options.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def get_all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            out[row["key"]] = json.loads(row["value"])
        except (TypeError, ValueError):
            out[row["key"]] = row["value"]
    return out


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def insert_cycle(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = ("ts", "loop", "active_profile", "tariff_peak", "price_pln_kwh",
            "outdoor_temp_c", "indoor_temp_c", "setpoint_komfort",
            "setpoint_ekonomia", "coefficient", "baseline_cost_today_pln",
            "sim_cost_today_komfort_pln", "sim_cost_today_ekonomia_pln",
            "write_enabled", *(name for name, _ in _ADDED_COLUMNS))
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO cycles ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )
    conn.commit()


def latest_cycle(conn: sqlite3.Connection, loop: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cycles WHERE loop = ? ORDER BY id DESC LIMIT 1",
        (loop,),
    ).fetchone()


def recent_cycles(conn: sqlite3.Connection, loop: str, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cycles WHERE loop = ? ORDER BY id DESC LIMIT ?",
        (loop, limit),
    ).fetchall()


def today_cycles(conn: sqlite3.Connection, loop: str, day_prefix: str) -> list[sqlite3.Row]:
    """`day_prefix` np. '2026-09-25' (porównanie prefiksem ISO ts)."""
    return conn.execute(
        "SELECT * FROM cycles WHERE loop = ? AND ts LIKE ? ORDER BY id ASC",
        (loop, f"{day_prefix}%"),
    ).fetchall()


def attic_today_totals(conn: sqlite3.Connection, day_prefix: str) -> dict[str, float]:
    """Energia (kWh) i koszt (PLN) AC poddasza od północy — suma przyrostów cykli."""
    row = conn.execute(
        "SELECT COALESCE(SUM(attic_energy_kwh), 0) AS kwh, "
        "COALESCE(SUM(attic_cost_pln), 0) AS pln "
        "FROM cycles WHERE loop = 'attic' AND ts LIKE ?", (f"{day_prefix}%",)).fetchone()
    return {"kwh": float(row["kwh"]), "pln": float(row["pln"])}
