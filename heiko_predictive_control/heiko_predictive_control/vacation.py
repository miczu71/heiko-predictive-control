"""Tryb urlopowy: krzywa grzewcza obniżona o `drop` (0–3; 0 = bez obniżki) przez całą dobę (oszczędzanie podczas nieobecności).

Czysta logika, bez I/O: dostaje bieżące i bazowe przesunięcie krzywej, minimum pokoi i stan bezpiecznika, a zwraca
docelowe przesunięcie (obcięte do ±3 — allowlista zapisu). Wykonanie (zapis do pompy, ręczna zmiana, opcje w bazie)
to `vacation_exec`.
Bezpiecznik: minimum pokoi < `min_room_c` nieprzerwanie przez 15 min → powrót do bazowego, aż pokój przekroczy próg
o `RESUME_MARGIN` (histereza). Brak odczytu nie wyzwala bezpiecznika i nie zeruje licznika."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

SHIFT_LIMIT = 3.0                           # allowlista zapisu (experiments._write)
BREAKER_HOLD = timedelta(minutes=15)
RESUME_MARGIN = 1.0                         # °C histerezy przy wznowieniu po bezpieczniku


@dataclass(frozen=True)
class Config:
    drop: int = 2
    min_room_c: float = 16.0


@dataclass(frozen=True)
class State:
    cold_since: datetime | None = None
    tripped: bool = False


@dataclass(frozen=True)
class Decision:
    target: float | None
    reason: str
    state: State
    base: float | None
    event: str | None                       # "bezpiecznik" tylko w ticku, w którym zadziałał


def _clamp(v: float) -> float:
    return max(-SHIFT_LIMIT, min(SHIFT_LIMIT, v))


def is_active(settings: dict, today: date) -> bool:
    """Urlop trwa, gdy włączony i `today` mieści się w [vacation_from, vacation_to] (włącznie; puste = bez ograniczenia).
    Nieparsowalna data = nieaktywny. Nie zależy od `heiko_enabled`."""
    if not settings.get("vacation_enabled", False):
        return False
    try:
        start, end = settings.get("vacation_from") or None, settings.get("vacation_to") or None
        if start and today < date.fromisoformat(start):
            return False
        return not (end and today > date.fromisoformat(end))
    except (TypeError, ValueError):
        return False


def decide(now: datetime, *, current_shift: float, base_shift: float | None, min_room_c: float | None,
           state: State, cfg: Config, in_peak: bool = False) -> Decision:
    base = current_shift if base_shift is None else base_shift
    drop = max(0, min(3, round(cfg.drop)))
    event = None
    if min_room_c is not None:
        if min_room_c >= cfg.min_room_c:
            state = replace(state, cold_since=None)
            if state.tripped and min_room_c >= cfg.min_room_c + RESUME_MARGIN:
                state = replace(state, tripped=False)
        else:
            since = state.cold_since or now
            state = replace(state, cold_since=since)
            if not state.tripped and now - since >= BREAKER_HOLD:
                state, event = replace(state, tripped=True), "bezpiecznik"
    if state.tripped:
        return Decision(base, "bezpiecznik", state, base, event)
    if in_peak:
        return Decision(-SHIFT_LIMIT, "urlop: szczyt", state, base, event)
    return Decision(_clamp(base - drop), "urlop", state, base, event)
