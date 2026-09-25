"""Pętla B (AC poddasza) — czysta logika decyzyjna, bez I/O.

`decide()` dostaje odczyty i trwały stan, zwraca komendy do wykonania, nowy stan
i fazę. Wywołujący (cycle.run_attic_cycle) sam woła usługi HA i zapisuje stan.

Zasady (patrz docs planu Etapu 2):
- add-on przejmuje AC tylko wtedy, gdy jest WYŁĄCZONE (włączone przez człowieka
  = ręczne użycie, nie dotykamy);
- AC reguluje samo, add-on koryguje offset nastawy względem termometru pokoju;
- ręczna zmiana AC w oknie pracy = odpuszczamy do końca dnia;
- otwarte okno dłużej niż 2 min = pauza; drzwi nie wpływają na sterowanie;
- urlop albo ręczny przełącznik pauzy = dzień nieaktywny (faza „wstrzymane");
- czujnik obecności: w oknie pracy pusto dłużej niż `attic_vacant_after_min` (licząc od
  początku okna) = AC wyłączone (faza „pusto"), grzanie wraca z obecnością. Czujnik
  niedostępny albo próg 0 = reguła wyłączona (bezpieczniej grzać niż nie)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

from .profiles import AtticProfiles

AC_MIN_C = 16.0
AC_MAX_C = 30.0
OFFSET_MIN_C = -1.0
OFFSET_MAX_C = 4.0
OFFSET_START_C = 1.5
HEAT_RATE_START = 4.0
HEAT_RATE_MIN, HEAT_RATE_MAX = 1.0, 10.0
HEAT_RATE_ALPHA = 0.3
OFFSET_GAIN = 0.3
OFFSET_DEADBAND_C = 0.2
PREHEAT_BOOST_C = 2.0        # nadwyżka nastawy podczas dogrzewania (jak stare 28°C)
REACHED_BELOW_C = 0.3        # T >= cel - 0,3 => dogrzane
FAR_BELOW_C = 1.0            # T < cel - 1,0 => wracamy do dogrzewania
TOO_WARM_ABOVE_C = 0.7       # T > cel + 0,7 => AC off (nadal przejęte)
MARGIN_MIN = {"komfort": 20, "ekonomia": 10}
OWN_WRITE_GRACE_S = 120      # tyle czekamy z oceną „ręcznej zmiany” po własnym zapisie
WINDOW_OPEN_PAUSE_S = 120
REGULATION_MIN_GAP_S = 240   # najwyżej ~1 zapis regulacyjny na cykl 5 min
OFFSET_SETTLE_S = 600        # offset uczymy dopiero, gdy AC zdążyło zareagować
MANUAL_SETPOINT_TOLERANCE_C = 0.5

OFF = {"service": "turn_off", "data": {}}


@dataclass
class AtticInputs:
    now: datetime
    is_workday: bool | None
    on_vacation: bool | None
    temp_c: float | None
    ac_state: str | None                 # 'off' | 'heat' | ... ; None = niedostępne
    ac_setpoint_c: float | None
    window_open_since: datetime | None = None   # najstarsza zmiana na 'otwarte'
    door_open: bool | None = None               # tylko log
    paused: bool | None = None                  # ręczny przełącznik pauzy; None = brak/niedostępny
    presence: bool | None = None                # czujnik obecności; None = brak/niedostępny
    vacant_since: datetime | None = None        # od kiedy czujnik pokazuje brak obecności


@dataclass
class AtticState:
    owned: bool = False
    owned_day: str | None = None
    mode: str = "dogrzewanie"            # 'dogrzewanie' | 'utrzymanie'
    last_cmd: dict | None = None         # {"hvac": 'heat'|'off', "temp": float|None, "ts": iso}
    override_date: str | None = None
    offset_c: float = OFFSET_START_C
    heat_rate_c_h: float = HEAT_RATE_START
    preheat: dict | None = None          # {"ts": iso, "temp": float}
    paused_by_window: bool = False
    last_takeover_day: str | None = None  # powiadomienie o starcie grzania: raz dziennie

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "AtticState":
        st = cls()
        if not isinstance(data, dict):
            return st
        for key, default in asdict(st).items():
            if key not in data:
                continue
            value = data[key]
            if isinstance(default, float):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            setattr(st, key, value)
        return st


@dataclass
class Decision:
    commands: list[dict]
    state: AtticState
    phase: str
    target_c: float | None = None
    planned_start: datetime | None = None
    dry_run: bool = False
    events: list[str] = field(default_factory=list)
    vacant_min: float | None = None      # minuty pustki (0 = ktoś jest); None = brak danych


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round_half(value: float) -> float:
    return round(value * 2) / 2


def _setpoint(target: float, offset: float, mode: str) -> float:
    raw = target + offset + (PREHEAT_BOOST_C if mode == "dogrzewanie" else 0.0)
    return _clamp(_round_half(raw), AC_MIN_C, AC_MAX_C)


def _lead_minutes(temp: float | None, target: float, rate: float, margin: int,
                  fallback_min: int, max_min: int) -> float:
    if temp is None:
        return float(min(fallback_min, max_min))
    deficit = target - temp
    if deficit <= 0:
        return 0.0
    return _clamp(deficit / rate * 60.0 + margin, 0.0, float(max_min))


def _release(st: AtticState) -> None:
    st.owned = False
    st.owned_day = None
    st.last_cmd = None
    st.preheat = None
    st.paused_by_window = False


def _cmd_heat(setpoint: float) -> dict:
    return {"service": "set_temperature",
            "data": {"temperature": setpoint, "hvac_mode": "heat"}}


def _record(st: AtticState, now: datetime, hvac: str, temp: float | None) -> None:
    st.last_cmd = {"hvac": hvac, "temp": temp, "ts": now.isoformat()}


def _learn_rate(st: AtticState, temp: float, now: datetime) -> None:
    p = st.preheat
    st.preheat = None
    if not p or p.get("temp") is None:
        return
    hours = (now - datetime.fromisoformat(p["ts"])).total_seconds() / 3600.0
    rise = temp - float(p["temp"])
    if hours < 0.25 or rise < 0.3:
        return
    measured = _clamp(rise / hours, HEAT_RATE_MIN, HEAT_RATE_MAX)
    st.heat_rate_c_h = round(
        (1 - HEAT_RATE_ALPHA) * st.heat_rate_c_h + HEAT_RATE_ALPHA * measured, 4)


def _manual_change(inp: AtticInputs, last_cmd: dict) -> bool:
    if inp.ac_state is None:
        return False
    if inp.ac_state != last_cmd["hvac"]:
        return True
    if last_cmd["hvac"] == "heat" and inp.ac_setpoint_c is not None \
            and last_cmd.get("temp") is not None:
        return abs(inp.ac_setpoint_c - float(last_cmd["temp"])) >= MANUAL_SETPOINT_TOLERANCE_C
    return False


def decide(inp: AtticInputs, state: AtticState, s: dict, enabled: bool) -> Decision:
    st = replace(state)          # płytka kopia; słowniki w środku podmieniamy, nie mutujemy
    now = inp.now
    today = now.date().isoformat()
    profile = s.get("attic_active_profile", "ekonomia")
    target = AtticProfiles.from_settings(s).target(profile)
    ac_on = inp.ac_state not in (None, "off")
    dry = not enabled

    vacant_min: float | None = None      # ustawiane niżej, gdy znane godziny okna

    def result(cmds, phase, planned=None, events=None, dry_run=None):
        return Decision(commands=cmds, state=st, phase=phase, target_c=target,
                        planned_start=planned, dry_run=dry if dry_run is None else dry_run,
                        events=events or [], vacant_min=vacant_min)

    # Wyłączone sterowanie: jeśli AC było przejęte, oddajemy je wyłączone (jednorazowo).
    if not enabled and st.owned:
        _release(st)
        return result([dict(OFF)] if ac_on else [], "przekazanie", dry_run=False)

    # Przejęcie z poprzedniego dnia (np. add-on nie działał o 16:00).
    if st.owned and st.owned_day != today:
        _release(st)
        return result([dict(OFF)] if ac_on else [], "koniec_okna", events=["end"])

    work_start = now.replace(hour=int(s.get("attic_work_start_hour", 8)),
                             minute=0, second=0, microsecond=0)
    window_end = now.replace(hour=int(s.get("attic_work_end_hour", 16)),
                             minute=0, second=0, microsecond=0)
    vacant_after = int(s.get("attic_vacant_after_min", 0) or 0)
    vacant = False
    if inp.presence is True:
        vacant_min = 0.0
    elif inp.presence is False and inp.vacant_since is not None:
        # licznik pustki startuje najwcześniej od początku okna pracy — przed 8:00
        # nikt jeszcze nie musi być na poddaszu, więc nie liczy się do progu
        since = max(inp.vacant_since, work_start)
        vacant_min = max(0.0, (now - since).total_seconds() / 60.0)
        vacant = vacant_after > 0 and vacant_min >= vacant_after

    blocked = bool(inp.on_vacation) or bool(inp.paused)       # urlop albo ręczna pauza
    day_active = bool(inp.is_workday) and not blocked

    planned_start = None
    if day_active and now < window_end:
        lead = _lead_minutes(inp.temp_c, target, st.heat_rate_c_h,
                             MARGIN_MIN.get(profile, 10),
                             int(s.get("attic_preheat_lead_min", 45)),
                             int(s.get("attic_preheat_max_min", 120)))
        planned_start = work_start - timedelta(minutes=lead)
    in_window = (day_active and now < window_end
                 and (now >= planned_start or st.owned))

    if not in_window:
        held = bool(inp.is_workday) and blocked and now < window_end
        if st.owned:
            _release(st)
            return result([dict(OFF)] if ac_on else [], "wstrzymane" if held else "koniec_okna",
                          planned_start, events=["end"])
        if held:
            phase = "wstrzymane"
        else:
            phase = "czeka" if day_active and now < window_end else "poza_oknem"
        return result([], phase, planned_start)

    if st.override_date == today:
        return result([], "przejecie_reczne", planned_start)

    window_pause = (inp.window_open_since is not None
                    and (now - inp.window_open_since).total_seconds() >= WINDOW_OPEN_PAUSE_S)
    t = inp.temp_c

    # ── AC nieprzejęte ───────────────────────────────────────────────────────
    if not st.owned:
        if window_pause:
            return result([], "pauza_okno", planned_start)
        if inp.ac_state is None:
            return result([], "brak_ac", planned_start)
        if ac_on:
            return result([], "reczne_uzycie", planned_start)
        if vacant:
            return result([], "pusto", planned_start)
        if t is not None and t >= target - REACHED_BELOW_C:
            return result([], "utrzymanie", planned_start)
        st.owned, st.owned_day = True, today
        st.mode = "dogrzewanie" if (t is None or t < target - FAR_BELOW_C) else "utrzymanie"
        st.preheat = ({"ts": now.isoformat(), "temp": t}
                      if st.mode == "dogrzewanie" and t is not None else None)
        sp = _setpoint(target, st.offset_c, st.mode)
        _record(st, now, "heat", sp)
        first_today = st.last_takeover_day != today
        st.last_takeover_day = today
        return result([_cmd_heat(sp)], st.mode, planned_start,
                      events=["takeover"] if first_today else [])

    # ── AC przejęte ──────────────────────────────────────────────────────────
    lc = st.last_cmd
    if lc and (now - datetime.fromisoformat(lc["ts"])).total_seconds() >= OWN_WRITE_GRACE_S \
            and _manual_change(inp, lc):
        st.override_date = today
        _release(st)
        return result([], "przejecie_reczne", planned_start, events=["manual_override"])

    if window_pause:
        cmds = []
        if ac_on:
            cmds = [dict(OFF)]
            _record(st, now, "off", None)
        newly_paused = not st.paused_by_window
        st.paused_by_window = True
        return result(cmds, "pauza_okno", planned_start,
                      events=["window_pause"] if newly_paused else [])

    if vacant:
        # nikogo nie ma od dawna: wyłączamy i oddajemy sterowanie; z obecnością
        # zadziała zwykłe przejęcie (szybkie dogrzewanie po powrocie)
        _release(st)
        return result([dict(OFF)] if ac_on else [], "pusto", planned_start, events=["vacant"])

    resume = st.paused_by_window
    st.paused_by_window = False
    last_hvac = "off" if resume else (lc["hvac"] if lc else "heat")

    if t is not None:
        if st.mode == "dogrzewanie" and t >= target - REACHED_BELOW_C:
            _learn_rate(st, t, now)
            st.mode = "utrzymanie"
        elif st.mode == "utrzymanie" and t < target - FAR_BELOW_C:
            st.mode = "dogrzewanie"
            st.preheat = {"ts": now.isoformat(), "temp": t}

    if t is None:
        hvac = "heat" if (resume or last_hvac == "heat") else "off"
    elif last_hvac == "heat":
        hvac = "off" if t > target + TOO_WARM_ABOVE_C else "heat"
    elif resume:
        hvac = "off" if t > target + TOO_WARM_ABOVE_C else "heat"
    else:
        hvac = "heat" if t < target - REACHED_BELOW_C else "off"

    elapsed = (now - datetime.fromisoformat(lc["ts"])).total_seconds() if lc else 1e9

    if (hvac == "heat" and last_hvac == "heat" and st.mode == "utrzymanie"
            and t is not None and elapsed >= OFFSET_SETTLE_S):
        err = target - t
        if abs(err) > OFFSET_DEADBAND_C:
            st.offset_c = round(_clamp(st.offset_c + OFFSET_GAIN * err,
                                       OFFSET_MIN_C, OFFSET_MAX_C), 4)

    if hvac == "off":
        if ac_on and not resume and elapsed >= REGULATION_MIN_GAP_S:
            _record(st, now, "off", None)
            return result([dict(OFF)], "utrzymanie", planned_start)
        if not ac_on and last_hvac != "off":
            _record(st, now, "off", None)
        return result([], "utrzymanie", planned_start)

    sp = _setpoint(target, st.offset_c, st.mode)
    needs_write = (not ac_on or inp.ac_state != "heat" or inp.ac_setpoint_c is None
                   or abs(inp.ac_setpoint_c - sp) >= MANUAL_SETPOINT_TOLERANCE_C)
    if needs_write and (resume or elapsed >= REGULATION_MIN_GAP_S):
        _record(st, now, "heat", sp)
        return result([_cmd_heat(sp)], st.mode, planned_start)
    return result([], st.mode, planned_start)
