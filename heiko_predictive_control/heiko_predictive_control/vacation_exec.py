"""Tryb urlopowy — wykonanie: odczyt przesunięcia, decyzja z `vacation.decide` i zapis do pompy.

Struktura jak `g13_exec` (tam szczegółowe uzasadnienia): jedyna ścieżka zapisu to `experiments._write`
(allowlista encji i zakresu ±3, `G13_MAX_STEP`). Tryb jest domyślnie wyłączony (`vacation_enabled`), działa całą dobę
(bez okna taryfy), test uruchomiony przez usera ma pierwszeństwo, a ręczna zmiana przesunięcia albo brak
potwierdzenia/odrzucenie zapisu wstrzymują tryb do jutra. Powrót do bazy tylko po wyłączeniu trybu (albo bezpiecznik).
Stan leży w bazie (`settings`, JSON) i przeżywa restart add-onu."""
from __future__ import annotations

import logging
from datetime import date, datetime

from . import db as dbm
from . import experiments, g13_exec, g13_peak, ha_client, tariff_feed, vacation
from .cycle import curve_shift_entity
from .g13_exec import _coldest_room

logger = logging.getLogger(__name__)

STATE_KEY = "vacation_state"
AWAY_ENTITY = "input_boolean.away_mode"
TITLE = "Heiko: tryb urlopowy"
_EPS = 0.05
_SHIFT_LIKE = f"%{experiments.ALLOWED_ENTITY_SUFFIX}"
DHW_AFTER = 48                                     # nastawa CWU przywracana po urlopie (decyzja roadmapy)
_DHW_KEYS = ("dhw_active", "dhw_before", "dhw_intent", "dhw_fail_day", "dhw_warn")


def _notify_to(service: str, message: str, notify) -> None:
    (notify or ha_client.notify)(service, TITLE, message)


def _announce_text(settings: dict) -> str:
    end = settings.get("vacation_to") or None
    until = f" do {date.fromisoformat(end).strftime('%d.%m')}" if end else ""
    return f"Tryb urlopowy włączony{until}: krzywa −{_config(settings).drop}, CWU {dhw_target(settings)}."


def _close(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) < _EPS


def _known(st: dict) -> tuple:
    """Wartości „swoje”: ostatni zapis, intencja, wartość sprzed niepotwierdzonego zapisu (`prev`) i `maybe`
    (cel zapisu, który zgłosił błąd — pompa mogła go jednak przyjąć)."""
    return st.get("last_written"), st.get("intent"), st.get("prev"), st.get("maybe")


def _num(settings: dict, key: str, default: float) -> float:
    value = settings.get(key)
    return default if value is None else float(value)


def _config(settings: dict) -> vacation.Config:
    return vacation.Config(drop=int(_num(settings, "vacation_curve_drop", 2)),
                           min_room_c=_num(settings, "vacation_min_room_c", 16.0))


def dhw_entity(settings: dict) -> str:
    """Encja nastawy CWU = encja przesunięcia krzywej z podmienionym sufiksem (bez osobnej opcji)."""
    return curve_shift_entity(settings).replace(experiments.ALLOWED_ENTITY_SUFFIX, experiments.DHW_SUFFIX)


def dhw_target(settings: dict) -> int:
    return max(experiments.DHW_MIN, min(experiments.DHW_MAX, int(_num(settings, "vacation_dhw_setpoint", 20))))


def active(settings: dict, today: date) -> bool:
    """Tryb działa tylko w oknie dat urlopu (`vacation.is_active`) i przy głównym wyłączniku zapisów (`heiko_enabled`)."""
    return vacation.is_active(settings, today) and bool(settings.get("heiko_enabled"))


def _away(conn, settings: dict, st: dict, now: datetime, get_state, away_call) -> None:
    """Tryb Away (`input_boolean.away_mode`): włączamy tylko gdy był wyłączony (wtedy jest „nasz”) i tylko my go
    wyłączamy. Nieudane wywołanie: stan bez zmian, ponowienie następnym tickiem."""
    desired = vacation.is_active(settings, now.date()) and bool(settings.get("vacation_with_away"))
    owned = bool(st.get("away_owned"))
    if desired and not owned:
        if ((get_state(AWAY_ENTITY) or {}).get("state")) != "off":
            return                                     # już włączony (nie nasz) albo brak odczytu — nie ruszamy
        if away_call("input_boolean", "turn_on", {"entity_id": AWAY_ENTITY}):
            st["away_owned"] = True
            dbm.set_setting(conn, STATE_KEY, st)
        else:
            logger.warning("Tryb urlopowy: nie udało się włączyć %s (away) — ponowię w następnym ticku", AWAY_ENTITY)
    elif not desired and owned:
        if away_call("input_boolean", "turn_off", {"entity_id": AWAY_ENTITY}):
            st["away_owned"] = False
            dbm.set_setting(conn, STATE_KEY, st)
        else:
            logger.warning("Tryb urlopowy: nie udało się wyłączyć %s (away) — ponowię w następnym ticku", AWAY_ENTITY)


def _in_peak(conn, settings: dict, now: datetime) -> bool:
    """Szczyt `evening_peak` z taryfy trwa teraz — tylko przy włączonym trybie Szczyt G13 (docisk urlopu)."""
    if not settings.get("g13_peak_enabled"):
        return False
    window = g13_peak.window_from_feed(tariff_feed.for_settings(conn, settings), now.date())
    return window is not None and not window.empty and window.start <= now.hour < window.end


def _take_over_g13(conn, current: float, now: datetime) -> float | None:
    """Pierwszy tick urlopu: jeśli krzywa jest przesunięta przez G13, bazą urlopu jest baza G13, a stan G13 czyścimy
    (G13 bez bazy nie wykona powrotu). Zwraca bazę G13 albo None (bez przejęcia)."""
    g13 = dbm.get_setting(conn, g13_exec.STATE_KEY) or {}
    base = g13.get("base")
    if base is None or not any(_close(current, v) for v in g13_exec._known(g13)):
        return None
    g13_exec._clear(g13)
    g13["last"] = {"ts": now.isoformat(timespec="seconds"), "reason": "urlop", "target": None}
    dbm.set_setting(conn, g13_exec.STATE_KEY, g13)
    return base


def _breaker_state(st: dict) -> vacation.State:
    return vacation.State(cold_since=datetime.fromisoformat(st["cold_since"]) if st.get("cold_since") else None,
                          tripped=bool(st.get("tripped")))


def _save(conn, st: dict, before: dict) -> None:
    if st != before:
        dbm.set_setting(conn, STATE_KEY, st)


def _apply(conn, settings: dict, st: dict, current: float, target: float, now: datetime, writer, notify,
           quiet: bool = False) -> bool:
    """Zapis `target` przez `experiments._write`; intencja trafia do stanu PRZED zapisem (odporność na restart)."""
    today = now.date().isoformat()
    previous, previous_prev = st.get("intent"), st.get("prev")
    st["intent"], st["prev"] = target, current
    dbm.set_setting(conn, STATE_KEY, st)
    try:
        ok = experiments._write(conn, settings, None, current, target, now, writer, max_step=experiments.G13_MAX_STEP)
    except ValueError as exc:
        logger.error("Tryb urlopowy: zapis %s → %s odrzucony przez allowlistę: %s", current, target, exc)
        ok = False
    st["last_write_id"] = conn.execute("SELECT MAX(id) FROM pump_writes").fetchone()[0]
    if ok:
        st["last_written"], st["maybe"] = target, None
    else:
        st["intent"], st["prev"], st["suspended_day"], st["maybe"] = (
            previous, current if previous_prev is None else previous_prev, today, target)
        if not quiet:
            experiments._notify(settings, f"Tryb urlopowy wstrzymany do jutra: HA odrzuciło zapis przesunięcia {target:g}.",
                                notify, TITLE)
    return ok


def _clear(st: dict) -> None:
    st["base"] = st["last_written"] = st["intent"] = st["prev"] = st["maybe"] = None


def _dhw_write(conn, settings: dict, st: dict, dhw: str, current: float, target: float, now: datetime, writer) -> bool:
    try:
        ok = experiments._write(conn, settings, None, current, target, now, writer, entity=dhw, supersede=False)
    except ValueError as exc:
        logger.error("Tryb urlopowy: zapis CWU %s → %s odrzucony przez allowlistę: %s", current, target, exc)
        ok = False
    return ok


def _dhw_enter(conn, settings: dict, st: dict, now: datetime, get_numeric, get_state, writer, notify) -> None:
    """Start trybu: nastawa CWU → `vacation_dhw_setpoint`. Odrzucenie/brak odczytu nie wstrzymują krzywej."""
    today = now.date().isoformat()
    dhw, target = dhw_entity(settings), dhw_target(settings)
    warn, current = None, get_numeric(dhw)
    if current is None:
        warn = "brak"
    else:
        minimum = ((get_state(dhw) or {}).get("attributes") or {}).get("min")
        try:
            too_high_min = minimum is not None and float(minimum) > target
        except (TypeError, ValueError):
            too_high_min = False
        if too_high_min:
            warn = "min"
    if warn is not None:
        if st.get("dhw_warn") != warn:
            logger.warning("Tryb urlopowy: nastawa CWU nie do ustawienia (%s) — krzywa działa bez CWU, ponawiam co tick",
                           "encja bez odczytu" if warn == "brak" else f"minimum encji {minimum} > cel {target}")
        st["dhw_warn"] = warn
    elif _close(current, target):
        st.update(dhw_active=True, dhw_before=current, dhw_warn=None)
    elif st.get("dhw_fail_day") != today:
        st["dhw_intent"], st["dhw_warn"] = target, None
        dbm.set_setting(conn, STATE_KEY, st)                     # intencja PRZED zapisem (odporność na restart)
        if _dhw_write(conn, settings, st, dhw, current, target, now, writer):
            st.update(dhw_active=True, dhw_before=current, dhw_warn=None)
        else:
            st["dhw_fail_day"] = today
            experiments._notify(settings, f"Tryb urlopowy: HA odrzuciło zapis nastawy CWU {target:g} — pozostaje {current:g}.",
                                notify, TITLE)
    dbm.set_setting(conn, STATE_KEY, st)                         # dalsze wczesne `return None` w `_run` nie wołają `_save`


def _dhw_exit(conn, settings: dict, st: dict, now: datetime, get_numeric, writer, notify) -> None:
    """Koniec trybu: jednorazowo `DHW_AFTER`, niezależnie od krzywej i od ręcznych zmian nastawy."""
    if experiments.running_test(conn) is not None:
        return
    dhw = dhw_entity(settings)
    current = get_numeric(dhw)
    if current is None:
        return
    if not _close(current, DHW_AFTER) and not _dhw_write(conn, settings, st, dhw, current, DHW_AFTER, now, writer):
        if st.get("dhw_fail_day") != now.date().isoformat():
            experiments._notify(settings, f"Tryb urlopowy wyłączony, ale powrót nastawy CWU do {DHW_AFTER} nie powiódł się "
                                          f"(jest {current:g}) — ponawiam w następnym cyklu; w razie potrzeby ustaw ręcznie.", notify, TITLE)
        st["dhw_fail_day"] = now.date().isoformat()
        dbm.set_setting(conn, STATE_KEY, st)
        return
    for key in _DHW_KEYS:
        st.pop(key, None)
    dbm.set_setting(conn, STATE_KEY, st)


def run(conn, settings: dict, now: datetime, *, get_numeric=ha_client.get_numeric_state, get_state=ha_client.get_state,
        writer=None, notify=None, away_call=ha_client.call_service) -> dict | None:
    with experiments._lock:                            # tick testów i tryb nie mogą się przeplatać
        return _run(conn, settings, now, get_numeric, get_state, writer, notify, away_call)


def _run(conn, settings, now, get_numeric, get_state, writer, notify, away_call) -> dict | None:
    today = now.date().isoformat()
    before = dbm.get_setting(conn, STATE_KEY) or {}
    st = dict(before)
    _away(conn, settings, st, now, get_state, away_call)
    before = dict(st)
    base = st.get("base")
    entity = curve_shift_entity(settings)

    if not vacation.is_active(settings, now.date()) and st.get("announced"):
        _notify_to("notify.family", "Tryb urlopowy zakończony.", notify)
        st.pop("announced", None)
        dbm.set_setting(conn, STATE_KEY, st)
        before = dict(st)

    if not active(settings, now.date()):
        if st.get("dhw_active"):
            _dhw_exit(conn, settings, st, now, get_numeric, writer, notify)
            before = dict(st)                          # stan CWU zapisany w helperze; krzywa porównuje już tylko swoje zmiany
        if base is None or experiments.running_test(conn) is not None:
            return None
        current = get_numeric(entity)
        if current is None:
            return None
        wrote = False
        if not _close(current, base) and any(_close(current, v) for v in _known(st)):
            failed_before = st.get("return_fail_day") == today
            wrote = _apply(conn, settings, st, current, base, now, writer, notify, quiet=True)
            if not wrote:                                  # stan zostaje: następny tick ponowi powrót
                if not failed_before:
                    experiments._notify(settings, f"Tryb urlopowy wyłączony, ale powrót przesunięcia do {base:g} nie "
                                                  "powiódł się — ponawiam w następnym cyklu; w razie potrzeby ustaw ręcznie.", notify, TITLE)
                st["return_fail_day"] = today
                _save(conn, st, before)
                return {"reason": "tryb wyłączony — powrót odrzucony, ponawiam", "target": base, "wrote": False}
        _clear(st)
        _save(conn, st, before)
        return {"reason": "tryb wyłączony — powrót do bazowego", "target": base, "wrote": wrote}

    if experiments.running_test(conn) is not None:
        return None

    if not st.get("dhw_active"):                       # CWU jest od krzywej niezależne (też przy `suspended_day`)
        _dhw_enter(conn, settings, st, now, get_numeric, get_state, writer, notify)
        before = dict(st)
    if not st.get("announced"):
        _notify_to("notify.family", _announce_text(settings), notify)
        st["announced"] = today
        dbm.set_setting(conn, STATE_KEY, st)
        before = dict(st)

    mode = conn.execute("SELECT id, ok, confirmed_at FROM pump_writes WHERE test_id IS NULL AND entity_id LIKE ? "
                        "ORDER BY id DESC LIMIT 1", (_SHIFT_LIKE,)).fetchone()
    if mode is not None and mode["ok"] and mode["confirmed_at"] is None:
        return None                                    # czekamy na potwierdzenie z ramki (experiments.tick)
    if mode is not None and mode["confirmed_at"] == experiments.NO_CONFIRMATION and mode["id"] == st.get("last_write_id") \
            and mode["id"] != st.get("nc_id"):                        # tylko zapis zrobiony przez tryb
        st["nc_id"], st["suspended_day"] = mode["id"], today          # powiadomienie wysłał już _confirm_writes
        _save(conn, st, before)
        return {"reason": "brak potwierdzenia zapisu — tryb wstrzymany do jutra"}

    current = get_numeric(entity)
    if current is None:
        return None

    if mode is not None and mode["id"] == st.get("last_write_id") and mode["confirmed_at"] not in (
            None, experiments.NO_CONFIRMATION, experiments.SUPERSEDED) and _close(current, st.get("last_written")):
        st["prev"] = None                              # zapis potwierdzony — stara wartość przestaje być „swoja”
    known = _known(st)
    if any(v is not None for v in known) and not any(_close(current, v) for v in known):
        st["suspended_day"] = today
        _clear(st)
        _save(conn, st, before)
        experiments._notify(settings, f"Tryb urlopowy wstrzymany do jutra: przesunięcie zmieniono ręcznie na {current:g}.",
                            notify, TITLE)
        return {"reason": "ręczna zmiana", "current": current}
    if st.get("suspended_day") == today:
        return None

    if base is None:                                   # pierwszy tick aktywacji: baza G13 zamiast przesunięcia szczytowego
        base = _take_over_g13(conn, current, now)
        if base is not None:
            st["last_written"] = current               # wartość po G13 jest teraz „nasza” (powrót do bazy, gdy nic nie zapiszemy)
    cfg = _config(settings)
    cold = _coldest_room(conn, now)
    decision = vacation.decide(now, current_shift=current, base_shift=base,
                               min_room_c=None if cold is None else cold[0],
                               state=_breaker_state(st), cfg=cfg, in_peak=_in_peak(conn, settings, now))
    s = decision.state
    st.update(cold_since=s.cold_since.isoformat() if s.cold_since else None, tripped=s.tripped)
    if decision.base is not None:
        st["base"] = decision.base
    last = st.get("last") or {}
    if (last.get("reason"), last.get("target")) != (decision.reason, decision.target):
        st["last"] = {"ts": now.isoformat(timespec="seconds"), "reason": decision.reason, "target": decision.target}

    if decision.event == "bezpiecznik":
        st["trips"] = [*(st.get("trips") or [])[-29:], today]
        room_c, room_name = cold                       # decyzja „bezpiecznik” wymaga odczytu, więc cold ≠ None
        st["trip_room"] = {"name": room_name, "c": round(room_c, 1)}
        _notify_to("notify.kacper", f"Tryb urlopowy: {room_name or 'najzimniejszy pokój'} ma {room_c:.1f} °C — poniżej "
                                    f"{cfg.min_room_c:g} °C przez 15 min, wracam do bazowego przesunięcia "
                                    f"do czasu ocieplenia.", notify)
    wrote = None
    if decision.target is not None and not _close(decision.target, current):
        wrote = _apply(conn, settings, st, current, decision.target, now, writer, notify)
    _save(conn, st, before)
    if wrote is None and decision.event is None:
        return None
    return {"reason": decision.reason, "target": decision.target, "current": current, "wrote": bool(wrote)}
