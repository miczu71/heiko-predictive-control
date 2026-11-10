"""API definicji testów (E5a): czyste funkcje na `conn`, bez Flask i bez wywołań usług HA.

Własne definicje są w `test_defs` (`builtin = 0`); wbudowane (`builtin = 1` albo klucz w `engine.BUILTIN`) są tylko do
odczytu. Zapis przechodzi przez `schema.validate`; do SQL trafia wyłącznie sparametryzowany `def_json`. Usunięcie definicji
nie rusza `tests.def_json` (zamrożona kopia trwającego testu i historii).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .. import experiments as ex
from .. import tariff_feed
from . import engine, schema
from .builtin import BUILTIN

KEY_RE = re.compile(r"[a-z][a-z0-9_]{1,39}")
MAX_BYTES = 65536
PREVIEW_DAYS = 14


def validate_key(key) -> str | None:
    """Komunikat błędu klucza albo None. Zarezerwowane klucze wbudowanych to 409 w `create` (nie błąd formatu)."""
    if not (isinstance(key, str) and KEY_RE.fullmatch(key)):
        return "key: format ^[a-z][a-z0-9_]{1,39}$"
    return None


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _row(conn, key: str):
    return conn.execute("SELECT key, def_json, builtin FROM test_defs WHERE key = ?", (key,)).fetchone()


def _is_builtin(conn, key: str) -> bool:
    if key in BUILTIN:
        return True
    row = _row(conn, key)
    return bool(row and row["builtin"])


def _load(raw) -> dict | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def get(conn, key: str) -> dict | None:
    """`{"definition", "builtin"}`; wbudowana bez wiersza w bazie z kodu; brak = None. Uszkodzony JSON → `definition` None."""
    if not isinstance(key, str):
        return None
    row = _row(conn, key)
    if row is not None:
        return {"definition": _load(row["def_json"]), "builtin": bool(row["builtin"]) or key in BUILTIN}
    if key in BUILTIN:
        return {"definition": json.loads(json.dumps(BUILTIN[key])), "builtin": True}
    return None


def list_all(conn) -> list[dict]:
    """Wbudowane w kolejności `BUILTIN`, potem własne alfabetycznie. `errors` = `schema.validate` zapisanej definicji."""
    rows = {r["key"]: r for r in conn.execute("SELECT key, def_json, builtin FROM test_defs ORDER BY key")}
    out = []
    for key in [*BUILTIN, *sorted(k for k in rows if k not in BUILTIN)]:
        row = rows.get(key)
        item = ({"definition": _load(row["def_json"]), "builtin": bool(row["builtin"]) or key in BUILTIN}
                if row is not None else get(conn, key))
        defn = item["definition"]
        errors = schema.validate(defn) if defn is not None else ["zapisany JSON jest uszkodzony"]
        defn = defn or {}
        out.append({"key": key, "title": defn.get("title") or key, "description": defn.get("description", ""),
                    "builtin": item["builtin"], "errors": errors})
    return out


def _check(defn) -> list[str]:
    errors = schema.validate(defn)
    if not errors and (err := validate_key(defn["key"])):
        errors = [err]
    return errors


def create(conn, defn, now: datetime) -> tuple[bool, list[str] | None, int]:
    """(ok, błędy, status HTTP): 201, 400 (walidacja), 409 (zajęty albo zarezerwowany klucz)."""
    errors = _check(defn)
    if errors:
        return False, errors, 400
    key = defn["key"]
    if key in BUILTIN or _is_builtin(conn, key):
        return False, [f"key: {key} to klucz wbudowanego testu (zarezerwowany) — wybierz inny"], 409
    if _row(conn, key) is not None:
        return False, [f"key: definicja {key} już istnieje"], 409
    stamp = _stamp(now)
    conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES (?, ?, 0, ?, ?)",
                 (key, json.dumps(defn), stamp, stamp))
    conn.commit()
    return True, None, 201


def update(conn, key: str, defn, now: datetime) -> tuple[bool, list[str] | None, int]:
    """(ok, błędy, status): 200, 400 (walidacja albo `key` ciała ≠ URL), 403 (wbudowana), 404."""
    if validate_key(key):
        return False, [validate_key(key)], 400
    if _is_builtin(conn, key):
        return False, [f"{key}: test wbudowany jest tylko do odczytu — zrób kopię pod nowym kluczem"], 403
    if _row(conn, key) is None:
        return False, [f"{key}: nie ma takiej definicji"], 404
    if isinstance(defn, dict) and defn.get("key") != key:
        return False, [f"key: musi być zgodny z adresem ({key})"], 400
    errors = _check(defn)
    if errors:
        return False, errors, 400
    conn.execute("UPDATE test_defs SET def_json = ?, updated = ? WHERE key = ? AND builtin = 0",
                 (json.dumps(defn), _stamp(now), key))
    conn.commit()
    return True, None, 200


def delete(conn, key: str) -> tuple[bool, list[str] | None, int]:
    """(ok, błędy, status): 200, 403 (wbudowana), 404. Nie rusza `tests.def_json`."""
    if _is_builtin(conn, key):
        return False, [f"{key}: test wbudowany jest tylko do odczytu"], 403
    if _row(conn, key) is None:
        return False, [f"{key}: nie ma takiej definicji"], 404
    conn.execute("DELETE FROM test_defs WHERE key = ? AND builtin = 0", (key,))
    conn.commit()
    return True, None, 200


def preview(conn, settings: dict, defn, now: datetime, get_state, get_numeric, is_workday) -> dict:
    """`{"ok", "errors", "preview"}` — ta sama ścieżka co Start (`experiments._plan_def`), bez zapisu."""
    errors = _check(defn)
    if errors:
        return {"ok": False, "errors": errors, "preview": None}
    shift, curve_on = ex._read(settings, get_state, get_numeric)
    target = engine.writes_target(defn)[0]
    p0 = ex._read_p0(settings, get_numeric) if target == "p0" else None
    steps, arms, blocked = ex._plan_def(defn, settings, now, shift, curve_on, ex.running_test(conn), is_workday, p0,
                                        ex._after_missing(conn, defn), tariff_feed.for_settings(conn, settings))
    horizon = (now.date() + timedelta(days=PREVIEW_DAYS)).isoformat()
    today = now.date().isoformat()
    writes = engine.has_writes(defn)
    end = None if writes else engine.end_at(defn, arms)
    return {"ok": True, "errors": [],
            "preview": {"schedule": steps,
                        "arms_by_day": {d: a for d, a in arms.items() if today <= d < horizon},
                        "blocked": blocked,
                        "end_at": end.isoformat(timespec="seconds") if end else None,
                        "writes": writes}}


def export_all(conn) -> dict:
    """Tylko własne definicje (bez wbudowanych i bez uszkodzonych)."""
    out = []
    for row in conn.execute("SELECT key, def_json FROM test_defs WHERE builtin = 0 ORDER BY key"):
        if row["key"] not in BUILTIN and (defn := _load(row["def_json"])) is not None:
            out.append(defn)
    return {"version": 1, "definitions": out}
