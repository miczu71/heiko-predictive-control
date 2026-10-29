"""Doradca D3a (0.12.0): testy uruchamiane przez usera — JEDYNY moduł pętli Heiko z zapisem do pompy.

Start testu = zgoda usera na cały, z góry pokazany harmonogram; potem add-on sam wykonuje kroki (`tick` co 5 min).
Twarde granice: zapis wyłącznie `number.set_value` na przesunięciu krzywej (zakres ±4, krok ≤ 1) albo — od 0.16.0 —
na parze czas pracy/postoju pompy obiegowej P0 (tylko pary 1/6 i 5/5), jeden test naraz, główny wyłącznik `heiko_enabled`. Zabezpieczenia: potwierdzenie zapisu z ramki pompy (10 min), ręczna zmiana wygrywa
(test przerwany, nic nie nadpisujemy), komfort (najzimniejszy pokój < minimum > 30 min → powrót), restart add-onu
w trakcie testu → powrót do wartości wyjściowej. Niezależny watchdog jest w HA (heartbeat > 45 min → przesunięcie 0) tylko dla przesunięcia krzywej — zawieszone 5/5
to ok. 15 W, bez ryzyka."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from . import catalog, comfort, floor_learn, ha_client, live, summaries, tariff_feed
from .cycle import curve_shift_entity

logger = logging.getLogger(__name__)

ALLOWED_ENTITY_SUFFIX = "heating_curve_parallel_shift"      # zapis tylko na encję przesunięcia krzywej (sprawdzane w _write)
ALLOWED_SERVICE = ("number", "set_value")
SHIFT_MIN, SHIFT_MAX, MAX_STEP = -4, 4, 1
# Pompa obiegowa P0 (test p0_ab): zapis tylko na dwóch encjach i tylko wartości z dwóch dozwolonych par (praca, postój).
# Wartości pojedynczych zapisów (1/5 i 5/6) wynikają z par, więc kolejność zapisu nigdy nie daje postoju < 5 min.
P0_PAIRS = ((1.0, 6.0), (5.0, 5.0))
P0_RUN_SUFFIX, P0_STOP_SUFFIX = "circulation_pump_p0_run_time", "circulation_pump_p0_stop_time"
P0_DAYS = 6                                # A B A B A B, zmiana o północy
P0_MIN_GAIN_C = 0.3                        # 5/5 wygrywa, gdy średni rozrzut stref w standby jest niższy o tyle
P0_AVG_TOLERANCE_C = 0.1                   # „średnia stref nie spada” z tolerancją szumu pomiaru
P0_WEATHER_BAND_C = 3.0                    # różnica średniej temp. zewn. wariantów, powyżej której pewność jest niska
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


def _p0_schedule(baseline: tuple, now: datetime) -> list[dict]:
    """A B A B A B w dobach od najbliższej północy; krok 7 (północ po ostatniej dobie) wraca do wartości wyjściowej."""
    if baseline not in P0_PAIRS:
        return []
    other = next(p for p in P0_PAIRS if p != baseline)
    first = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return [{"at": (first + timedelta(days=i)).isoformat(timespec="minutes"), "value": pair[0], "stop": pair[1]}
            for i, pair in enumerate([baseline, other] * (P0_DAYS // 2) + [baseline])]


def _inertia_windows(day, feed) -> tuple | None:
    """Okna testu na dobę: drogie strefy (`is_peak`) z feedu taryfy; None = feed nie zna doby (okna domyślne)."""
    runs = feed.day_peak_windows(day) if feed is not None else None
    if runs is None:
        return None
    return tuple((a, b - 1) for a, b in runs if b - a >= INERTIA_MIN_WINDOW_H)


def build_schedule(kind: str, baseline, now: datetime, is_workday, feed=None) -> list[dict]:
    """Kroki [{at, value}] od `now` (P0: też `stop`); ostatni krok zawsze wraca do wartości wyjściowej."""
    steps: list[tuple[datetime, float]] = []
    if kind == "p0_ab":
        return _p0_schedule(baseline, now)
    if kind == "zapis":
        delta = MAX_STEP if baseline < SHIFT_MAX else -MAX_STEP
        steps = [(now, baseline + delta), (now + timedelta(minutes=CONFIRM_MIN), baseline)]
    elif kind == "bezwladnosc":
        day, days = now.date(), 0
        while days < INERTIA_DAYS and day <= now.date() + timedelta(days=14):
            windows = _inertia_windows(day, feed)
            if windows is not None or is_workday(day):
                for start_h, end_h in (INERTIA_WINDOWS if windows is None else windows):
                    start = datetime.combine(day, datetime.min.time()).replace(hour=start_h)
                    if start >= now + timedelta(minutes=5):
                        steps += [(start, baseline - 1), (start.replace(hour=end_h), baseline)]
                days += any(at.date() == day for at, _ in steps)
            day += timedelta(days=1)
    return [{"at": at.isoformat(timespec="minutes"), "value": value} for at, value in steps]


def _validate_p0(schedule: list[dict], baseline: tuple) -> str | None:
    if not schedule or (schedule[-1]["value"], schedule[-1].get("stop")) != baseline:
        return "harmonogram nie kończy się powrotem do wartości wyjściowej"
    for step in schedule:
        if (step["value"], step.get("stop")) not in P0_PAIRS:
            return f"para {step['value']:g}/{step.get('stop')} spoza dozwolonych {', '.join(_fmt(p) for p in P0_PAIRS)}"
    return None


def validate(schedule: list[dict], baseline) -> str | None:
    """Twarde granice w kodzie, niezależnie od definicji testu. Zwraca przyczynę odrzucenia albo None."""
    if isinstance(baseline, tuple):
        return _validate_p0(schedule, baseline)
    if not schedule or schedule[-1]["value"] != baseline:
        return "harmonogram nie kończy się powrotem do wartości wyjściowej"
    prev = baseline
    for step in schedule:
        if not SHIFT_MIN <= step["value"] <= SHIFT_MAX:
            return f"wartość {step['value']:g} poza zakresem {SHIFT_MIN}…{SHIFT_MAX}"
        if abs(step["value"] - prev) > MAX_STEP:
            return f"krok {prev:g} → {step['value']:g} większy niż {MAX_STEP}"
        prev = step["value"]
    return None


def _plan(kind: Kind, settings: dict, now: datetime, shift: float | None, curve_on: bool | None, running,
          is_workday, p0: tuple | None = None, d4b_done: bool = True, feed=None) -> tuple[list[dict], str | None]:
    """Harmonogram i ewentualna blokada Startu — to samo dla podglądu w UI i dla samego Startu."""
    is_p0 = kind.target == "p0"
    if not settings.get("heiko_enabled"):
        reason = "zapis do pompy wyłączony (Opcje → „Sterowanie aktywne (Heiko)”)"
    elif running is not None:
        reason = "inny test jest w toku"
    elif is_p0 and p0 is None:
        reason = "nie mogę odczytać czasu pracy i postoju pompy obiegowej P0"
    elif not is_p0 and shift is None:
        reason = "nie mogę odczytać przesunięcia krzywej"
    elif curve_on is None:
        reason = "nie wiem, czy krzywa grzewcza jest włączona (encja przełącznika w Opcjach)"
    elif curve_on != kind.curve_on:
        reason = f"wymaga {'włączonej' if kind.curve_on else 'wyłączonej'} krzywej grzewczej"
    elif is_p0 and not d4b_done:
        reason = "najpierw zakończ test „Bezwładność −1” (D4b)"
    elif is_p0 and p0 not in P0_PAIRS:
        reason = f"P0 ma teraz {_fmt(p0)} — test porównuje tylko {' i '.join(_fmt(p) for p in P0_PAIRS)}"
    else:
        reason = None
    base = (p0 or P0_PAIRS[0]) if is_p0 else (shift if shift is not None else 0.0)
    schedule = build_schedule(kind.key, base, now, is_workday, feed)
    return schedule, reason or validate(schedule, base)


def _read(settings: dict, get_state, get_numeric) -> tuple[float | None, bool | None]:
    curve = (get_state(settings.get("heiko_curve_switch_entity") or "") or {}).get("state")
    return get_numeric(curve_shift_entity(settings)), live._as_bool(curve)


def p0_entities(settings: dict) -> tuple[str, str] | None:
    """(czas pracy, czas postoju) P0 rozwiązane z listy stanów HA po sufiksie z katalogu (prefiks obszaru bywa różny)."""
    ids = catalog.resolve_params(ha_client.get_all_states() or [])
    run, stop = ids.get("p0_run_time"), ids.get("p0_stop_time")
    return (run, stop) if run and stop else None


def _read_p0(settings: dict, get_numeric) -> tuple[float, float] | None:
    entities = p0_entities(settings)
    pair = tuple(get_numeric(e) for e in entities) if entities else None
    return pair if pair and None not in pair else None


def _d4b_done(conn) -> bool:
    return conn.execute("SELECT 1 FROM tests WHERE kind = 'bezwladnosc' AND status = ? LIMIT 1", (DONE,)).fetchone() is not None


def running_test(conn):
    return conn.execute("SELECT * FROM tests WHERE status = ? ORDER BY id DESC LIMIT 1", (RUNNING,)).fetchone()


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
    if isinstance(d["expected"], tuple):               # P0: osobno wartość i postój, żeby UI pokazał „5/5”
        d["expected"], d["expected_stop"] = d["expected"]
        d["baseline_stop"] = d["schedule"][-1]["stop"]
    pending = [w["ts"] for w in _last_writes(conn, row) if w["confirmed_at"] is None] if row["status"] == RUNNING else []
    d["pending_since"] = pending[0] if pending else None
    return d


def _passed(conn, kind: str) -> dict | None:
    """Ostatni zakończony test danego typu, jeśli spełnił swoją miarę wyniku (karta w UI: odznaka, na koniec listy)."""
    for row in conn.execute("SELECT ended, result FROM tests WHERE kind = ? AND status = ? ORDER BY id DESC",
                            (kind, DONE)):
        r = json.loads(row["result"] or "{}")
        if kind == "bezwladnosc":
            ok = (r.get("refit") or {}).get("identified")
        elif kind == "p0_ab":
            ok = r.get("verdict")
        else:
            ok = 0 < r.get("confirmed", 0) == r.get("writes")
        if ok:
            return {"ended": row["ended"], "confirmed": r["confirmed"], "writes": r["writes"],
                    **({"verdict": r["verdict"]} if kind == "p0_ab" else {})}
    return None


def overview(conn, settings: dict, now: datetime, get_state, get_numeric, is_workday) -> dict:
    """Dane zakładki „Testy”: typy testów z podglądem harmonogramu i blokadą, test w toku, historia, dziennik zapisów."""
    shift, curve_on = _read(settings, get_state, get_numeric)
    p0, d4b = _read_p0(settings, get_numeric), _d4b_done(conn)
    running = running_test(conn)
    feed = tariff_feed.for_settings(conn, settings, is_workday)
    kinds = []
    for kind in KINDS.values():
        schedule, blocked = _plan(kind, settings, now, shift, curve_on, running, is_workday, p0, d4b, feed)
        kinds.append({"key": kind.key, "title": kind.title, "description": kind.description, "measure": kind.measure,
                      "schedule": schedule, "blocked": blocked, "passed": _passed(conn, kind.key),
                      "abort_conditions": P0_ABORT_CONDITIONS if kind.target == "p0" else ABORT_CONDITIONS})
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
    kind = KINDS.get(kind_key)
    if kind is None:
        return {"ok": False, "error": "nieznany test"}
    shift, curve_on = _read(settings, get_state, get_numeric)
    p0 = _read_p0(settings, get_numeric) if kind.target == "p0" else None
    with _lock:
        schedule, reason = _plan(kind, settings, now, shift, curve_on, running_test(conn), is_workday, p0, _d4b_done(conn),
                                  tariff_feed.for_settings(conn, settings, is_workday))
        if reason:
            return {"ok": False, "error": reason}
        baseline = p0 if p0 else shift
        cur = conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES (?, ?, ?, ?, ?)",
                           (kind.key, RUNNING, _iso(now), json.dumps(schedule), baseline[0] if p0 else baseline))
        conn.commit()
        logger.info("Test %s (#%d) uruchomiony, wartość wyjściowa %s, %d kroków", kind.key, cur.lastrowid,
                    _fmt(baseline), len(schedule))
        return {"ok": True, "id": cur.lastrowid}


def _curve_ok(old, new) -> bool:
    return SHIFT_MIN <= new <= SHIFT_MAX and abs(new - old) <= MAX_STEP


# Allowlista zapisu: sufiks encji → dopuszczalna zmiana (stary, nowy). Wszystko inne kończy się wyjątkiem.
_WRITE_RULES = {
    ALLOWED_ENTITY_SUFFIX: _curve_ok,
    P0_RUN_SUFFIX: lambda old, new: new in {p[0] for p in P0_PAIRS},
    P0_STOP_SUFFIX: lambda old, new: new in {p[1] for p in P0_PAIRS} and new >= 5,
}


def _write(conn, settings: dict, test_id: int | None, old: float, new: float, now: datetime, writer,
           entity: str | None = None, supersede: bool = True) -> bool:
    """Jedyne miejsce, które wywołuje usługę HA. `entity` domyślnie = przesunięcie krzywej z Opcji."""
    entity = entity or curve_shift_entity(settings)
    rule = next((r for suffix, r in _WRITE_RULES.items() if entity.endswith(suffix)), None)
    if not (entity.startswith("number.") and rule and rule(old, new)):
        raise ValueError(f"zapis {entity} {old} → {new} poza granicami")
    ok = (writer or ha_client.call_service)(*ALLOWED_SERVICE, {"entity_id": entity, "value": new})
    if supersede:
        conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE confirmed_at IS NULL", (SUPERSEDED,))
    conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok) VALUES (?, ?, ?, ?, ?, ?)",
                 (_iso(now), test_id, entity, old, new, int(ok)))
    conn.commit()
    logger.info("Zapis do pompy %s: %s → %s (%s)", entity, old, new, "przyjęty" if ok else "ODRZUCONY")
    return ok


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


def _write_to(conn, settings, test, old, new, now, writer, force: bool = False) -> bool:
    if isinstance(new, tuple):
        return _write_p0(conn, settings, test["id"], old, new, now, writer, force)
    return _write(conn, settings, test["id"], old, new, now, writer)


def _title(test) -> str:
    kind = KINDS.get(test["kind"]) if test is not None else None
    return "Heiko: test pompy obiegowej P0" if kind and kind.target == "p0" else "Heiko: test przesunięcia krzywej"


def _notify(settings: dict, message: str, notify, title: str = "Heiko: test przesunięcia krzywej") -> None:
    (notify or ha_client.notify)(settings.get("advisor_notify_service") or "", title, message)


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
    what = "nastawę P0" if isinstance(baseline, tuple) else "przesunięcie"
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
    """Po starcie add-onu test „w toku” nie jest wznawiany: powrót do wartości wyjściowej."""
    with _lock:
        test = running_test(conn)
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


def _p0_result(conn, settings: dict, test, now: datetime) -> dict:
    """Wynik testu A/B: per wariant (para) średni rozrzut stref w standby, średnia stref, kWh/dobę i temp. zewn.;
    werdykt: 5/5 wygrywa, gdy rozrzut niższy o ≥ P0_MIN_GAIN_C i średnia stref nie spada (z tolerancją szumu)."""
    steps = json.loads(test["schedule"])
    tz = ZoneInfo(settings.get("timezone") or "Europe/Warsaw")
    pooled: dict[str, dict] = {}
    for cur, nxt in zip(steps, steps[1:]):                 # doba = od kroku do następnego (ostatni krok to powrót)
        start, end = datetime.fromisoformat(cur["at"]), datetime.fromisoformat(nxt["at"])
        v = pooled.setdefault(_fmt((cur["value"], cur["stop"])), {"days": 0, "mode": {}, "zones": {}, "n": 0,
                                                                  "kwh": [], "out": []})
        v["days"] += 1
        samples = summaries.zone_samples(conn, settings, int(start.replace(tzinfo=tz).timestamp()),
                                         int(end.replace(tzinfo=tz).timestamp()))
        if samples:
            v["mode"].update(samples[0])
            v["zones"].update(samples[1])
            v["n"] = samples[2]
        kwh = _day_kwh(conn, settings, start.date(), now)
        if kwh is not None:
            v["kwh"].append(kwh)
        out = conn.execute("SELECT AVG(outdoor_temp_c) FROM cycles WHERE loop = 'heiko' AND ts >= ? AND ts < ?",
                           (start.isoformat(), end.isoformat())).fetchone()[0]
        if out is not None:
            v["out"].append(out)
    variants = {}
    for label, v in pooled.items():
        spread = summaries.standby_spread(v["mode"], v["zones"], v["n"]) if v["n"] else None
        variants[label] = {"days": v["days"], "spread_c": spread and spread["spread_c"],
                           "zones_avg_c": spread and spread["zones_avg_c"], "samples": spread["samples"] if spread else 0,
                           "kwh_day": round(mean(v["kwh"]), 2) if v["kwh"] else None,
                           "outdoor_c": round(mean(v["out"]), 1) if v["out"] else None}
    a, b = variants.get("1/6"), variants.get("5/5")
    result: dict = {"variants": variants, "verdict": None, "confidence": None}
    if a and b and a["spread_c"] is not None and b["spread_c"] is not None:
        gain = round(a["spread_c"] - b["spread_c"], 2)
        result["gain_c"] = gain
        result["verdict"] = "5/5" if gain >= P0_MIN_GAIN_C and b["zones_avg_c"] >= a["zones_avg_c"] - P0_AVG_TOLERANCE_C \
            else "1/6"
        if a["kwh_day"] is not None and b["kwh_day"] is not None:
            result["extra_kwh_day"] = round(b["kwh_day"] - a["kwh_day"], 2)
        weather = a["outdoor_c"] is not None and b["outdoor_c"] is not None
        result["confidence"] = "niska" if not weather or abs(a["outdoor_c"] - b["outdoor_c"]) > P0_WEATHER_BAND_C else "wysoka"
        result["outdoor_c"] = round((a["outdoor_c"] + b["outdoor_c"]) / 2, 1) if weather else None
    return result


def _done_message(test, result: dict) -> str:
    if test["kind"] != "p0_ab":
        return f"Test „{KINDS[test['kind']].title}” zakończony. Wynik w zakładce Testy."
    if result["verdict"] is None:
        return "Test P0 zakończony bez werdyktu (za mało próbek w standby). Szczegóły w zakładce Testy."
    kwh = f", koszt {result['extra_kwh_day']:+g} kWh/dobę" if "extra_kwh_day" in result else ""
    return (f"Test P0 zakończony: werdykt {result['verdict']} (rozrzut pokoi przy 5/5 {'niższy' if result['gain_c'] >= 0 else 'wyższy'} o {abs(result['gain_c']):g}°C{kwh}, "
            f"pewność {result['confidence']}). Wynik w zakładce Testy.")


def _result(conn, test, now: datetime, settings: dict) -> dict:
    writes = conn.execute("SELECT ts, confirmed_at FROM pump_writes WHERE test_id = ?", (test["id"],)).fetchall()
    delays = [(datetime.fromisoformat(w["confirmed_at"]) - datetime.fromisoformat(w["ts"])).total_seconds() / 60
              for w in writes if w["confirmed_at"] not in (None, NO_CONFIRMATION, SUPERSEDED)]
    result = {"writes": len(writes), "confirmed": len(delays),
              "max_confirm_min": round(max(delays), 1) if delays else None}
    if test["kind"] == "p0_ab":
        result.update(_p0_result(conn, settings, test, now))
    if test["kind"] == "bezwladnosc":
        rows = conn.execute("SELECT reduced_active, water_setpoint_c, base_curve_c, curve_shift_c FROM cycles "
                            "WHERE loop = 'heiko' AND ts >= ? ORDER BY id", (test["started"],)).fetchall()
        result["excitation"] = floor_learn.excitation(rows)
        refit = floor_learn.refit_from_cycles(conn, settings, now)
        result["refit"] = {k: refit.get(k) for k in ("ok", "reason", "rmse_c", "tau_h", "identified")}
    return result


def _current(kind: Kind | None, settings: dict, get_numeric):
    """Bieżąca nastawa: przesunięcie krzywej albo para P0 (None, gdy któregoś odczytu brak)."""
    if kind is not None and kind.target == "p0":
        return _read_p0(settings, get_numeric)
    return get_numeric(curve_shift_entity(settings))


def tick(conn, settings: dict, now: datetime, get_numeric=ha_client.get_numeric_state,
         writer=None, notify=None) -> dict | None:
    """Jeden krok wykonawcy: potwierdzenia → zmiana ręczna → komfort → następny krok harmonogramu → koniec."""
    with _lock:
        if running_test(conn) is None and conn.execute(
                "SELECT 1 FROM pump_writes WHERE ok = 1 AND confirmed_at IS NULL LIMIT 1").fetchone() is None:
            return None                                # nic do roboty — bez odczytu z HA
        _confirm_writes(conn, settings, get_numeric, now, notify)
        test = running_test(conn)
        if test is None:
            return None
        kind, title = KINDS.get(test["kind"]), _title(test)
        what = "nastawy P0" if kind is not None and kind.target == "p0" else "przesunięcia"
        current = _current(kind, settings, get_numeric)
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
            target = (step["value"], step["stop"]) if "stop" in step else step["value"]
            if target != current and not _write_to(conn, settings, test, current, target, now, writer):
                return _abort(conn, settings, test, "HA odrzuciło zapis", now, writer, notify,
                              force=isinstance(target, tuple))
            conn.execute("UPDATE tests SET step = step + 1 WHERE id = ?", (test["id"],))
            conn.commit()
            return {"status": RUNNING, "step": test["step"] + 1, **({"wrote": step["value"]} if target != current else {})}
        result = _result(conn, test, now, settings)
        _finish(conn, test, DONE, now, result=result)
        _notify(settings, _done_message(test, result), notify, title)
        return {"status": DONE, "result": result}
