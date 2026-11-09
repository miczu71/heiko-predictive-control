"""Tryb „Szczyt G13”: w dni robocze podłogówka nie grzeje w szczycie 16–21 (zima, taryfa G13).

Czysta logika, bez I/O: dostaje czas, dzień roboczy, bieżące i bazowe przesunięcie krzywej, minimum pokoi
i stan bezpiecznika, a zwraca docelowe przesunięcie. Wykonanie (zapis do pompy, ręczna zmiana, wykluczenie
z testami, opcje w bazie) to osobny etap — tu tylko decyzja.

Okna (czas lokalny): 13:00–16:00 przesunięcie bazowe + `preheat_shift` (dogrzanie wylewki w tańszej strefie),
16:00–21:00 bazowe + `peak_shift`, od 21:00 powrót do bazowego. Wynik jest obcięty do ±3 (allowlista zapisu).
Bezpiecznik: minimum pokoi stref dziennych < `min_room_c` nieprzerwanie przez 15 min → powrót do bazowego do
końca okna (jedno zdarzenie na dobę). Brak odczytu pokoi nie wyzwala bezpiecznika i nie zeruje licznika."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

PREHEAT_FROM_H = 13
PEAK_FROM_H = 16
WINDOW_END_H = 21
BREAKER_HOLD = timedelta(minutes=15)
SHIFT_LIMIT = 3.0                           # allowlista zapisu (experiments._write)
WORKDAY_PROBE_H = 17                        # godzina szczytu G13: is_peak tylko w dni robocze


@dataclass(frozen=True)
class Config:
    min_room_c: float = 20.5
    preheat_shift: float = 1.0
    peak_shift: float = -3.0


@dataclass(frozen=True)
class State:
    """Stan bezpiecznika; wołający trzyma go między tickami (`decide` sama zeruje go przy nowej dobie)."""
    day: date | None = None
    cold_since: datetime | None = None
    tripped: bool = False


@dataclass(frozen=True)
class Decision:
    target: float | None                    # None = nic nie zapisuj
    reason: str
    state: State
    base: float | None = None               # bazowe przesunięcie do zapamiętania (None poza oknem)
    event: str | None = None                # "bezpiecznik" tylko w ticku, w którym zadziałał


def _clamp(v: float) -> float:
    return max(-SHIFT_LIMIT, min(SHIFT_LIMIT, v))


def workday_from_feed(feed, day: date, now: datetime) -> bool | None:
    """True/False z harmonogramu taryfy (szczyt o 17:00 istnieje tylko w dni robocze); None = brak danych
    (zapas G12w albo brak godziny), wtedy tryb nic nie robi."""
    if feed.status(now).get("source") != "feed":
        return None
    row = feed.at(datetime(day.year, day.month, day.day, WORKDAY_PROBE_H))
    return None if row is None else bool(row.is_peak)


def decide(now: datetime, *, workday: bool | None, current_shift: float, base_shift: float | None,
           min_room_c: float | None, state: State, cfg: Config) -> Decision:
    day = now.date()
    if state.day != day:
        state = State(day=day)
    if workday is None:
        return Decision(None, "brak harmonogramu taryfy", state)
    if not workday:
        return Decision(None, "dzień nieroboczy", state)

    hour = now.hour
    base = current_shift if base_shift is None else base_shift
    if not PREHEAT_FROM_H <= hour < WINDOW_END_H:
        if hour >= WINDOW_END_H and base_shift is not None and current_shift != base_shift:
            return Decision(base_shift, "koniec okna", state)
        return Decision(None, "poza oknem", state)

    state, event = _breaker(now, min_room_c, state, cfg)
    if state.tripped:
        return Decision(base, "bezpiecznik", state, base, event)
    shift = cfg.preheat_shift if hour < PEAK_FROM_H else cfg.peak_shift
    return Decision(_clamp(base + shift), "dogrzanie" if hour < PEAK_FROM_H else "szczyt", state, base)


def _breaker(now: datetime, room: float | None, state: State, cfg: Config) -> tuple[State, str | None]:
    if state.tripped or room is None:
        return state, None
    if room >= cfg.min_room_c:
        return replace(state, cold_since=None), None
    since = state.cold_since or now
    if now - since >= BREAKER_HOLD:
        return replace(state, cold_since=since, tripped=True), "bezpiecznik"
    return replace(state, cold_since=since), None
