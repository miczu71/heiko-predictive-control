"""Tryb „Szczyt G13”: podłogówka nie grzeje w szczycie `evening_peak` (okno z taryfy pv_roi, np. zimą 16–21, latem 19–22).

Czysta logika, bez I/O: dostaje czas, okno szczytu, bieżące i bazowe przesunięcie krzywej, minimum pokoi
i stan bezpiecznika, a zwraca docelowe przesunięcie. Wykonanie (zapis do pompy, ręczna zmiana, wykluczenie
z testami, opcje w bazie) to osobny etap — tu tylko decyzja.

Okno [start, end) czyta `window_from_feed` z harmonogramu taryfy (święta, pora roku i zmiany taryfy uwzględnia
pv_roi): od `start − 3 h` przesunięcie bazowe + `preheat_shift` (dogrzanie wylewki w tańszej strefie),
w oknie bazowe + `peak_shift`, od końca okna powrót do bazowego. Wynik jest obcięty do ±3 (allowlista zapisu).
Bezpiecznik: minimum pokoi stref dziennych < `min_room_c` nieprzerwanie przez 15 min → powrót do bazowego do
końca okna (jedno zdarzenie na dobę). Brak odczytu pokoi nie wyzwala bezpiecznika i nie zeruje licznika."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

PREHEAT_LEAD_H = 3                          # dogrzanie startuje tyle godzin przed szczytem
PEAK_ZONE = "evening_peak"
BREAKER_HOLD = timedelta(minutes=15)
SHIFT_LIMIT = 3.0                           # allowlista zapisu (experiments._write)


@dataclass(frozen=True)
class Window:
    """Okno szczytu w godzinach lokalnych doby: [start, end), end ≤ 24."""
    start: int
    end: int

    @property
    def preheat_from(self) -> int:
        return max(0, self.start - PREHEAT_LEAD_H)

    @property
    def empty(self) -> bool:
        return self.start == self.end


NO_PEAK = Window(0, 0)


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
    window: Window | None = None            # okno użyte do decyzji (do zapamiętania w stanie)


def _clamp(v: float) -> float:
    return max(-SHIFT_LIMIT, min(SHIFT_LIMIT, v))


def window_from_feed(feed, day: date) -> Window | None:
    """Pierwszy ciągły odcinek godzin `evening_peak` doby `day` z harmonogramu taryfy, bez względu na jego świeżość
    (ostatni znany harmonogram obowiązuje). None = brak którejkolwiek godziny doby (tryb nic nie robi);
    NO_PEAK = doba bez szczytu."""
    zones = []
    for h in range(24):
        row = feed.at(datetime(day.year, day.month, day.day, h))
        if row is None:
            return None
        zones.append(row.zone == PEAK_ZONE)
    if True not in zones:
        return NO_PEAK
    start = zones.index(True)
    end = start
    while end < 24 and zones[end]:
        end += 1
    return Window(start, end)


def decide(now: datetime, *, window: Window | None, current_shift: float, base_shift: float | None,
           min_room_c: float | None, state: State, cfg: Config) -> Decision:
    day = now.date()
    if state.day != day:
        state = State(day=day)
    if window is None:
        return Decision(None, "brak harmonogramu taryfy", state)
    if window.empty:
        return Decision(None, "brak szczytu evening_peak w taryfie", state)

    hour = now.hour
    base = current_shift if base_shift is None else base_shift
    if not window.preheat_from <= hour < window.end:
        if hour >= window.end and base_shift is not None and current_shift != base_shift:
            return Decision(base_shift, "koniec okna", state, window=window)
        return Decision(None, "poza oknem", state, window=window)

    state, event = _breaker(now, min_room_c, state, cfg)
    if state.tripped:
        return Decision(base, "bezpiecznik", state, base, event, window)
    preheat = hour < window.start
    shift = cfg.preheat_shift if preheat else cfg.peak_shift
    return Decision(_clamp(base + shift), "dogrzanie" if preheat else "szczyt", state, base, window=window)


def _breaker(now: datetime, room: float | None, state: State, cfg: Config) -> tuple[State, str | None]:
    if state.tripped or room is None:
        return state, None
    if room >= cfg.min_room_c:
        return replace(state, cold_since=None), None
    since = state.cold_since or now
    if now - since >= BREAKER_HOLD:
        return replace(state, cold_since=since, tripped=True), "bezpiecznik"
    return replace(state, cold_since=since), None
