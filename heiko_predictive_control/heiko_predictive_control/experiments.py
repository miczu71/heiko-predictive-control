"""Doradca D3a (0.12.0): testy uruchamiane przez usera — JEDYNY moduł pętli Heiko z zapisem do pompy.

Start testu = zgoda usera na cały, z góry pokazany harmonogram; potem add-on sam wykonuje kroki (`tick` co 5 min).
Twarde granice: zapis wyłącznie `number.set_value` na przesunięciu krzywej (zakres ±3, krok ≤ 1) albo — od 0.16.0 —
na parze czas pracy/postoju pompy obiegowej P0 (tylko pary 1/6 i 5/5) albo — H4, poza testami, `write_setting` po akceptacji
propozycji Doradcy — na opóźnieniu grzałki AH (slot 52: 20–60 min, wielokrotność 5, krok ≤ 5), jeden test naraz, główny wyłącznik `heiko_enabled`. Zabezpieczenia: potwierdzenie zapisu z ramki pompy (10 min), ręczna zmiana wygrywa
(test przerwany, nic nie nadpisujemy), komfort (najzimniejszy pokój < minimum > 30 min → powrót), restart add-onu
w trakcie testu → powrót do wartości wyjściowej. Niezależny watchdog jest w HA (heartbeat > 45 min → przesunięcie 0) tylko dla przesunięcia krzywej — zawieszone 5/5
to ok. 15 W, bez ryzyka. Od 0.22.0 `_write_mode` wywołuje też usługę `heiko_heatpump.set_mode` (tylko `dhw`/`auto`) dla `tail_cut`."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import catalog, comfort, db as dbm, floor_learn, ha_client, live, summaries, tariff_feed, vacation
from .cycle import curve_shift_entity

logger = logging.getLogger(__name__)

ALLOWED_ENTITY_SUFFIX = "heating_curve_parallel_shift"      # zapis tylko na encję przesunięcia krzywej (sprawdzane w _write)
ALLOWED_SERVICE = ("number", "set_value")
DHW_SUFFIX = "dhw_setpoint"                  # nastawa CWU (tryb urlopowy); NIE łapie anti_legionella_setpoint (endswith)
DHW_MIN, DHW_MAX = 25, 60
SHIFT_MIN, SHIFT_MAX, MAX_STEP = -3, 3, 1
G13_MAX_STEP = 2 * SHIFT_MAX               # tryb Szczyt G13: skok w całym zakresie ±3 (tylko przesunięcie krzywej, przez `_write`)
# Pompa obiegowa P0 (test p0_ab): zapis tylko na dwóch encjach i tylko wartości z dwóch dozwolonych par (praca, postój).
# Wartości pojedynczych zapisów (1/5 i 5/6) wynikają z par, więc kolejność zapisu nigdy nie daje postoju < 5 min.
P0_PAIRS = ((1.0, 6.0), (5.0, 5.0))
P0_RUN_SUFFIX, P0_STOP_SUFFIX = "circulation_pump_p0_run_time", "circulation_pump_p0_stop_time"
P0_DAYS = 6                                # A B A B A B, zmiana o północy
P0_MIN_GAIN_C = 0.3                        # 5/5 wygrywa, gdy średni rozrzut stref w standby jest niższy o tyle
P0_AVG_TOLERANCE_C = 0.1                   # „średnia stref nie spada” z tolerancją szumu pomiaru
P0_WEATHER_BAND_C = 3.0                    # różnica średniej temp. zewn. wariantów, powyżej której pewność jest niska
MODE_SERVICE = ("heiko_heatpump", "set_mode")   # tail_cut: tryb pracy pompy (zapis tylko przez `_write_mode`)
MODE_CODES = {"dhw": 3, "auto": 4}                # dozwolone tryby → kod `raw_value` sensora mode_setting
MODE_WRITE_ID = "heiko_heatpump.set_mode"         # `pump_writes.entity_id` zapisów trybu
SLOT52_SUFFIX = "backup_source_start_delay"   # H4: opóźnienie grzałki AH (slot 52), zapis tylko po akceptacji propozycji w Doradcy
CONFIRM_MIN = 10
# Integracja heiko_heatpump po `set_value` od razu pokazuje nową wartość (optymistycznie), a odczyt z ramki pompy
# (`Curve_Parallel`, odpytanie co 60 s) zastępuje ją dopiero później — wcześniejsza zgodność nic nie potwierdza.
FRAME_DELAY_MIN = 2
NO_CONFIRMATION = "brak potwierdzenia"
SUPERSEDED = "zastąpiony"                  # niepotwierdzony zapis, po którym przyszedł następny (np. powrót po przerwaniu)
RUNNING, DONE, ABORTED = "w_toku", "zakończony", "przerwany"

INERTIA_WINDOWS = ((6, 12), (15, 21))     # domyślne godziny −1 w dobie roboczej (skok 6 h, powrót 3 h / 9 h) — gdy feed nie zna doby
INERTIA_MIN_WINDOW_H = 2                  # okno z feedu: odcinek drogich godzin [s, e) -> skok o s, powrót o e−1, min. tyle godzin
INERTIA_DAYS = 3

ABORT_CONDITIONS = (
    "przycisk „Przerwij” — powrót do wartości wyjściowej",
    "najzimniejszy pokój poniżej minimum komfortu dłużej niż 30 min — powrót i powiadomienie",
    f"pompa nie potwierdzi zapisu w {CONFIRM_MIN} min — powrót i powiadomienie",
    "ręczna zmiana przesunięcia (panel, HA) — test przerwany, add-on niczego nie nadpisuje",
    "restart add-onu w trakcie testu — powrót do wartości wyjściowej",
    "add-on nie melduje się > 45 min — automatyzacja w HA ustawia przesunięcie 0",
)

P0_ABORT_CONDITIONS = (
    "przycisk „Przerwij” — powrót do wartości wyjściowej",
    "najzimniejszy pokój poniżej minimum komfortu dłużej niż 30 min — powrót i powiadomienie",
    f"pompa nie potwierdzi zapisu w {CONFIRM_MIN} min — powrót i powiadomienie",
    "ręczna zmiana czasu pracy lub postoju P0 (panel, HA) — test przerwany, add-on niczego nie nadpisuje",
    "restart add-onu w trakcie testu — powrót do wartości wyjściowej",
)

SINGLE_TARGETS = {"slot52": "backup_start_delay", "dhw": "dhw_setpoint"}   # cel testu → klucz katalogu encji
TARGET_PARAM = {"curve": "curve_shift", "slot52": "slot52", "dhw": "dhw"}  # cel testu → nazwa nastawy w `PUMP_PARAMS`
TARGET_WHAT = {"curve": "przesunięcia", "p0": "nastawy P0", "slot52": "opóźnienia AH (slot 52)", "dhw": "nastawy CWU"}
TARGET_WHAT_ACC = {"curve": "przesunięcie", "p0": "nastawę P0", "slot52": "opóźnienie AH (slot 52)", "dhw": "nastawę CWU"}

_lock = threading.Lock()                   # tick z harmonogramu i przyciski z UI nie mogą się przeplatać


@dataclass(frozen=True)
class Kind:
    key: str
    title: str
    curve_on: bool                         # wymagany stan krzywej grzewczej
    description: str
    measure: str
    target: str = "curve"                  # co zapisuje test: "curve" (przesunięcie krzywej) albo "p0" (para praca/postój)


KINDS = {k.key: k for k in (
    Kind("zapis", "Test zapisu (poza sezonem)", False,
         "Przesunięcie krzywej +1 na 10 min i powrót. Przy wyłączonej krzywej nie zmienia grzania — sprawdza tylko, "
         "czy zapis z HA dociera do pompy.",
         f"oba zapisy potwierdzone z ramki pompy w ≤ {CONFIRM_MIN} min"),
    Kind("bezwladnosc", "Bezwładność −1", True,
         f"Przesunięcie krzywej −1 w oknach {', '.join(f'{a}–{b}' for a, b in INERTIA_WINDOWS)} przez "
         f"{INERTIA_DAYS} doby robocze, poza oknami powrót. Uczy model, jak szybko dom stygnie i się nagrzewa.",
         f"≥ {floor_learn.EXCITATION_MIN_TRANSITIONS} przejść i średni spadek celu wody ≥ "
         f"{floor_learn.EXCITATION_MIN_DROP_C:g}°C; potem dopasowanie modelu (zidentyfikowany: RMSE ≤ 0,5°C)"),
    Kind("p0_ab", "Pompa obiegowa P0: 1/6 vs 5/5", True,
         f"{P0_DAYS} doby na przemian: czas pracy/postoju P0 1/6 i 5/5 (zmiana o północy, ostatnia doba wraca do wartości "
         "wyjściowej). Mierzy, czy 5/5 wyrównuje temperatury pokoi i ile kosztuje. Zapisuje tylko te dwie pary; "
         "start dopiero po teście „Bezwładność −1”.",
         f"werdykt: 5/5 wygrywa, gdy średni rozrzut stref w standby jest niższy o ≥ {P0_MIN_GAIN_C:g}°C, a średnia stref nie "
         f"spada; wynik z kWh/dobę i pewnością (niska, gdy średnia temp. zewn. wariantów różni się > {P0_WEATHER_BAND_C:g}°C)",
         target="p0"),
)}


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _fmt(value) -> str:
    """Wartość nastawy do komunikatów: przesunięcie „1” albo para P0 „5/5”."""
    return "/".join(f"{v:g}" for v in value) if isinstance(value, tuple) else f"{value:g}"


def _definition(kind) -> dict:
    """Definicja testu: słownik albo dawny `Kind` (→ wbudowana definicja tego samego klucza)."""
    if isinstance(kind, Kind):
        from .testkit.builtin import BUILTIN            # późny import: testkit.* importuje ten moduł
        return BUILTIN[kind.key]
    return kind


def _after_missing(conn, defn: dict) -> str | None:
    """Tytuł pierwszego testu z `requires.after`, który nie ma zakończonego przebiegu (None = wszystkie są)."""
    from .testkit import engine
    for key in (defn.get("requires") or {}).get("after") or []:
        if conn.execute("SELECT 1 FROM tests WHERE kind = ? AND status = ? LIMIT 1", (key, DONE)).fetchone() is None:
            return (engine.get_def(conn, key) or {}).get("title") or key
    return None


def _plan_def(defn: dict, settings: dict, now: datetime, shift: float | None, curve_on: bool | None, running,
              is_workday, p0: tuple | None = None, after_missing: str | None = None, feed=None,
              single: float | None = None):
    """Harmonogram (format `tests.schedule`), doby wariantów i ewentualna blokada Startu — to samo dla podglądu w UI
    i dla samego Startu."""
    from .testkit import engine, schema
    errors = schema.validate(defn)
    if errors:
        return [], {}, f"definicja testu niepoprawna: {errors[0]}"
    target, target_error = engine.writes_target(defn)
    if target_error:
        return [], {}, target_error
    req = defn.get("requires") or {}
    writes = engine.has_writes(defn)
    if req.get("heiko_enabled", True) and not settings.get("heiko_enabled"):
        reason = "zapis do pompy wyłączony (Opcje → „Sterowanie aktywne (Heiko)”)"
    elif settings.get("g13_peak_enabled") and (writes or "g13_peak" not in (defn.get("coexist") or [])):
        reason = "wyłącz tryb Szczyt G13 (Opcje), żeby uruchomić test"
    elif vacation.is_active(settings, now.date()):
        reason = "wyłącz tryb urlopowy (Opcje), żeby uruchomić test"
    elif running is not None:
        reason = "inny test jest w toku"
    elif target == "p0" and p0 is None:
        reason = "nie mogę odczytać czasu pracy i postoju pompy obiegowej P0"
    elif target == "curve" and shift is None:
        reason = "nie mogę odczytać przesunięcia krzywej"
    elif target in SINGLE_TARGETS and single is None:
        reason = f"nie mogę odczytać {TARGET_WHAT[target]}"
    elif req.get("curve_on") is not None and curve_on is None:
        reason = "nie wiem, czy krzywa grzewcza jest włączona (encja przełącznika w Opcjach)"
    elif req.get("curve_on") is not None and curve_on != req["curve_on"]:
        reason = f"wymaga {'włączonej' if req['curve_on'] else 'wyłączonej'} krzywej grzewczej"
    elif after_missing:
        reason = f"najpierw zakończ test „{after_missing}”"
    elif target == "p0" and p0 not in P0_PAIRS:
        reason = f"P0 ma teraz {_fmt(p0)} — test porównuje tylko {' i '.join(_fmt(p) for p in P0_PAIRS)}"
    else:
        reason = None
    if target == "p0":
        base, legacy_base = {"p0_run": (p0 or P0_PAIRS[0])[0], "p0_stop": (p0 or P0_PAIRS[0])[1]}, p0 or P0_PAIRS[0]
    elif target in SINGLE_TARGETS:
        legacy_base = single if single is not None else 0.0
        base = {TARGET_PARAM[target]: legacy_base}
    else:
        legacy_base = shift if shift is not None else 0.0
        base = {"curve_shift": legacy_base} if target == "curve" else {}
    steps, arms = engine.plan(defn, base, now, is_workday, feed)
    if reason is None and target is not None:
        reason = engine.validate_steps(steps, legacy_base, TARGET_PARAM.get(target, "curve_shift"))
    if reason is None and not arms:
        reason = "w najbliższym czasie nie ma doby, w której test mógłby ruszyć"
    return steps, arms, reason


def _plan(kind, settings: dict, now: datetime, shift: float | None, curve_on: bool | None, running,
          is_workday, p0: tuple | None = None, d4b_done: bool = True, feed=None) -> tuple[list[dict], str | None]:
    """Skrót dla `_plan_def`: (harmonogram, blokada). `kind` = definicja albo `Kind`; `d4b_done=False` = nie spełniono
    `requires.after`."""
    defn = _definition(kind)
    missing = None if d4b_done else ((defn.get("requires") or {}).get("after") or ["inny test"])[0]
    steps, _, reason = _plan_def(defn, settings, now, shift, curve_on, running, is_workday, p0, missing, feed)
    return steps, reason


def _read(settings: dict, get_state, get_numeric) -> tuple[float | None, bool | None]:
    curve = (get_state(settings.get("heiko_curve_switch_entity") or "") or {}).get("state")
    return get_numeric(curve_shift_entity(settings)), live._as_bool(curve)


def single_entity(target: str | None, settings: dict) -> str | None:
    """Encja jedynej nastawy testu: przesunięcie krzywej (z Opcji) albo slot52/dhw rozwiązane z listy stanów HA po
    sufiksie z katalogu (None, gdy encji nie ma)."""
    if target == "curve":
        return curve_shift_entity(settings)
    key = SINGLE_TARGETS.get(target)
    return catalog.resolve_params(ha_client.get_all_states() or []).get(key) if key else None


def _read_single(target: str | None, settings: dict, get_numeric) -> float | None:
    entity = single_entity(target, settings)
    return get_numeric(entity) if entity else None


def p0_entities(settings: dict) -> tuple[str, str] | None:
    """(czas pracy, czas postoju) P0 rozwiązane z listy stanów HA po sufiksie z katalogu (prefiks obszaru bywa różny)."""
    ids = catalog.resolve_params(ha_client.get_all_states() or [])
    run, stop = ids.get("p0_run_time"), ids.get("p0_stop_time")
    return (run, stop) if run and stop else None


def _read_p0(settings: dict, get_numeric) -> tuple[float, float] | None:
    entities = p0_entities(settings)
    pair = tuple(get_numeric(e) for e in entities) if entities else None
    return pair if pair and None not in pair else None


def running_test(conn):
    return conn.execute("SELECT * FROM tests WHERE status = ? ORDER BY id DESC LIMIT 1", (RUNNING,)).fetchone()


def writing_test(conn):
    """Test w toku, który zapisuje do pompy (jego zamrożona definicja ma `writes`; brak `def_json` = stary wiersz, tak).
    Test samych nakładek nie wyłącza G13/tail_cut/urlopu i nie jest przerywany restartem."""
    from .testkit import engine
    test = running_test(conn)
    if test is None or test["def_json"] is None:
        return test
    return test if engine.has_writes(engine.frozen_def(test)) else None


def _expected(test):
    """Ostatnia wartość zlecona przez test (krok wykonany = zapisany albo już zgodny); dla P0 para (praca, postój)."""
    schedule = json.loads(test["schedule"])
    step = schedule[test["step"] - 1] if test["step"] else None
    if schedule and "stop" in schedule[-1]:
        return (step["value"], step["stop"]) if step else (test["baseline"], schedule[-1]["stop"])
    return step["value"] if step else test["baseline"]


def _last_writes(conn, test) -> list:
    """Zapisy ostatniego kroku (dla P0 dwa — z tą samą chwilą)."""
    return conn.execute("SELECT * FROM pump_writes WHERE test_id = ? AND ts = (SELECT ts FROM pump_writes "
                        "WHERE test_id = ? ORDER BY id DESC LIMIT 1) ORDER BY id", (test["id"], test["id"])).fetchall()


def _as_dict(conn, row) -> dict:
    d = dict(row)
    d["schedule"] = json.loads(d["schedule"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d["expected"] = _expected(row)
    d["target"] = _target(row)                         # curve | p0 | slot52 | dhw | None — jednostka wartości w UI
    if isinstance(d["expected"], tuple):               # P0: osobno wartość i postój, żeby UI pokazał „5/5”
        d["expected"], d["expected_stop"] = d["expected"]
        d["baseline_stop"] = d["schedule"][-1]["stop"]
    pending = [w["ts"] for w in _last_writes(conn, row) if w["confirmed_at"] is None] if row["status"] == RUNNING else []
    d["pending_since"] = pending[0] if pending else None
    return d


def _inertia_tests(conn):
    """Zakończone testy „Bezwładność” od najnowszego, z rozpakowanym wynikiem."""
    for row in conn.execute("SELECT id, started, ended, result FROM tests WHERE kind = 'bezwladnosc' AND status = ? "
                            "ORDER BY id DESC", (DONE,)):
        yield row, json.loads(row["result"] or "{}")


def last_inertia_start(conn, now: datetime, days: int = 14) -> str | None:
    """`started` ostatniego zakończonego testu „Bezwładność”, jeśli skończył się w ostatnich `days` dniach."""
    for row, _ in _inertia_tests(conn):
        if row["ended"] and datetime.fromisoformat(row["ended"]) >= now - timedelta(days=days):
            return row["started"]
        return None
    return None


def inertia_done_recently(conn, now: datetime, days: int = 30) -> bool:
    """Czy w ostatnich `days` dniach skończył się test „Bezwładność” z dostatecznym wymuszeniem."""
    return any((r.get("excitation") or {}).get("sufficient") and row["ended"]
               and datetime.fromisoformat(row["ended"]) >= now - timedelta(days=days)
               for row, r in _inertia_tests(conn))


def recheck_inertia(conn, settings: dict, now: datetime) -> bool:
    """Jednorazowe przeliczenie ostatniego testu „Bezwładność” na jego oknie (naprawa wyniku sprzed E1):
    wymuszenie wystarczyło, a identyfikacji nie było i nie sprawdzano jej ponownie. Idempotentne (`result.recheck`)."""
    for row, r in _inertia_tests(conn):
        if (not (r.get("excitation") or {}).get("sufficient") or (r.get("refit") or {}).get("identified")
                or r.get("recheck")):
            return False
        previous = dbm.get_setting(conn, "floor_model_state")
        if previous is not None:
            dbm.set_setting(conn, "floor_model_state_before_recheck", previous)
        refit = floor_learn.refit_from_cycles(conn, settings, now, excitation_since=row["started"])
        r["refit"] = {k: refit.get(k) for k in ("ok", "reason", "rmse_c", "tau_h", "identified")}
        r["recheck"] = bool(refit.get("ok"))               # nieudany refit (brak danych) ponawiamy przy następnym starcie
        conn.execute("UPDATE tests SET result = ? WHERE id = ?", (json.dumps(r), row["id"]))
        conn.commit()
        return r["recheck"]
    return False


def _abort_conditions(target: str | None) -> tuple:
    """Warunki przerwania w karcie: dla slot52/dhw z nazwą nastawy, bez watchdoga krzywej (działa tylko dla przesunięcia)."""
    if target not in SINGLE_TARGETS:
        return ABORT_CONDITIONS
    return tuple(c.replace("ręczna zmiana przesunięcia", f"ręczna zmiana {TARGET_WHAT[target]}")
                 for c in ABORT_CONDITIONS if "automatyzacja w HA ustawia przesunięcie 0" not in c)


def overview(conn, settings: dict, now: datetime, get_state, get_numeric, is_workday) -> dict:
    """Dane zakładki „Testy”: definicje testów z podglądem harmonogramu i blokadą, test w toku, historia, dziennik zapisów."""
    from .testkit import engine
    shift, curve_on = _read(settings, get_state, get_numeric)
    p0 = _read_p0(settings, get_numeric)
    running = running_test(conn)
    feed = tariff_feed.for_settings(conn, settings)
    kinds = []
    for defn in engine.list_defs(conn):
        target = engine.writes_target(defn)[0]
        single = _read_single(target, settings, get_numeric) if target in SINGLE_TARGETS else None
        schedule, _, blocked = _plan_def(defn, settings, now, shift, curve_on, running, is_workday, p0,
                                         _after_missing(conn, defn), feed, single)
        key = defn.get("key")
        kind = KINDS.get(key)
        p0_test = target == "p0"
        kinds.append({"key": key, "title": defn.get("title"), "description": defn.get("description", ""),
                      "measure": kind.measure if kind else engine.describe_measure(defn),
                      "schedule": schedule, "blocked": blocked, "passed": engine.passed(conn, defn),
                      "abort_conditions": P0_ABORT_CONDITIONS if p0_test else _abort_conditions(target)})
    kinds.sort(key=lambda k: k["passed"] is not None)          # wykonane na koniec (sortowanie stabilne)
    history = conn.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 20").fetchall()
    writes = conn.execute("SELECT * FROM pump_writes ORDER BY id DESC LIMIT 30").fetchall()
    return {"enabled": bool(settings.get("heiko_enabled")), "shift": shift, "curve_on": curve_on,
            "p0": list(p0) if p0 else None,
            "entity": curve_shift_entity(settings), "kinds": kinds, "abort_conditions": ABORT_CONDITIONS,
            "confirm_min": CONFIRM_MIN, "active": _as_dict(conn, running) if running else None,
            "history": [_as_dict(conn, r) for r in history], "writes": [dict(r) for r in writes]}


def start(conn, settings: dict, kind_key: str, now: datetime, get_state, get_numeric, is_workday) -> dict:
    """Start = zgoda na cały harmonogram. Sam nic nie zapisuje — pierwszy krok wykonuje `tick`."""
    from .testkit import engine
    defn = engine.get_def(conn, kind_key)
    if defn is None:
        return {"ok": False, "error": "nieznany test"}
    shift, curve_on = _read(settings, get_state, get_numeric)
    target = engine.writes_target(defn)[0]
    p0 = _read_p0(settings, get_numeric) if target == "p0" else None
    single = _read_single(target, settings, get_numeric) if target in SINGLE_TARGETS else None
    with _lock:
        schedule, arms, reason = _plan_def(defn, settings, now, shift, curve_on, running_test(conn), is_workday, p0,
                                           _after_missing(conn, defn), tariff_feed.for_settings(conn, settings), single)
        if reason:
            return {"ok": False, "error": reason}
        baseline = p0 if p0 else (single if target in SINGLE_TARGETS else (shift if target == "curve" else 0.0))
        cur = conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline, def_key, def_json, arms_json) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (kind_key, RUNNING, _iso(now), json.dumps(schedule), baseline[0] if p0 else baseline, kind_key,
                            json.dumps(defn), json.dumps(arms)))
        conn.commit()
        logger.info("Test %s (#%d) uruchomiony, wartość wyjściowa %s, %d kroków, %d dób", kind_key, cur.lastrowid,
                    _fmt(baseline), len(schedule), len(arms))
        return {"ok": True, "id": cur.lastrowid}


def _curve_ok(old, new) -> bool:
    return SHIFT_MIN <= new <= SHIFT_MAX and abs(new - old) <= MAX_STEP


# Allowlista zapisu: sufiks encji → dopuszczalna zmiana (stary, nowy). Wszystko inne kończy się wyjątkiem.
_WRITE_RULES = {
    ALLOWED_ENTITY_SUFFIX: _curve_ok,
    P0_RUN_SUFFIX: lambda old, new: new in {p[0] for p in P0_PAIRS},
    P0_STOP_SUFFIX: lambda old, new: new in {p[1] for p in P0_PAIRS} and new >= 5,
    SLOT52_SUFFIX: lambda old, new: 20 <= new <= 60 and new % 5 == 0 and abs(new - old) <= 5,
    DHW_SUFFIX: lambda old, new: DHW_MIN <= new <= DHW_MAX and new == int(new),       # krok całkowity, bez limitu skoku
}


def _write(conn, settings: dict, test_id: int | None, old: float, new: float, now: datetime, writer,
           entity: str | None = None, supersede: bool = True, max_step: float | None = None) -> bool:
    """Jedyne miejsce, które wywołuje usługę HA. `entity` domyślnie = przesunięcie krzywej z Opcji. `max_step` (tryb
    Szczyt G13) zastępuje limit kroku TYLKO dla przesunięcia krzywej; zakres ±3 i pozostałe encje bez zmian."""
    entity = entity or curve_shift_entity(settings)
    rule = next((r for suffix, r in _WRITE_RULES.items() if entity.endswith(suffix)), None)
    if max_step is not None and entity.endswith(ALLOWED_ENTITY_SUFFIX):
        rule = lambda old, new: SHIFT_MIN <= new <= SHIFT_MAX and abs(new - old) <= max_step
    if not (entity.startswith("number.") and rule and rule(old, new)):
        logger.warning("Odrzucony zapis %s: %s → %s (poza granicami)", entity, old, new)
        raise ValueError(f"zapis {entity} {old} → {new} poza granicami")
    ok = (writer or ha_client.call_service)(*ALLOWED_SERVICE, {"entity_id": entity, "value": new})
    if supersede:
        conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE confirmed_at IS NULL", (SUPERSEDED,))
    conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok) VALUES (?, ?, ?, ?, ?, ?)",
                 (_iso(now), test_id, entity, old, new, int(ok)))
    conn.commit()
    logger.info("Zapis do pompy %s: %s → %s (%s)", entity, old, new, "przyjęty" if ok else "ODRZUCONY")
    return ok


def _write_mode(conn, old_code: int | None, mode: str, now: datetime, writer=None) -> tuple[bool, int]:
    """Zapis trybu pracy pompy (`dhw`/`auto`) dla `tail_cut`; drugie (obok `_write`) miejsce z wywołaniem usługi HA.
    Nie unieważnia cudzych niepotwierdzonych wierszy; zwraca (przyjęty, id wiersza `pump_writes`)."""
    if mode not in MODE_CODES:
        logger.warning("Odrzucony zapis trybu pompy: %r (dozwolone: %s)", mode, ", ".join(MODE_CODES))
        raise ValueError(f"tryb pompy {mode!r} poza allowlistą")
    ok = (writer or ha_client.call_service)(*MODE_SERVICE, {"mode": mode})
    cur = conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok) VALUES (?, NULL, ?, ?, ?, ?)",
                       (_iso(now), MODE_WRITE_ID, old_code, MODE_CODES[mode], int(ok)))
    conn.commit()
    logger.info("Zapis trybu pompy %s: %s → %s (%s)", MODE_WRITE_ID, old_code, mode, "przyjęty" if ok else "ODRZUCONY")
    return bool(ok), cur.lastrowid


def write_setting(conn, entity: str, old: float, new: float, now: datetime, writer=None) -> bool:
    """Zapis jednej nastawy z allowlisty po akceptacji propozycji Doradcy (H4: slot 52), bez testu. `ValueError` przy
    złamaniu allowlisty nie jest tu łapany. Trwający test blokuje zapis (`RuntimeError`), a `supersede=False` nie
    unieważnia niepotwierdzonych zapisów testu. Potwierdzenie z ramki robi `tick` → `_confirm_writes`."""
    if running_test(conn) is not None:
        raise RuntimeError("trwa test — zatwierdź zmianę slotu 52 po jego zakończeniu")
    return _write(conn, {}, None, old, new, now, writer, entity=entity, supersede=False)


def _write_p0(conn, settings: dict, test_id: int | None, old: tuple, new: tuple, now: datetime, writer,
              force: bool = False) -> bool:
    """Para (praca, postój) jako dwa zapisy w jednej chwili. Pary spoza allowlisty odrzuca już `validate`, a każdy
    pojedynczy zapis sprawdza `_write`. `force` zapisuje też encje, które wyglądają na już zgodne (po nieudanym kroku)."""
    entities = p0_entities(settings)
    if entities is None:
        raise ValueError("nie znajduję encji P0 w HA")
    conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE confirmed_at IS NULL", (SUPERSEDED,))
    return all([_write(conn, settings, test_id, o, n, now, writer, entity=e, supersede=False)
                for e, o, n in zip(entities, old, new) if force or o != n])


def _target(test) -> str | None:
    from .testkit import engine
    return engine.writes_target(engine.frozen_def(test))[0]


def _write_to(conn, settings, test, old, new, now, writer, force: bool = False) -> bool:
    if isinstance(new, tuple):
        return _write_p0(conn, settings, test["id"], old, new, now, writer, force)
    target = _target(test)
    if target in SINGLE_TARGETS:
        entity = single_entity(target, settings)
        if entity is None:
            raise ValueError(f"nie znajduję encji {TARGET_WHAT[target]} w HA")
        return _write(conn, settings, test["id"], old, new, now, writer, entity=entity)
    return _write(conn, settings, test["id"], old, new, now, writer)


def _title(test) -> str:
    if test is None:
        return "Heiko: test przesunięcia krzywej"
    kind = KINDS.get(test["kind"])
    if kind is not None:
        return "Heiko: test pompy obiegowej P0" if kind.target == "p0" else "Heiko: test przesunięcia krzywej"
    from .testkit import engine
    target = engine.writes_target(engine.frozen_def(test))[0]
    return {"p0": "Heiko: test pompy obiegowej P0", "curve": "Heiko: test przesunięcia krzywej",
            "slot52": "Heiko: test opóźnienia grzałki AH", "dhw": "Heiko: test nastawy CWU"}.get(
        target, "Heiko: test nakładki ustawień")


def _notify(settings: dict, message: str, notify, title: str = "Heiko: test przesunięcia krzywej") -> None:
    service = settings.get("advisor_notify_service") or ""
    if not service:
        logger.warning("%s: brak usługi powiadomień (advisor_notify_service) — pominięto: %s", title, message)
    (notify or ha_client.notify)(service, title, message)


def _finish(conn, test, status: str, now: datetime, reason: str | None = None, result: dict | None = None) -> None:
    conn.execute("UPDATE tests SET status = ?, ended = ?, abort_reason = ?, result = ? WHERE id = ?",
                 (status, _iso(now), reason, json.dumps(result) if result else None, test["id"]))
    conn.commit()


def _baseline(test):
    expected = _expected(test)
    return (test["baseline"], json.loads(test["schedule"])[-1]["stop"]) if isinstance(expected, tuple) else test["baseline"]


def _abort(conn, settings: dict, test, reason: str, now: datetime, writer, notify, force: bool = False) -> dict:
    """Przerwanie z powrotem do wartości wyjściowej (zmiana ręczna idzie osobną ścieżką w `tick` — bez zapisu)."""
    restored, expected, baseline = None, _expected(test), _baseline(test)
    what = TARGET_WHAT_ACC.get(_target(test), "przesunięcie")
    if expected != baseline or force:
        try:
            restored = _write_to(conn, settings, test, expected, baseline, now, writer, force)
        except ValueError as exc:                          # np. brak encji P0 przy starcie HA — test i tak musi się zakończyć
            logger.error("Przywrócenie wartości wyjściowej niemożliwe: %s", exc)
            restored = False
    _finish(conn, test, ABORTED, now, reason)
    msg = f"Test przerwany: {reason}."
    if restored is not None:
        msg += (f" Przywracam {what} {_fmt(baseline)}." if restored
                else f" NIE udało się przywrócić ({what} {_fmt(baseline)}) — sprawdź pompę.")
    _notify(settings, msg, notify, _title(test))
    logger.warning("Test #%d przerwany: %s", test["id"], reason)
    return {"ok": True, "status": ABORTED, "reason": reason}


def abort(conn, settings: dict, now: datetime, reason: str = "przerwany przez użytkownika",
          writer=None, notify=None) -> dict:
    with _lock:
        test = running_test(conn)
        if test is None:
            return {"ok": False, "error": "żaden test nie jest w toku"}
        return _abort(conn, settings, test, reason, now, writer, notify)


def recover_on_start(conn, settings: dict, now: datetime, writer=None, notify=None) -> dict | None:
    """Po starcie add-onu test z zapisami nie jest wznawiany: powrót do wartości wyjściowej. Test samych nakładek nic
    nie zapisuje do pompy, więc wznawia się z bazy."""
    with _lock:
        test = writing_test(conn)
        return _abort(conn, settings, test, "restart add-onu", now, writer, notify) if test else None


def _confirm_writes(conn, settings: dict, get_numeric, now: datetime, notify) -> None:
    """Odczyt kontrolny niepotwierdzonych zapisów (jedyny zegar potwierdzenia — także dla powrotów po przerwaniu)."""
    deadline = _iso(now - timedelta(minutes=CONFIRM_MIN))
    after_frame = _iso(now - timedelta(minutes=FRAME_DELAY_MIN))
    current: dict[str, float | None] = {}
    for w in conn.execute("SELECT w.*, t.status AS test_status FROM pump_writes w LEFT JOIN tests t ON t.id = w.test_id "
                          "WHERE w.ok = 1 AND w.confirmed_at IS NULL").fetchall():
        value = current.setdefault(w["entity_id"], get_numeric(w["entity_id"]))
        if value is not None and value == w["new"] and w["ts"] <= after_frame:
            conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE id = ?", (_iso(now), w["id"]))
        elif w["ts"] < deadline:
            conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE id = ?", (NO_CONFIRMATION, w["id"]))
            if w["test_status"] != RUNNING:            # test w toku przerwie `tick` (z własnym powiadomieniem)
                if w["entity_id"].endswith(SLOT52_SUFFIX):
                    _notify(settings, f"Pompa nie potwierdziła zapisu opóźnienia grzałki (slot 52) = {w['new']:g} — sprawdź ręcznie.",
                            notify, "Heiko Doradca: zapis slotu 52")
                    continue
                if w["test_id"] is None and w["entity_id"].endswith(ALLOWED_ENTITY_SUFFIX):
                    _notify(settings, f"Pompa nie potwierdziła zapisu przesunięcia {w['new']:g} (tryb Szczyt G13) — sprawdź ręcznie.",
                            notify, _title(None))
                    continue
                if w["entity_id"].endswith(DHW_SUFFIX):
                    _notify(settings, f"Pompa nie potwierdziła zapisu nastawy CWU = {w['new']:g} — sprawdź ręcznie.",
                            notify, "Heiko: tryb urlopowy")
                    continue
                what = "przesunięcia" if w["entity_id"].endswith(ALLOWED_ENTITY_SUFFIX) else f"{w['entity_id']} ="
                _notify(settings, f"Pompa nie potwierdziła przywrócenia {what} {w['new']:g} — sprawdź ręcznie.", notify,
                        _title(None) if what == "przesunięcia" else "Heiko: test pompy obiegowej P0")
    conn.commit()


def _day_kwh(conn, settings: dict, day, now: datetime) -> float | None:
    """Energia pompy z doby (streszczenie `energy`); brakującą dobę zamkniętą dociąga z historii HA (tylko odczyt)."""
    def stored():
        row = conn.execute("SELECT data FROM daily_summary WHERE day = ? AND topic = 'energy'", (day.isoformat(),)).fetchone()
        return json.loads(row["data"]).get("kwh") if row else None
    if stored() is None and day < now.date():
        try:
            summaries.store_day(conn, settings, day, ha_client.get_all_states() or [], now)
        except Exception:                                  # brak HA nie psuje wyniku — kWh będzie puste
            logger.warning("Nie udało się dociągnąć energii z %s", day, exc_info=True)
    return stored()


def _current(target: str | None, settings: dict, get_numeric):
    """Bieżąca nastawa: przesunięcie krzywej albo para P0 (None, gdy któregoś odczytu brak)."""
    if target == "p0":
        return _read_p0(settings, get_numeric)
    if target in SINGLE_TARGETS:
        return _read_single(target, settings, get_numeric)
    return get_numeric(curve_shift_entity(settings))


def _complete(conn, settings: dict, test, defn: dict, now: datetime, notify, title: str) -> dict:
    from .testkit import engine
    try:
        result = engine.result(conn, settings, test, now)
    except Exception as exc:                      # wynik nie do policzenia nie może zostawić testu w RUNNING
        logger.exception("Test #%s: nie udało się policzyć wyniku", test["id"])
        _finish(conn, test, ABORTED, now, reason=f"błąd obliczenia wyniku: {exc}")
        return {"status": ABORTED, "reason": f"błąd obliczenia wyniku: {exc}"}
    _finish(conn, test, DONE, now, result=result)
    _notify(settings, engine.done_message(defn, test, result), notify, title)
    return {"status": DONE, "result": result}


def _tick_overlay(conn, settings: dict, test, defn: dict, now: datetime, notify) -> dict:
    """Test bez `writes` (same nakładki): nic nie czyta z pompy i nic nie zapisuje; kończy się po `engine.end_at`."""
    from .testkit import engine
    arms = json.loads(test["arms_json"] or "{}")
    end = engine.end_at(defn, arms)
    if end is None or now >= end:
        return _complete(conn, settings, test, defn, now, notify, _title(test))
    return {"status": RUNNING, "arm": engine.arm_for_day(test, now.date()), "until": _iso(end)}


def tick(conn, settings: dict, now: datetime, get_numeric=ha_client.get_numeric_state,
         writer=None, notify=None) -> dict | None:
    """Jeden krok wykonawcy: potwierdzenia → zmiana ręczna → komfort → następny krok harmonogramu → koniec."""
    from .testkit import engine
    with _lock:
        if running_test(conn) is None and conn.execute(
                "SELECT 1 FROM pump_writes WHERE ok = 1 AND confirmed_at IS NULL LIMIT 1").fetchone() is None:
            return None                                # nic do roboty — bez odczytu z HA
        _confirm_writes(conn, settings, get_numeric, now, notify)
        test = running_test(conn)
        if test is None:
            return None
        defn, title = engine.frozen_def(test), _title(test)
        if not engine.has_writes(defn):
            return _tick_overlay(conn, settings, test, defn, now, notify)
        target = engine.writes_target(defn)[0]
        what = TARGET_WHAT.get(target, "przesunięcia")
        current = _current(target, settings, get_numeric)
        expected, last = _expected(test), _last_writes(conn, test)
        if any(w["confirmed_at"] == NO_CONFIRMATION for w in last):
            return _abort(conn, settings, test, f"pompa nie potwierdziła zapisu {_fmt(expected)} w {CONFIRM_MIN} min",
                          now, writer, notify)
        if any(w["confirmed_at"] is None for w in last):
            return {"status": RUNNING, "waiting": "potwierdzenie zapisu"}
        if current is None:
            logger.warning("Test #%d: brak odczytu %s — czekam", test["id"], what)
            return {"status": RUNNING, "waiting": f"odczyt {what}"}
        if current != expected:
            _finish(conn, test, ABORTED, now, f"ręczna zmiana {what} na {_fmt(current)} (oczekiwane {_fmt(expected)})")
            _notify(settings, f"Test przerwany ręcznie: {what} zmieniono na {_fmt(current)}. Add-on niczego nie nadpisuje.",
                    notify, title)
            return {"status": ABORTED, "reason": "ręczna zmiana"}
        minimum = comfort.room_min(settings)
        if comfort.sustained_cold(comfort.recent_rows(conn, now), minimum):
            return _abort(conn, settings, test, f"najzimniejszy pokój poniżej {minimum:g}°C dłużej niż "
                          f"{comfort.ALARM_MINUTES} min", now, writer, notify)
        schedule = json.loads(test["schedule"])
        if test["step"] < len(schedule):
            step = schedule[test["step"]]
            if now < datetime.fromisoformat(step["at"]):
                return {"status": RUNNING, "next": step}
            target_value = (step["value"], step["stop"]) if "stop" in step else step["value"]
            if target_value != current and not _write_to(conn, settings, test, current, target_value, now, writer):
                return _abort(conn, settings, test, "HA odrzuciło zapis", now, writer, notify,
                              force=isinstance(target_value, tuple))
            conn.execute("UPDATE tests SET step = step + 1 WHERE id = ?", (test["id"],))
            conn.commit()
            return {"status": RUNNING, "step": test["step"] + 1,
                    **({"wrote": step["value"]} if target_value != current else {})}
        return _complete(conn, settings, test, defn, now, notify, title)
