"""E4: migracja `test_defs` i kolumn `tests.def_key/def_json/arms_json` — idempotentna, addytywna, z backfillem."""
import json
import sqlite3

from heiko_predictive_control import db as dbm
from heiko_predictive_control.testkit import engine
from heiko_predictive_control.testkit.builtin import BUILTIN

OLD_TESTS = """
CREATE TABLE tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, status TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
    schedule TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, baseline REAL NOT NULL, result TEXT, abort_reason TEXT);
"""


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_has_table_and_columns():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    assert _columns(conn, "test_defs") == {"key", "def_json", "builtin", "created", "updated"}
    assert {"def_key", "def_json", "arms_json"} <= _columns(conn, "tests")


def test_migrate_twice_is_harmless():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    dbm.migrate(conn)
    assert {"def_key", "def_json", "arms_json"} <= _columns(conn, "tests")


def test_old_tests_table_gets_columns_and_backfill():
    conn = dbm.get_conn(":memory:")
    conn.executescript(OLD_TESTS)
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('p0_ab', 'zakończony', 'x', '[]', 1)")
    conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES ('przyszly', 'zakończony', 'x', '[]', 1)")
    conn.commit()
    dbm.migrate(conn)
    rows = conn.execute("SELECT kind, def_key, def_json, arms_json FROM tests ORDER BY id").fetchall()
    assert rows[0]["def_key"] == "p0_ab" and json.loads(rows[0]["def_json"]) == BUILTIN["p0_ab"]
    assert rows[0]["arms_json"] is None
    assert rows[1]["def_key"] is None and rows[1]["def_json"] is None          # nieznany kind bez zmian
    dbm.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM tests").fetchone()[0] == 2


def test_seed_builtin_inserts_three_and_is_idempotent():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    engine.seed_builtin(conn)
    engine.seed_builtin(conn)
    rows = conn.execute("SELECT key, def_json, builtin FROM test_defs ORDER BY key").fetchall()
    assert [r["key"] for r in rows] == ["bezwladnosc", "p0_ab", "zapis"] and all(r["builtin"] == 1 for r in rows)
    assert json.loads(rows[1]["def_json"]) == BUILTIN["p0_ab"]


def test_seed_builtin_overwrites_stale_builtin_but_keeps_user_defs():
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    engine.seed_builtin(conn)
    conn.execute("UPDATE test_defs SET def_json = '{}' WHERE key = 'zapis'")
    conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES ('moj_test', '{}', 0, 'a', 'a')")
    conn.commit()
    engine.seed_builtin(conn)
    assert json.loads(conn.execute("SELECT def_json FROM test_defs WHERE key = 'zapis'").fetchone()[0]) == BUILTIN["zapis"]
    assert conn.execute("SELECT def_json FROM test_defs WHERE key = 'moj_test'").fetchone()[0] == "{}"
