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
"""


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


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
            "write_enabled")
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
