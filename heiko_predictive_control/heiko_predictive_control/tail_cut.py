"""Odcinanie „ogona” grzania (0.22.0, domyślnie wyłączone `tail_cut_enabled`): gdy pompa grzeje, a temperatura wody (Tc)
przekroczy nastawę + `tail_margin_k` (domyślnie 0), odliczamy `tail_delay_min` (domyślnie 120) i na chwilę przełączamy tryb
pompy z Auto na CWU (`dhw`), by skończyć cykl grzania; po `tail_dhw_hold_min` wracamy do Auto. Odliczanie (0.22.1) startuje
przy pierwszym przekroczeniu progu i NIE zeruje się, gdy Tc chwilowo spadnie (pompa robi piłę Tc ~55 min, sprężarka nie
staje) — jak stara automatyzacja Pompa_fix; zeruje je wyjście z grzania, tryb inny niż Auto, niski pokój, brak danych.

Moduł sam nie wywołuje usług HA: tryb zapisuje wyłącznie `experiments._write_mode` (allowlista `dhw`/`auto`). Potwierdzenie
zapisu to odczyt `raw_value` sensora `mode_setting` (nie `experiments._confirm_writes`), więc wiersz `pump_writes` zawsze
dostaje `confirmed_at` albo `NO_CONFIRMATION`. Stan (odliczanie, `forced`, pauza, historia cięć) leży w `settings` i przeżywa
restart add-onu; stan `forced` zapisujemy PRZED zapisem do pompy. Test uruchomiony przez usera, niski komfort pokoju albo
wyłączenie opcji natychmiast przywracają Auto."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from . import db as dbm
from . import experiments, ha_client
from .experiments import _iso
from .cycle import _DEFAULT_MODE, _DEFAULT_WATER_TEMP, DEFAULT_WATER_TARGET_ENTITY
from .g13_exec import _min_room, _num
from .heating_runs import HEATING_CODE

logger = logging.getLogger(__name__)

STATE_KEY = "tail_cut_state"
TITLE = "Heiko: ogon grzania"
MODE_SETTING_ENTITY = "sensor.heiko_heat_pump_mode_setting"
AUTO_CODE = experiments.MODE_CODES["auto"]
CONFIRM_WAIT_S = 70               # ramka pompy odświeża się co ~60 s
ATTEMPTS = 2
CUTS_KEPT = 60
MAX_GAP_MIN = 15                  # dłuższa przerwa między tickami (restart, błędy odczytu) restartuje odliczanie


def active(settings: dict) -> bool:
    return bool(settings.get("tail_cut_enabled", False)) and bool(settings.get("heiko_enabled"))


def _mode_entity(settings: dict) -> str:
    return settings.get("heiko_mode_setting_entity") or MODE_SETTING_ENTITY


def _raw(get_state, entity: str) -> int | None:
    st = get_state(entity)
    try:
        return int(round(float(((st or {}).get("attributes") or {}).get("raw_value"))))
    except (TypeError, ValueError):
        return None


def _save(conn, st: dict, before: dict) -> None:
    if st != before:
        dbm.set_setting(conn, STATE_KEY, st)


def _set_mode(conn, settings: dict, mode: str, old_code: int | None, now: datetime, get_state, writer, notify,
              sleep) -> bool:
    """Do 2 prób: zapis → odczekanie na ramkę → odczyt `raw_value`. Wiersz dziennika nigdy nie zostaje z NULL."""
    code = experiments.MODE_CODES[mode]
    for _ in range(ATTEMPTS):
        try:
            ok, row_id = experiments._write_mode(conn, old_code, mode, now, writer)
        except ValueError as exc:
            logger.error("Ogon grzania: zapis trybu %s odrzucony: %s", mode, exc)
            return False
        confirmed = False
        if ok:
            sleep(CONFIRM_WAIT_S)
            confirmed = _raw(get_state, _mode_entity(settings)) == code
        conn.execute("UPDATE pump_writes SET confirmed_at = ? WHERE id = ?",
                     (_iso(now) if confirmed else experiments.NO_CONFIRMATION, row_id))
        conn.commit()
        if confirmed:
            return True
    return False


def run(conn, settings: dict, now: datetime, *, get_state=ha_client.get_state, get_numeric=ha_client.get_numeric_state,
        writer=None, notify=None, sleep=time.sleep) -> dict | None:
    with experiments._lock:                            # tick testów, G13 i ogon nie mogą się przeplatać
        before = dbm.get_setting(conn, STATE_KEY) or {}
        st = dict(before)
        try:
            return _run(conn, settings, now, st, get_state, get_numeric, writer, notify, sleep)
        finally:
            _save(conn, st, before)


def _return_to_auto(conn, settings, st, now, get_state, writer, notify, sleep, reason: str) -> dict:
    cut_at = datetime.fromisoformat(st["cut_at"]) if st.get("cut_at") else now
    old = _raw(get_state, _mode_entity(settings))
    ok = _set_mode(conn, settings, "auto", old, now, get_state, writer, notify, sleep)
    if ok:
        st["forced"], st["alerted"] = False, False
        st["pause_until"] = _iso(cut_at + timedelta(minutes=_num(settings, "tail_min_pause_min", 30)))
        st["last"] = {"ts": _iso(now), "reason": reason}
        return {"reason": reason, "wrote": True}
    if not st.get("alerted"):
        st["alerted"] = True
        experiments._notify(settings, "Nie udało się wrócić do trybu Auto po odcięciu ogona grzania — sprawdź pompę ręcznie, "
                                      "ponawiam co minutę.", notify, TITLE)
    return {"reason": "powrót do Auto niepotwierdzony, ponawiam", "wrote": False}


def _run(conn, settings, now, st, get_state, get_numeric, writer, notify, sleep) -> dict | None:
    if st.get("forced"):
        cut_at = datetime.fromisoformat(st["cut_at"]) if st.get("cut_at") else None
        room = _min_room(conn, now)
        if cut_at is None or now - cut_at >= timedelta(minutes=_num(settings, "tail_dhw_hold_min", 2)):
            reason = "koniec odcięcia"
        elif experiments.writing_test(conn) is not None:
            reason = "trwa test"
        elif room is None or room < _num(settings, "tail_min_room_c", 20.5):
            reason = "pokój poniżej progu"
        elif not active(settings):
            reason = "tryb wyłączony"
        else:
            return None
        return _return_to_auto(conn, settings, st, now, get_state, writer, notify, sleep, reason)

    if not active(settings) or experiments.writing_test(conn) is not None:
        st["tail_since"] = None
        return None
    if st.get("pause_until") and now < datetime.fromisoformat(st["pause_until"]):
        st["tail_since"] = None
        return None

    work = _raw(get_state, settings.get("heiko_working_mode_entity") or _DEFAULT_MODE)
    tc = get_numeric(settings.get("heiko_water_temp_entity") or _DEFAULT_WATER_TEMP)
    setpoint = get_numeric(settings.get("heiko_water_setpoint_entity") or DEFAULT_WATER_TARGET_ENTITY)
    mode = _raw(get_state, _mode_entity(settings))
    room = _min_room(conn, now)
    if None in (work, tc, setpoint, mode, room):
        st["tail_since"] = None
        return None
    if not (work == HEATING_CODE and mode == AUTO_CODE and room >= _num(settings, "tail_min_room_c", 20.5)):
        st["tail_since"] = None
        return None
    since_iso = st.get("tail_since")
    if since_iso and now - datetime.fromisoformat(st.get("tail_seen") or since_iso) > timedelta(minutes=MAX_GAP_MIN):
        since_iso = st["tail_since"] = None            # stary znacznik po przerwie w tickach — liczymy od nowa
    if not since_iso and tc <= setpoint + _num(settings, "tail_margin_k", 0):
        return None                                    # próg jeszcze nieprzekroczony; po starcie odliczania spadek Tc go nie zeruje
    since = datetime.fromisoformat(since_iso) if since_iso else now
    st["tail_since"], st["tail_seen"] = _iso(since), _iso(now)
    minutes = (now - since).total_seconds() / 60
    if minutes < _num(settings, "tail_delay_min", 120):
        return None

    st["forced"], st["cut_at"], st["tail_since"] = True, _iso(now), None
    dbm.set_setting(conn, STATE_KEY, st)               # przed zapisem do pompy: po restarcie wiemy, że trzymamy CWU
    pause = timedelta(minutes=_num(settings, "tail_min_pause_min", 30))
    if _set_mode(conn, settings, "dhw", mode, now, get_state, writer, notify, sleep):
        st["cuts"] = [*(st.get("cuts") or [])[-(CUTS_KEPT - 1):], _iso(now)]
        st["last"] = {"ts": _iso(now), "reason": "odcięto ogon"}
        experiments._notify(settings, f"Odcięto ogon grzania (Tc {tc:g}, nastawa {setpoint:g}, od {minutes:.0f} min). "
                                      f"Powrót do Auto za {_num(settings, 'tail_dhw_hold_min', 2):g} min.", notify, TITLE)
        return {"reason": "odcięto ogon", "tc": tc, "setpoint": setpoint, "wrote": True}
    experiments._notify(settings, "Pompa nie potwierdziła przełączenia na CWU przy odcinaniu ogona grzania — wracam do Auto.",
                        notify, TITLE)
    st["alerted"] = True
    if _set_mode(conn, settings, "auto", mode, now, get_state, writer, notify, sleep):
        st["forced"], st["alerted"] = False, False
        st["pause_until"] = _iso(now + pause)
    st["last"] = {"ts": _iso(now), "reason": "brak potwierdzenia CWU"}
    return {"reason": "brak potwierdzenia CWU", "wrote": False}


def recover_on_start(conn, settings: dict, now: datetime, *, get_state=ha_client.get_state, writer=None, notify=None,
                     sleep=time.sleep) -> dict | None:
    """Po restarcie add-onu: jeśli trzymaliśmy CWU (`forced`), wracamy do Auto; bez `forced` nic nie zapisujemy."""
    with experiments._lock:
        before = dbm.get_setting(conn, STATE_KEY) or {}
        st = dict(before)
        if not st.get("forced"):
            return None
        try:
            return _return_to_auto(conn, settings, st, now, get_state, writer, notify, sleep, "restart add-onu")
        finally:
            _save(conn, st, before)
