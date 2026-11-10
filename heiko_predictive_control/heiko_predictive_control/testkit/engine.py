"""Silnik testów z definicji (E4): wyszukanie i zasiew definicji, harmonogram w formacie `tests.schedule`, walidacja
kroków, nakładka ustawień, wynik/odznaka/komunikat końcowy wg miary głównej.

Bez wywołań usług HA (zapisy do pompy robi wyłącznie `experiments._write*`). `experiments` importuje ten moduł
wewnątrz funkcji (testkit.* importują `experiments` na górze — cykl).
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import date, datetime, time, timedelta

from .. import experiments as ex
from . import compat, measures, overlay, schedule
from .builtin import BUILTIN
from .measures import Ctx, arm_days_from_plan
from .params import PUMP_PARAMS

logger = logging.getLogger(__name__)

SUPPORTED_PARAMS = ("curve_shift", "p0_run", "p0_stop", "slot52", "dhw")      # nastawy dozwolone w `writes` definicji
DEFAULT_WINDOW = "00:00→00:00"

# Opis miary głównej w kartach (definicje spoza `experiments.KINDS` nie mają własnego tekstu).
MEASURE_TEXT = {
    "floor_cost": "średni dobowy koszt ogrzewania podłogowego wariantu (niższy lepszy), ze strażnikami komfortu",
    "kwh_day": "średnie zużycie energii pompy na dobę (kWh) w wariancie",
    "min_room_at": "najniższa temperatura najzimniejszego pokoju o wskazanej godzinie (najgorsza doba)",
    "safety_trips": "liczba dób z zadziałaniem bezpiecznika trybu Szczyt G13",
    "zone_spread": "średni rozrzut temperatur stref w standby w wariancie",
    "excitation": "dostateczne wymuszenie (przejścia i spadek celu wody) do uczenia modelu",
    "inertia_refit": "dopasowanie modelu bezwładności podłogi (RMSE)",
    "writes_confirmed": "zapisy potwierdzone z ramki pompy w czasie",
}


def _field(test, name):
    """Pole wiersza `tests` (sqlite3.Row albo dict); brak kolumny = None."""
    return test[name] if name in test.keys() else None


# --- definicje w bazie ---

def seed_builtin(conn, now: datetime | None = None) -> None:
    """Upsert wbudowanych definicji (przy każdym starcie — wbudowane są zawsze zgodne z kodem)."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    for key, defn in BUILTIN.items():
        conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES (?, ?, 1, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET def_json = excluded.def_json, builtin = 1, updated = excluded.updated",
                     (key, json.dumps(defn), stamp, stamp))
    conn.commit()


def get_def(conn, key: str) -> dict | None:
    row = conn.execute("SELECT def_json FROM test_defs WHERE key = ?", (key,)).fetchone()
    if row is not None:
        try:
            return json.loads(row["def_json"])
        except ValueError:
            logger.warning("Definicja testu %s ma uszkodzony JSON", key)
    return copy.deepcopy(BUILTIN.get(key))


def list_defs(conn) -> list[dict]:
    """Wbudowane w kolejności `BUILTIN`, potem własne alfabetycznie (brakujące w bazie wbudowane z kodu)."""
    rows: dict[str, dict] = {}
    for row in conn.execute("SELECT key, def_json FROM test_defs ORDER BY key"):
        try:
            rows[row["key"]] = json.loads(row["def_json"])
        except ValueError:
            logger.warning("Definicja testu %s ma uszkodzony JSON — pomijam", row["key"])
    out = [rows.pop(key, None) or copy.deepcopy(BUILTIN[key]) for key in BUILTIN]
    return out + [rows[key] for key in sorted(rows)]


_UNKNOWN_KIND = {"title": "test", "arms": {"A": {"writes": [{"param": "curve_shift", "value": "base"}]}}}


def frozen_def(test) -> dict:
    """Definicja zamrożona w chwili startu (`tests.def_json`); stare wiersze: wbudowana wg `kind`, nieznany `kind` =
    test przesunięcia krzywej (jak legacy)."""
    raw = _field(test, "def_json")
    if raw:
        return json.loads(raw)
    kind = test["kind"]
    return copy.deepcopy(BUILTIN[kind]) if kind in BUILTIN else {**_UNKNOWN_KIND, "key": kind}


def has_writes(defn: dict) -> bool:
    return any((arm or {}).get("writes") for arm in (defn.get("arms") or {}).values())


def writes_target(defn: dict) -> tuple[str | None, str | None]:
    """(`"curve"` | `"p0"` | `"slot52"` | `"dhw"` | None, błąd). None bez błędu = test bez `writes` (same nakładki).
    Jedna nastawa (przesunięcie krzywej, slot52, dhw) albo para P0 razem; mieszanki → błąd."""
    arms = (defn.get("arms") or {}).values()
    per_arm = [{w["param"] for w in (arm or {}).get("writes") or []} for arm in arms]
    used = set().union(*per_arm) if per_arm else set()
    if not used:
        return None, None
    unsupported = sorted(used - set(SUPPORTED_PARAMS))
    if unsupported:
        return None, f"nastawa {unsupported[0]} nie jest jeszcze obsługiwana w testach"
    if not all(per_arm):
        return None, "wszystkie warianty testu muszą zapisywać nastawy (wariant bez zapisu nie wraca do wartości wyjściowej)"
    if used == {"curve_shift"}:
        return "curve", None
    if used in ({"slot52"}, {"dhw"}):
        return next(iter(used)), None
    if used == {"p0_run", "p0_stop"} and all(p == used for p in per_arm):
        return "p0", None
    if used <= {"p0_run", "p0_stop"}:
        return None, "czas pracy i postoju P0 trzeba zapisywać razem w każdym wariancie"
    return None, "test może zapisywać tylko jedną nastawę (przesunięcie krzywej, slot52 albo dhw) albo parę P0"


def describe_measure(defn: dict) -> str:
    primary = (defn.get("verdict") or {}).get("primary")
    return MEASURE_TEXT.get(primary, "wynik w zakładce Testy")


# --- harmonogram ---

def to_steps(plan_steps: list[dict]) -> list[dict]:
    """`Plan.steps` (`{at, param, value}`) → format `tests.schedule`: krzywa `{at, value}`; P0 `{at, value, stop}` (kroki
    z tą samą chwilą łączą się w jeden)."""
    out: list[dict] = []
    p0: dict[str, dict] = {}
    for step in plan_steps:
        param = step["param"]
        if param in ("curve_shift", "slot52", "dhw"):
            out.append({"at": step["at"], "value": step["value"]})
        elif param in ("p0_run", "p0_stop"):
            if step["at"] not in p0:
                p0[step["at"]] = {"at": step["at"], "value": None, "stop": None}
                out.append(p0[step["at"]])
            p0[step["at"]]["value" if param == "p0_run" else "stop"] = step["value"]
        else:
            raise ValueError(f"nastawa {param} nie jest obsługiwana w testach")
    return out


def plan(defn: dict, baseline: dict, now: datetime, is_workday, feed=None) -> tuple[list[dict], dict[str, str]]:
    """(kroki w formacie `tests.schedule`, `{doba: wariant}`)."""
    built = schedule.build(defn, baseline, now, is_workday, feed)
    return to_steps(built.steps), dict(built.arms_by_day)


def _g(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def validate_steps(steps: list[dict], baseline, param: str = "curve_shift") -> str | None:
    """Twarde granice w kodzie, niezależnie od definicji. `baseline`: liczba (`param`) albo para (P0). Zwraca przyczynę
    odrzucenia albo None. Te same komunikaty co dawne `experiments.validate`."""
    if isinstance(baseline, tuple):
        if not steps or (steps[-1].get("value"), steps[-1].get("stop")) != baseline:
            return "harmonogram nie kończy się powrotem do wartości wyjściowej"
        for step in steps:
            pair = (step.get("value"), step.get("stop"))
            if pair not in ex.P0_PAIRS:
                return f"para {_g(pair[0])}/{_g(pair[1])} spoza dozwolonych {', '.join(ex._fmt(p) for p in ex.P0_PAIRS)}"
        return None
    if not steps or steps[-1]["value"] != baseline:
        return "harmonogram nie kończy się powrotem do wartości wyjściowej"
    check, prev = PUMP_PARAMS[param].check, baseline
    for step in steps:
        value = step["value"]
        if not check(prev, value):
            if param != "curve_shift":
                if not check(value, value):
                    return f"wartość {_g(value)} poza dozwolonymi regułami nastawy {param}"
                return f"krok {_g(prev)} → {_g(value)} niedozwolony"
            if not ex.SHIFT_MIN <= value <= ex.SHIFT_MAX:
                return f"wartość {value:g} poza zakresem {ex.SHIFT_MIN}…{ex.SHIFT_MAX}"
            return f"krok {prev:g} → {value:g} większy niż {ex.MAX_STEP}"
        prev = value
    return None


def _windows(defn: dict) -> list[str]:
    return [e["window"] for e in defn.get("measures") or [] if isinstance(e, dict) and e.get("window")]


def day_window(defn: dict) -> str:
    """Okno doby wariantu dla miar: pierwsze `window` z miar, inaczej cała doba lokalna."""
    return next(iter(_windows(defn)), DEFAULT_WINDOW)


def end_at(defn: dict, arms_by_day: dict[str, str]) -> datetime | None:
    """Koniec testu bez `writes`: północ po ostatniej dobie wariantu + godzina końca okna miary przechodzącego przez
    północ (np. 13:00→13:00 = +13 h; przy kilku — najpóźniejszy koniec). None = brak dób."""
    if not arms_by_day:
        return None
    last = date.fromisoformat(max(arms_by_day))
    end = datetime.combine(last + timedelta(days=1), time())
    extra = timedelta(0)
    for window in _windows(defn):
        start, stop = measures._window(last, window)
        if stop.date() > start.date():                                  # przechodzi przez północ
            extra = max(extra, stop - datetime.combine(stop.date(), time()))
    return end + extra


# --- wariant doby i nakładka ---

def arm_for_day(test, day: date) -> str | None:
    arms = json.loads(_field(test, "arms_json") or "{}")
    return arms.get(day.isoformat())


def active_overlay(conn, now: datetime) -> dict | None:
    """`{"overrides": {...}}` dla testu w toku, jeśli dziś jest dobą wariantu z `overrides`; inaczej None."""
    test = ex.running_test(conn)
    if test is None:
        return None
    arm = arm_for_day(test, now.date())
    if arm is None:
        return None
    overrides = ((frozen_def(test).get("arms") or {}).get(arm) or {}).get("overrides")
    return {"overrides": dict(overrides)} if overrides else None


def effective(conn, settings: dict, now: datetime) -> dict:
    """Ustawienia z nałożonymi `overrides` bieżącej doby testu (nowa kopia; `settings` bez zmian)."""
    return overlay.effective_settings(settings, active_overlay(conn, now))


# --- wynik, odznaka, komunikat ---

def _primary(defn: dict) -> str | None:
    return (defn.get("verdict") or {}).get("primary")


def result(conn, settings: dict, test, now: datetime) -> dict:
    defn = frozen_def(test)
    primary = _primary(defn)
    base = compat.zapis_result(conn, test)
    if primary == "zone_spread":
        return {**base, **compat.p0_result(conn, settings, test, now, day_kwh=ex._day_kwh)}
    if primary == "excitation":
        return compat.inertia_result(conn, settings, test, now)
    if primary == "writes_confirmed":
        return base
    arms = json.loads(_field(test, "arms_json") or "{}")
    ctx = Ctx(conn, settings, now, test, day_kwh=ex._day_kwh)
    return {**base, **measures.evaluate(defn, ctx, arm_days_from_plan(arms, day_window(defn)))}


def passed(conn, defn: dict, rows=None) -> dict | None:
    """Ostatni zakończony test tej definicji, jeśli spełnił miarę główną (odznaka w UI). `rows` = wiersze
    `(ended, result)` od najnowszego (domyślnie z bazy)."""
    key, primary = defn["key"], _primary(defn)
    if rows is None:
        rows = conn.execute("SELECT ended, result FROM tests WHERE kind = ? AND status = ? ORDER BY id DESC",
                            (key, ex.DONE)).fetchall()
    for row in rows:
        r = json.loads(row["result"] or "{}")
        if primary == "excitation":
            ok = (r.get("excitation") or {}).get("sufficient")        # miara testu = wymuszenie, nie identyfikacja
        elif primary == "zone_spread":
            ok = r.get("verdict")
        elif primary == "writes_confirmed":
            ok = 0 < r.get("confirmed", 0) == r.get("writes")
        else:
            ok = ((r.get("verdict") or {}) if isinstance(r.get("verdict"), dict) else {}).get("winner")
        if ok:
            out = {"ended": row["ended"], "confirmed": r.get("confirmed"), "writes": r.get("writes")}
            if primary == "zone_spread":
                out["verdict"] = r["verdict"]
            elif primary == "excitation":
                out["identified"] = bool((r.get("refit") or {}).get("identified"))
            elif primary != "writes_confirmed":
                out["winner"] = ok
            return out
    return None


def done_message(defn: dict, test, result: dict) -> str:
    primary = _primary(defn)
    if primary == "zone_spread":
        if result["verdict"] is None:
            return "Test P0 zakończony bez werdyktu (za mało próbek w standby). Szczegóły w zakładce Testy."
        kwh = f", koszt {result['extra_kwh_day']:+g} kWh/dobę" if "extra_kwh_day" in result else ""
        return (f"Test P0 zakończony: werdykt {result['verdict']} (rozrzut pokoi przy 5/5 "
                f"{'niższy' if result['gain_c'] >= 0 else 'wyższy'} o {abs(result['gain_c']):g}°C{kwh}, "
                f"pewność {result['confidence']}). Wynik w zakładce Testy.")
    title = defn.get("title") or defn.get("key") or test["kind"]
    if primary in ("excitation", "writes_confirmed"):
        return f"Test „{title}” zakończony. Wynik w zakładce Testy."
    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    return (f"Test „{title}” zakończony: {verdict.get('winner') or 'brak rozstrzygnięcia'} "
            f"(pewność {verdict.get('confidence') or 'brak'}). Wynik w zakładce Testy.")
