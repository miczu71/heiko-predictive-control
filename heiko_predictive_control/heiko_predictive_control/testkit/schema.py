"""Walidator definicji testu (JSON). Czysty moduł, bez I/O.

`validate(defn)` zwraca listę komunikatów po polsku (pusta = poprawna). Każdy komunikat zaczyna się od ścieżki pola.
Nie rzuca na złym typie wejścia — zwraca błąd.
"""
from __future__ import annotations

import json
import math
import re

from .params import MEASURE_PARAMS, MEASURES, OVERRIDABLE, PUMP_PARAMS

MAX_BYTES = 16 * 1024
MAX_DEPTH = 6
MAX_DESCRIPTION = 500

TIME = r"(?:[01]\d|2[0-3]):[0-5]\d"
KEY_RE = re.compile(r"[a-z][a-z0-9_]{2,39}")
ARM_RE = re.compile(r"[A-Z]")
HHMM_RE = re.compile(TIME)
ZONE_RE = re.compile(r"zone:[a-z0-9_]{1,40}")
BASE_RE = re.compile(r"base[+-]\d+(?:\.\d+)?")
WINDOW_RE = re.compile(rf"{TIME}\s*(?:→|-)\s*{TIME}")

TOP_FIELDS = {"key", "title", "description", "requires", "coexist", "days", "design", "order", "arms",
              "windows", "restore", "measures", "verdict", "abort"}
REQUIRED_FIELDS = ("key", "title", "design", "arms", "days")
DESIGNS = {"single", "sequence", "alternate"}
FILTERS = {"workday", "all", "peak_day"}
COEXIST_ALLOWED = {"g13_peak"}
REQUIRES_FIELDS = {"curve_on", "after", "heiko_enabled"}
ARM_FIELDS = {"overrides", "writes"}
WRITE_FIELDS = {"param", "value", "from", "to"}
VERDICT_FIELDS = {"primary", "better", "min_gain", "guards", "weather_band_c", "min_n"}
GUARD_FIELDS = {"measure", "op", "value"}
ABORT_FIELDS = {"sustained_cold"}


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _one_of(x, options) -> bool:
    return isinstance(x, str) and x in options


def _join(path: str, key) -> str:
    return f"{path}.{key}" if path else str(key)


def _unknown_fields(obj: dict, allowed, path: str, errors: list[str]) -> None:
    for k in obj:
        if k not in allowed:
            errors.append(f"{_join(path, k)}: nieznane pole")


def _too_deep(obj) -> bool:
    """Iteracyjnie, z odcięciem: cykle i bardzo zagnieżdżone wejścia nie wywołują RecursionError."""
    stack = [(obj, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            return True
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return False


def _check_writes(path: str, writes, errors: list[str]) -> None:
    if not isinstance(writes, list):
        errors.append(f"{path}: oczekiwano listy")
        return
    for i, w in enumerate(writes):
        wp = f"{path}[{i}]"
        if not isinstance(w, dict):
            errors.append(f"{wp}: oczekiwano obiektu {{param, value}}")
            continue
        _unknown_fields(w, WRITE_FIELDS, wp, errors)
        param = w.get("param")
        if "param" not in w:
            errors.append(f"{wp}.param: brak wymaganego pola")
        elif not _one_of(param, PUMP_PARAMS):
            errors.append(f"{wp}.param: nieznana nastawa {param!r}")
        if "value" not in w:
            errors.append(f"{wp}.value: brak wymaganego pola")
        else:
            value = w["value"]
            if _is_num(value):
                # Zakres bez skoku: skok (względem bieżącej wartości) sprawdza `_write` przy zapisie.
                if _one_of(param, PUMP_PARAMS) and not PUMP_PARAMS[param].check(value, value):
                    errors.append(f"{wp}.value: wartość {value} spoza reguły zapisu dla {param}")
            elif not (isinstance(value, str) and BASE_RE.fullmatch(value)):
                errors.append(f"{wp}.value: oczekiwano liczby albo base+N / base-N")
        for edge in ("from", "to"):
            if edge in w:
                v = w[edge]
                if not (isinstance(v, str) and (HHMM_RE.fullmatch(v) or ZONE_RE.fullmatch(v))):
                    errors.append(f"{wp}.{edge}: oczekiwano HH:MM albo zone:<nazwa>")


def _check_arms(arms: dict, errors: list[str]) -> None:
    if not 1 <= len(arms) <= 4:
        errors.append("arms: wymagane 1–4 wariantów")
    for arm, body in arms.items():
        ap = f"arms.{arm}"
        if not (isinstance(arm, str) and ARM_RE.fullmatch(arm)):
            errors.append(f"arms.{arm}: klucz wariantu to jedna wielka litera")
        if not isinstance(body, dict):
            errors.append(f"{ap}: oczekiwano obiektu")
            continue
        _unknown_fields(body, ARM_FIELDS, ap, errors)
        overrides = body.get("overrides", {})
        if not isinstance(overrides, dict):
            errors.append(f"{ap}.overrides: oczekiwano obiektu")
        else:
            for name, value in overrides.items():
                op = f"{ap}.overrides.{name}"
                if name not in OVERRIDABLE:
                    errors.append(f"{op}: nieznana opcja (niedozwolona do nadpisania)")
                    continue
                if not _is_num(value):
                    errors.append(f"{op}: oczekiwano liczby")
                    continue
                bounds = OVERRIDABLE[name]
                if bounds is not None and not bounds[0] <= value <= bounds[1]:
                    errors.append(f"{op}: poza zakresem {bounds[0]}–{bounds[1]}")
        if "writes" in body:
            _check_writes(f"{ap}.writes", body["writes"], errors)


def _has_writes(defn: dict) -> bool:
    arms = defn.get("arms")
    if not isinstance(arms, dict):
        return False
    return any(isinstance(b, dict) and isinstance(b.get("writes"), list) and b["writes"] for b in arms.values())


def _check_measure(i: int, item, errors: list[str]) -> str | None:
    """Zwraca nazwę miary (gdy poprawna) albo None."""
    mp = f"measures[{i}]"
    if isinstance(item, str):
        name, params = item, {}
    elif isinstance(item, dict) and isinstance(item.get("name"), str):
        name = item["name"]
        params = {k: v for k, v in item.items() if k != "name"}
    else:
        errors.append(f"{mp}: oczekiwano nazwy miary albo obiektu z polem name")
        return None
    if name not in MEASURES:
        errors.append(f"{mp}: nieznana miara {name}")
        return None
    spec = MEASURE_PARAMS.get(name, {})
    for p in params:
        if p not in spec:
            errors.append(f"{mp}.{p}: nieznany parametr miary {name}")
    for p, required in spec.items():
        if required and p not in params:
            errors.append(f"{mp}: miara {name} wymaga parametru {p}")
    if "window" in params and not (isinstance(params["window"], str) and WINDOW_RE.fullmatch(params["window"])):
        errors.append(f"{mp}.window: oczekiwano HH:MM→HH:MM")
    if "per" in params and params["per"] != "hdd":
        errors.append(f"{mp}.per: dozwolone tylko hdd")
    if "at" in params and not (isinstance(params["at"], str) and HHMM_RE.fullmatch(params["at"])):
        errors.append(f"{mp}.at: oczekiwano HH:MM")
    return name


def _check_measures(measures, errors: list[str]) -> set[str]:
    if not isinstance(measures, list):
        errors.append("measures: oczekiwano listy")
        return set()
    names = set()
    for i, item in enumerate(measures):
        name = _check_measure(i, item, errors)
        if name:
            names.add(name)
    return names


def _check_verdict(verdict: dict, names: set[str], errors: list[str]) -> None:
    _unknown_fields(verdict, VERDICT_FIELDS, "verdict", errors)
    if not _one_of(verdict.get("primary"), names):
        errors.append("verdict.primary: wymagana miara z listy measures")
    if verdict.get("better") not in ("lower", "higher") or not isinstance(verdict.get("better"), str):
        errors.append("verdict.better: dozwolone lower|higher")
    if "min_gain" in verdict and not (_is_num(verdict["min_gain"]) and verdict["min_gain"] >= 0):
        errors.append("verdict.min_gain: oczekiwano liczby ≥ 0")
    if "weather_band_c" in verdict:
        wb = verdict["weather_band_c"]
        if not (_is_num(wb) and 0 <= wb <= 10):
            errors.append("verdict.weather_band_c: oczekiwano liczby 0–10")
    if "min_n" in verdict:
        mn = verdict["min_n"]
        if not (_is_int(mn) and 1 <= mn <= 30):
            errors.append("verdict.min_n: oczekiwano całkowitej 1–30")
    if "guards" in verdict:
        guards = verdict["guards"]
        if not isinstance(guards, list):
            errors.append("verdict.guards: oczekiwano listy")
            return
        for i, g in enumerate(guards):
            gp = f"verdict.guards[{i}]"
            if not isinstance(g, dict):
                errors.append(f"{gp}: oczekiwano obiektu {{measure, op, value}}")
                continue
            _unknown_fields(g, GUARD_FIELDS, gp, errors)
            if not _one_of(g.get("measure"), names):
                errors.append(f"{gp}.measure: wymagana miara z listy measures")
            if g.get("op") not in (">=", "<=") or not isinstance(g.get("op"), str):
                errors.append(f"{gp}.op: dozwolone >= albo <=")
            if not _is_num(g.get("value")):
                errors.append(f"{gp}.value: oczekiwano liczby")


def validate(defn) -> list[str]:
    """Lista błędów definicji testu; pusta lista = definicja poprawna."""
    if not isinstance(defn, dict):
        return ["definicja: oczekiwano obiektu JSON"]
    if _too_deep(defn):
        return ["definicja: zbyt głęboka (ponad 6 poziomów)"]
    try:
        size = len(json.dumps(defn, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ["definicja: nie jest poprawnym JSON"]
    if size > MAX_BYTES:
        return ["definicja: zbyt duża (ponad 16 KB)"]

    errors: list[str] = []
    _unknown_fields(defn, TOP_FIELDS, "", errors)
    for f in REQUIRED_FIELDS:
        if f not in defn:
            errors.append(f"{f}: brak wymaganego pola")

    if "key" in defn and not (isinstance(defn["key"], str) and KEY_RE.fullmatch(defn["key"])):
        errors.append("key: format ^[a-z][a-z0-9_]{2,39}$")
    if "title" in defn and not (isinstance(defn["title"], str) and 1 <= len(defn["title"]) <= 80):
        errors.append("title: 1–80 znaków")
    if "description" in defn and not (isinstance(defn["description"], str)
                                      and len(defn["description"]) <= MAX_DESCRIPTION):
        errors.append(f"description: tekst do {MAX_DESCRIPTION} znaków")
    if "design" in defn and not _one_of(defn["design"], DESIGNS):
        errors.append("design: dozwolone single|sequence|alternate")

    if "days" in defn:
        days = defn["days"]
        if not isinstance(days, dict):
            errors.append("days: oczekiwano obiektu")
        else:
            _unknown_fields(days, {"filter", "count"}, "days", errors)
            if not _one_of(days.get("filter"), FILTERS):
                errors.append("days.filter: dozwolone workday|all|peak_day")
            if not (_is_int(days.get("count")) and 1 <= days["count"] <= 60):
                errors.append("days.count: oczekiwano całkowitej 1–60")

    arm_keys: list[str] = []
    if "arms" in defn:
        if not isinstance(defn["arms"], dict):
            errors.append("arms: oczekiwano obiektu")
        else:
            _check_arms(defn["arms"], errors)
            arm_keys = [k for k in defn["arms"] if isinstance(k, str)]
            if "design" in defn and defn["design"] == "single" and len(defn["arms"]) != 1:
                errors.append("arms: single wymaga dokładnie 1 wariantu")
            if "design" in defn and defn["design"] == "alternate" and len(defn["arms"]) < 2:
                errors.append("arms: alternate wymaga ≥2 wariantów")

    if "order" in defn:
        order = defn["order"]
        if not (isinstance(order, list) and all(isinstance(x, str) for x in order)
                and len(order) == len(set(order)) and set(order) == set(arm_keys)):
            errors.append("order: musi zawierać dokładnie klucze arms (bez powtórzeń)")
    elif defn.get("design") == "alternate":
        errors.append("order: wymagane dla design alternate")

    if "coexist" in defn:
        coexist = defn["coexist"]
        if not (isinstance(coexist, list) and all(_one_of(c, COEXIST_ALLOWED) for c in coexist)):
            errors.append("coexist: dozwolone tylko g13_peak")
        elif _has_writes(defn) and "g13_peak" in coexist:
            errors.append("coexist: g13_peak nie łączy się z writes (dwóch pisarzy krzywej)")

    if "requires" in defn:
        req = defn["requires"]
        if not isinstance(req, dict):
            errors.append("requires: oczekiwano obiektu")
        else:
            _unknown_fields(req, REQUIRES_FIELDS, "requires", errors)
            for flag in ("curve_on", "heiko_enabled"):
                if flag in req and not isinstance(req[flag], bool):
                    errors.append(f"requires.{flag}: oczekiwano true/false")
            if "after" in req and not (isinstance(req["after"], list)
                                       and all(isinstance(x, str) for x in req["after"])):
                errors.append("requires.after: oczekiwano listy kluczy definicji")

    if "windows" in defn:
        windows = defn["windows"]
        if not isinstance(windows, list):
            errors.append("windows: oczekiwano listy")
        else:
            for i, w in enumerate(windows):
                if not (isinstance(w, str) and (HHMM_RE.fullmatch(w) or ZONE_RE.fullmatch(w))):
                    errors.append(f"windows[{i}]: oczekiwano HH:MM albo zone:<nazwa>")

    if "restore" in defn:
        if not isinstance(defn["restore"], bool):
            errors.append("restore: oczekiwano true/false")
        elif defn["restore"] is False and _has_writes(defn):
            errors.append("restore: false niedozwolone, gdy są writes (test musi wracać do bazy)")

    names: set[str] = set()
    if "measures" in defn:
        names = _check_measures(defn["measures"], errors)
    if "verdict" in defn:
        if not isinstance(defn["verdict"], dict):
            errors.append("verdict: oczekiwano obiektu")
        else:
            _check_verdict(defn["verdict"], names, errors)

    if "abort" in defn:
        abort = defn["abort"]
        if not isinstance(abort, dict):
            errors.append("abort: oczekiwano obiektu")
        else:
            _unknown_fields(abort, ABORT_FIELDS, "abort", errors)
            if "sustained_cold" in abort and not isinstance(abort["sustained_cold"], dict):
                errors.append("abort.sustained_cold: oczekiwano obiektu")
    return errors
