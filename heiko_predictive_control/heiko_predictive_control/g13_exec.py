"""Tryb „Szczyt G13” — wykonanie (E3): odczyt przesunięcia, decyzja z `g13_peak.decide` i zapis do pompy.

Jedyna ścieżka zapisu to `experiments._write` (allowlista encji i zakresu ±3, krok dowolny tylko dla przesunięcia
krzywej przez `G13_MAX_STEP`). Tryb jest domyślnie wyłączony (`g13_peak_enabled`), test uruchomiony przez usera ma
pierwszeństwo, a ręczna zmiana przesunięcia albo brak potwierdzenia/odrzucenie zapisu wstrzymują tryb do jutra.
Stan (baza, ostatni zapis, intencja, bezpiecznik) leży w bazie (`settings`, JSON) i przeżywa restart add-onu."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import db as dbm
from . import experiments, g13_peak, ha_client, tariff_feed
from .cycle import curve_shift_entity

logger = logging.getLogger(__name__)

STATE_KEY = "g13_peak_state"
TITLE = "Heiko: Szczyt G13"
ROOM_MAX_AGE = timedelta(minutes=20)
_EPS = 0.05
_SHIFT_LIKE = f"%{experiments.ALLOWED_ENTITY_SUFFIX}"


def _close(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) < _EPS


def _known(st: dict) -> tuple:
    """Wartości, które tryb uznaje za „swoje”: ostatni zapis, intencja i wartość sprzed zapisu (`prev`, dopóki zapis
    nie został potwierdzony — po braku potwierdzenia pompa może mieć starą albo spóźnioną nową) i `maybe` (cel
    zapisu, który zgłosił błąd, np. timeout — pompa mogła go jednak przyjąć)."""
    return st.get("last_written"), st.get("intent"), st.get("prev"), st.get("maybe")


def _num(settings: dict, key: str, default: float) -> float:
    """Opcja liczbowa; pusta (`None` po wyczyszczeniu pola w Opcjach) = wartość domyślna."""
    value = settings.get(key)
    return default if value is None else float(value)


def _config(settings: dict) -> g13_peak.Config:
    return g13_peak.Config(min_room_c=_num(settings, "g13_peak_min_room_c", 20.5),
                           preheat_shift=_num(settings, "g13_peak_preheat_shift", 1.0),
                           peak_shift=_num(settings, "g13_peak_peak_shift", -3.0))


def active(settings: dict) -> bool:
    """Tryb działa tylko przy własnym przełączniku i głównym wyłączniku zapisów do pompy (`heiko_enabled`)."""
    return bool(settings.get("g13_peak_enabled", False)) and bool(settings.get("heiko_enabled"))


def _breaker_state(st: dict) -> g13_peak.State:
    return g13_peak.State(day=date.fromisoformat(st["day"]) if st.get("day") else None,
                          cold_since=datetime.fromisoformat(st["cold_since"]) if st.get("cold_since") else None,
                          tripped=bool(st.get("tripped")))


def _min_room(conn, now: datetime) -> float | None:
    row = dbm.latest_cycle(conn, "heiko")
    if row is None or row["min_room_c"] is None or row["ts"] < (now - ROOM_MAX_AGE).isoformat():
        return None
    return float(row["min_room_c"])


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
        logger.error("Szczyt G13: zapis %s → %s odrzucony przez allowlistę: %s", current, target, exc)
        ok = False
    st["last_write_id"] = conn.execute("SELECT MAX(id) FROM pump_writes").fetchone()[0]
    if ok:
        st["last_written"], st["maybe"] = target, None
    else:
        # wartość sprzed próby zostaje „swoja” (prev), a cel próby jest możliwy (maybe): pompa mogła go przyjąć mimo błędu
        st["intent"], st["prev"], st["suspended_day"], st["maybe"] = (
            previous, current if previous_prev is None else previous_prev, today, target)
        if not quiet:
            experiments._notify(settings, f"Tryb Szczyt G13 wstrzymany do jutra: HA odrzuciło zapis przesunięcia {target:g}.",
                                notify, TITLE)
    return ok


def _clear(st: dict) -> None:
    st["base"] = st["last_written"] = st["intent"] = st["prev"] = st["maybe"] = None


def run(conn, settings: dict, now: datetime, *, get_numeric=ha_client.get_numeric_state, writer=None, notify=None) -> dict | None:
    with experiments._lock:                            # tick testów i tryb nie mogą się przeplatać
        return _run(conn, settings, now, get_numeric, writer, notify)


def _run(conn, settings, now, get_numeric, writer, notify) -> dict | None:
    today = now.date().isoformat()
    before = dbm.get_setting(conn, STATE_KEY) or {}
    st = dict(before)
    base = st.get("base")
    entity = curve_shift_entity(settings)

    if not active(settings):
        if base is None or experiments.running_test(conn) is not None:
            return None
        current = get_numeric(entity)
        if current is None:
            return None
        wrote = False
        if not _close(current, base) and any(_close(current, v) for v in _known(st)):
            failed_before = st.get("return_fail_day") == today
            wrote = _apply(conn, settings, st, current, base, now, writer, notify, quiet=True)     # powrót do bazowego po wyłączeniu
            if not wrote:                                  # stan zostaje: następny tick ponowi powrót (nie zostawiamy −3)
                if not failed_before:
                    experiments._notify(settings, f"Tryb Szczyt G13 wyłączony, ale powrót przesunięcia do {base:g} nie "
                                                  "powiódł się — ponawiam co 5 min; w razie potrzeby ustaw ręcznie.", notify, TITLE)
                st["return_fail_day"] = today
                _save(conn, st, before)
                return {"reason": "tryb wyłączony — powrót odrzucony, ponawiam", "target": base, "wrote": False}
        _clear(st)
        _save(conn, st, before)
        return {"reason": "tryb wyłączony — powrót do bazowego", "target": base, "wrote": wrote}

    if experiments.running_test(conn) is not None:
        return None

    mode = conn.execute("SELECT id, ok, confirmed_at FROM pump_writes WHERE test_id IS NULL AND entity_id LIKE ? "
                        "ORDER BY id DESC LIMIT 1", (_SHIFT_LIKE,)).fetchone()
    if mode is not None and mode["ok"] and mode["confirmed_at"] is None:
        return None                                    # czekamy na potwierdzenie z ramki (experiments.tick)
    if mode is not None and mode["confirmed_at"] == experiments.NO_CONFIRMATION and mode["id"] == st.get("last_write_id") \
            and mode["id"] != st.get("nc_id"):                        # tylko zapis zrobiony przez tryb
        st["nc_id"], st["suspended_day"] = mode["id"], today          # powiadomienie wysłał już _confirm_writes
                                                                      # last_written/intent/prev zostają: pompa ma starą albo spóźnioną nową wartość
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
        experiments._notify(settings, f"Tryb Szczyt G13 wstrzymany do jutra: przesunięcie zmieniono ręcznie na {current:g}.",
                            notify, TITLE)
        return {"reason": "ręczna zmiana", "current": current}

    after_window = now.hour >= g13_peak.WINDOW_END_H or st.get("day") not in (None, today)
    if base is not None and after_window:
        if _close(current, base):
            _clear(st)
            _save(conn, st, before)
            return {"reason": "stan wyzerowany", "current": current}
        wrote = _apply(conn, settings, st, current, base, now, writer, notify,
                       quiet=st.get("return_fail_day") == today)         # uporczywe odrzucenie: jedno powiadomienie na dobę
        if not wrote:
            st["return_fail_day"] = today
        _save(conn, st, before)
        return {"reason": "koniec okna", "target": base, "wrote": wrote}
    if st.get("suspended_day") == today:
        return None

    cfg = _config(settings)
    feed = tariff_feed.for_settings(conn, settings)
    workday = g13_peak.workday_from_feed(feed, now.date(), now)
    decision = g13_peak.decide(now, workday=workday, current_shift=current, base_shift=base, min_room_c=_min_room(conn, now),
                               state=_breaker_state(st), cfg=cfg)
    s = decision.state
    st.update(day=s.day.isoformat() if s.day else None, cold_since=s.cold_since.isoformat() if s.cold_since else None,
              tripped=s.tripped)
    if decision.base is not None:
        st["base"] = decision.base
    last = st.get("last") or {}
    if (last.get("reason"), last.get("target")) != (decision.reason, decision.target):
        st["last"] = {"ts": now.isoformat(timespec="seconds"), "reason": decision.reason, "target": decision.target}

    if decision.event == "bezpiecznik":
        st["trips"] = [*(st.get("trips") or [])[-29:], today]          # historia do kafla na pulpicie
        experiments._notify(settings, f"Tryb Szczyt G13: pokój poniżej {cfg.min_room_c:g} °C przez 15 min — wracam do "
                                      "bazowego do 21:00.", notify, TITLE)
    wrote = None
    if decision.target is not None and not _close(decision.target, current):
        wrote = _apply(conn, settings, st, current, decision.target, now, writer, notify)
    _save(conn, st, before)
    if wrote is None and decision.event is None:
        return None
    return {"reason": decision.reason, "target": decision.target, "current": current, "wrote": bool(wrote)}
