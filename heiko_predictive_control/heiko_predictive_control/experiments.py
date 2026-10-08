"""Doradca D3a (0.12.0): testy uruchamiane przez usera — JEDYNY moduł pętli Heiko z zapisem do pompy.

Start testu = zgoda usera na cały, z góry pokazany harmonogram; potem add-on sam wykonuje kroki (`tick` co 5 min).
Twarde granice: zapis wyłącznie `number.set_value` na przesunięciu krzywej, zakres ±4, krok ≤ 1, jeden test naraz,
główny wyłącznik `heiko_enabled`. Zabezpieczenia: potwierdzenie zapisu z ramki pompy (10 min), ręczna zmiana wygrywa
(test przerwany, nic nie nadpisujemy), komfort (najzimniejszy pokój < minimum > 30 min → powrót), restart add-onu
w trakcie testu → powrót do wartości wyjściowej. Niezależny watchdog jest w HA (heartbeat > 45 min → przesunięcie 0)."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import comfort, floor_learn, ha_client, live
from .cycle import curve_shift_entity

logger = logging.getLogger(__name__)

ALLOWED_ENTITY_SUFFIX = "heating_curve_parallel_shift"      # zapis tylko na encję przesunięcia krzywej (sprawdzane w _write)
ALLOWED_SERVICE = ("number", "set_value")
SHIFT_MIN, SHIFT_MAX, MAX_STEP = -4, 4, 1
CONFIRM_MIN = 10
# Integracja heiko_heatpump po `set_value` od razu pokazuje nową wartość (optymistycznie), a odczyt z ramki pompy
# (`Curve_Parallel`, odpytanie co 60 s) zastępuje ją dopiero później — wcześniejsza zgodność nic nie potwierdza.
FRAME_DELAY_MIN = 2
NO_CONFIRMATION = "brak potwierdzenia"
SUPERSEDED = "zastąpiony"                  # niepotwierdzony zapis, po którym przyszedł następny (np. powrót po przerwaniu)
RUNNING, DONE, ABORTED = "w_toku", "zakończony", "przerwany"

INERTIA_WINDOWS = ((6, 12), (15, 21))     # godziny −1 w dobie roboczej (skok 6 h, powrót 3 h / 9 h)
INERTIA_DAYS = 3

ABORT_CONDITIONS = (
    "przycisk „Przerwij” — powrót do wartości wyjściowej",
    "najzimniejszy pokój poniżej minimum komfortu dłużej niż 30 min — powrót i powiadomienie",
    f"pompa nie potwierdzi zapisu w {CONFIRM_MIN} min — powrót i powiadomienie",
    "ręczna zmiana przesunięcia (panel, HA) — test przerwany, add-on niczego nie nadpisuje",
    "restart add-onu w trakcie testu — powrót do wartości wyjściowej",
    "add-on nie melduje się > 45 min — automatyzacja w HA ustawia przesunięcie 0",
)

_lock = threading.Lock()                   # tick z harmonogramu i przyciski z UI nie mogą się przeplatać


@dataclass(frozen=True)
class Kind:
    key: str
    title: str
    curve_on: bool                         # wymagany stan krzywej grzewczej
    description: str
    measure: str


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
)}


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def build_schedule(kind: str, baseline: float, now: datetime, is_workday) -> list[dict]:
    """Kroki [{at, value}] od `now`; ostatni krok zawsze wraca do wartości wyjściowej."""
    steps: list[tuple[datetime, float]] = []
    if kind == "zapis":
        delta = MAX_STEP if baseline < SHIFT_MAX else -MAX_STEP
        steps = [(now, baseline + delta), (now + timedelta(minutes=CONFIRM_MIN), baseline)]
    elif kind == "bezwladnosc":
        day, days = now.date(), 0
        while days < INERTIA_DAYS and day <= now.date() + timedelta(days=14):
            if is_workday(day):
                for start_h, end_h in INERTIA_WINDOWS:
                    start = datetime.combine(day, datetime.min.time()).replace(hour=start_h)
                    if start >= now + timedelta(minutes=5):
                        steps += [(start, baseline - 1), (start.replace(hour=end_h), baseline)]
                days += any(at.date() == day for at, _ in steps)
            day += timedelta(days=1)
    return [{"at": at.isoformat(timespec="minutes"), "value": value} for at, value in steps]


def validate(schedule: list[dict], baseline: float) -> str | None:
    """Twarde granice w kodzie, niezależnie od definicji testu. Zwraca przyczynę odrzucenia albo None."""
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
          is_workday) -> tuple[list[dict], str | None]:
    """Harmonogram i ewentualna blokada Startu — to samo dla podglądu w UI i dla samego Startu."""
    if not settings.get("heiko_enabled"):
        reason = "zapis do pompy wyłączony (Opcje → „Sterowanie aktywne (Heiko)”)"
    elif running is not None:
        reason = "inny test jest w toku"
    elif shift is None:
        reason = "nie mogę odczytać przesunięcia krzywej"
    elif curve_on is None:
        reason = "nie wiem, czy krzywa grzewcza jest włączona (encja przełącznika w Opcjach)"
    elif curve_on != kind.curve_on:
        reason = f"wymaga {'włączonej' if kind.curve_on else 'wyłączonej'} krzywej grzewczej"
    else:
        reason = None
    base = shift if shift is not None else 0.0
    schedule = build_schedule(kind.key, base, now, is_workday)
    return schedule, reason or validate(schedule, base)


def _read(settings: dict, get_state, get_numeric) -> tuple[float | None, bool | None]:
    curve = (get_state(settings.get("heiko_curve_switch_entity") or "") or {}).get("state")
    return get_numeric(curve_shift_entity(settings)), live._as_bool(curve)


def running_test(conn):
    return conn.execute("SELECT * FROM tests WHERE status = ? ORDER BY id DESC LIMIT 1", (RUNNING,)).fetchone()


def _expected(test) -> float:
    """Ostatnia wartość zlecona przez test (krok wykonany = zapisany albo już zgodny)."""
    return json.loads(test["schedule"])[test["step"] - 1]["value"] if test["step"] else test["baseline"]


def _last_write(conn, test):
    return conn.execute("SELECT * FROM pump_writes WHERE test_id = ? ORDER BY id DESC LIMIT 1", (test["id"],)).fetchone()


def _as_dict(conn, row) -> dict:
    d = dict(row)
    d["schedule"] = json.loads(d["schedule"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d["expected"] = _expected(row)
    last = _last_write(conn, row) if row["status"] == RUNNING else None
    d["pending_since"] = last["ts"] if last is not None and last["confirmed_at"] is None else None
    return d


def _passed(conn, kind: str) -> dict | None:
    """Ostatni zakończony test danego typu, jeśli spełnił swoją miarę wyniku (karta w UI: odznaka, na koniec listy)."""
    for row in conn.execute("SELECT ended, result FROM tests WHERE kind = ? AND status = ? ORDER BY id DESC",
                            (kind, DONE)):
        r = json.loads(row["result"] or "{}")
        ok = (r.get("refit") or {}).get("identified") if kind == "bezwladnosc" else 0 < r.get("confirmed", 0) == r.get("writes")
        if ok:
            return {"ended": row["ended"], "confirmed": r["confirmed"], "writes": r["writes"]}
    return None


def overview(conn, settings: dict, now: datetime, get_state, get_numeric, is_workday) -> dict:
    """Dane zakładki „Testy”: typy testów z podglądem harmonogramu i blokadą, test w toku, historia, dziennik zapisów."""
    shift, curve_on = _read(settings, get_state, get_numeric)
    running = running_test(conn)
    kinds = []
    for kind in KINDS.values():
        schedule, blocked = _plan(kind, settings, now, shift, curve_on, running, is_workday)
        kinds.append({"key": kind.key, "title": kind.title, "description": kind.description, "measure": kind.measure,
                      "schedule": schedule, "blocked": blocked, "passed": _passed(conn, kind.key)})
    kinds.sort(key=lambda k: k["passed"] is not None)          # wykonane na koniec (sortowanie stabilne)
    history = conn.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 20").fetchall()
    writes = conn.execute("SELECT * FROM pump_writes ORDER BY id DESC LIMIT 30").fetchall()
    return {"enabled": bool(settings.get("heiko_enabled")), "shift": shift, "curve_on": curve_on,
            "entity": curve_shift_entity(settings), "kinds": kinds, "abort_conditions": ABORT_CONDITIONS,
            "confirm_min": CONFIRM_MIN, "active": _as_dict(conn, running) if running else None,
            "history": [_as_dict(conn, r) for r in history], "writes": [dict(r) for r in writes]}


def start(conn, settings: dict, kind_key: str, now: datetime, get_state, get_numeric, is_workday) -> dict:
    """Start = zgoda na cały harmonogram. Sam nic nie zapisuje — pierwszy krok wykonuje `tick`."""
    kind = KINDS.get(kind_key)
    if kind is None:
        return {"ok": False, "error": "nieznany test"}
    shift, curve_on = _read(settings, get_state, get_numeric)
    with _lock:
        schedule, reason = _plan(kind, settings, now, shift, curve_on, running_test(conn), is_workday)
        if reason:
            return {"ok": False, "error": reason}
        cur = conn.execute("INSERT INTO tests (kind, status, started, schedule, baseline) VALUES (?, ?, ?, ?, ?)",
                           (kind.key, RUNNING, _iso(now), json.dumps(schedule), shift))
        conn.commit()
        logger.info("Test %s (#%d) uruchomiony, wartość wyjściowa %s, %d kroków", kind.key, cur.lastrowid, shift,
                    len(schedule))
        return {"ok": True, "id": cur.lastrowid}


def _write(conn, settings: dict, test_id: int | None, old: float, new: float, now: datetime, writer) -> bool:
    entity = curve_shift_entity(settings)
    if not (entity.startswith("number.") and entity.endswith(ALLOWED_ENTITY_SUFFIX)
            and SHIFT_MIN <= new <= SHIFT_MAX and abs(new - old) <= MAX_STEP):
        raise ValueError(f"zapis {entity} {old} → {new} poza granicami")
    ok = (writer or ha_client.call_service)(*ALLOWED_SERVICE, {"entity_id": entity, "value": new})
    conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE confirmed_at IS NULL", (SUPERSEDED,))
    conn.execute("INSERT INTO pump_writes (ts, test_id, entity_id, old, new, ok) VALUES (?, ?, ?, ?, ?, ?)",
                 (_iso(now), test_id, entity, old, new, int(ok)))
    conn.commit()
    logger.info("Zapis do pompy %s: %s → %s (%s)", entity, old, new, "przyjęty" if ok else "ODRZUCONY")
    return ok


def _notify(settings: dict, message: str, notify) -> None:
    (notify or ha_client.notify)(settings.get("advisor_notify_service") or "", "Heiko: test przesunięcia krzywej", message)


def _finish(conn, test, status: str, now: datetime, reason: str | None = None, result: dict | None = None) -> None:
    conn.execute("UPDATE tests SET status = ?, ended = ?, abort_reason = ?, result = ? WHERE id = ?",
                 (status, _iso(now), reason, json.dumps(result) if result else None, test["id"]))
    conn.commit()


def _abort(conn, settings: dict, test, reason: str, now: datetime, writer, notify) -> dict:
    """Przerwanie z powrotem do wartości wyjściowej (zmiana ręczna idzie osobną ścieżką w `tick` — bez zapisu)."""
    restored, expected = None, _expected(test)
    if expected != test["baseline"]:
        restored = _write(conn, settings, test["id"], expected, test["baseline"], now, writer)
    _finish(conn, test, ABORTED, now, reason)
    msg = f"Test przerwany: {reason}."
    if restored is not None:
        msg += (f" Przywracam przesunięcie {test['baseline']:g}." if restored
                else f" NIE udało się przywrócić przesunięcia {test['baseline']:g} — sprawdź pompę.")
    _notify(settings, msg, notify)
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


def _confirm_writes(conn, settings: dict, current: float | None, now: datetime, notify) -> None:
    """Odczyt kontrolny niepotwierdzonych zapisów (jedyny zegar potwierdzenia — także dla powrotów po przerwaniu)."""
    deadline = _iso(now - timedelta(minutes=CONFIRM_MIN))
    after_frame = _iso(now - timedelta(minutes=FRAME_DELAY_MIN))
    for w in conn.execute("SELECT w.*, t.status AS test_status FROM pump_writes w LEFT JOIN tests t ON t.id = w.test_id "
                          "WHERE w.ok = 1 AND w.confirmed_at IS NULL").fetchall():
        if current is not None and current == w["new"] and w["ts"] <= after_frame:
            conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE id = ?", (_iso(now), w["id"]))
        elif w["ts"] < deadline:
            conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE id = ?", (NO_CONFIRMATION, w["id"]))
            if w["test_status"] != RUNNING:            # test w toku przerwie `tick` (z własnym powiadomieniem)
                _notify(settings, f"Pompa nie potwierdziła przywrócenia przesunięcia {w['new']:g} — sprawdź ręcznie.", notify)
    conn.commit()


def _result(conn, test, now: datetime, settings: dict) -> dict:
    writes = conn.execute("SELECT ts, confirmed_at FROM pump_writes WHERE test_id = ?", (test["id"],)).fetchall()
    delays = [(datetime.fromisoformat(w["confirmed_at"]) - datetime.fromisoformat(w["ts"])).total_seconds() / 60
              for w in writes if w["confirmed_at"] not in (None, NO_CONFIRMATION, SUPERSEDED)]
    result = {"writes": len(writes), "confirmed": len(delays),
              "max_confirm_min": round(max(delays), 1) if delays else None}
    if test["kind"] == "bezwladnosc":
        rows = conn.execute("SELECT reduced_active, water_setpoint_c, base_curve_c, curve_shift_c FROM cycles "
                            "WHERE loop = 'heiko' AND ts >= ? ORDER BY id", (test["started"],)).fetchall()
        result["excitation"] = floor_learn.excitation(rows)
        refit = floor_learn.refit_from_cycles(conn, settings, now)
        result["refit"] = {k: refit.get(k) for k in ("ok", "reason", "rmse_c", "tau_h", "identified")}
    return result


def tick(conn, settings: dict, now: datetime, get_numeric=ha_client.get_numeric_state,
         writer=None, notify=None) -> dict | None:
    """Jeden krok wykonawcy: potwierdzenia → zmiana ręczna → komfort → następny krok harmonogramu → koniec."""
    with _lock:
        if running_test(conn) is None and conn.execute(
                "SELECT 1 FROM pump_writes WHERE ok = 1 AND confirmed_at IS NULL LIMIT 1").fetchone() is None:
            return None                                # nic do roboty — bez odczytu z HA
        current = get_numeric(curve_shift_entity(settings))
        _confirm_writes(conn, settings, current, now, notify)
        test = running_test(conn)
        if test is None:
            return None
        expected, last = _expected(test), _last_write(conn, test)
        if last is not None and last["confirmed_at"] == NO_CONFIRMATION:
            return _abort(conn, settings, test, f"pompa nie potwierdziła zapisu {last['new']:g} w {CONFIRM_MIN} min",
                          now, writer, notify)
        if last is not None and last["confirmed_at"] is None:
            return {"status": RUNNING, "waiting": "potwierdzenie zapisu"}
        if current is None:
            logger.warning("Test #%d: brak odczytu przesunięcia — czekam", test["id"])
            return {"status": RUNNING, "waiting": "odczyt przesunięcia"}
        if current != expected:
            _finish(conn, test, ABORTED, now, f"ręczna zmiana przesunięcia na {current:g} (oczekiwane {expected:g})")
            _notify(settings, f"Test przerwany ręcznie: przesunięcie zmieniono na {current:g}. Add-on niczego nie nadpisuje.",
                    notify)
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
            if step["value"] != current and not _write(conn, settings, test["id"], current, step["value"], now, writer):
                return _abort(conn, settings, test, "HA odrzuciło zapis", now, writer, notify)
            conn.execute("UPDATE tests SET step = step + 1 WHERE id = ?", (test["id"],))
            conn.commit()
            return {"status": RUNNING, "step": test["step"] + 1,
                    **({"wrote": step["value"]} if step["value"] != current else {})}
        result = _result(conn, test, now, settings)
        _finish(conn, test, DONE, now, result=result)
        _notify(settings, f"Test „{KINDS[test['kind']].title}” zakończony. Wynik w zakładce Testy.", notify)
        return {"status": DONE, "result": result}
