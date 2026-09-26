"""Planer pętli A — mini-MPC na blokach taryfy (funkcje czyste, bez I/O).

Horyzont ~36 h w krokach 15 min. Dla każdego bloku taryfy (szczyt / poza
szczytem, dzień / noc, maks. 8 h) wybieramy przesunięcie δ nastawy wody względem
**równoważnika krzywej grzewczej** (interpolacja 5 punktów krzywej przy
prognozowanej temperaturze zewnętrznej). Minimalizujemy koszt energii pompy
przy ograniczeniu, że przewidywana temperatura pokoi mieści się w paśmie
komfortu; wyjście z pasma jest karane kwadratowo, a ciepło zmagazynowane w domu
na końcu horyzontu — wyceniane po cenie taniej taryfy (inaczej optymalizator
wychładzałby dom pod koniec planu).

Baseline = δ 0 we wszystkich blokach, czyli natywna krzywa. Liczymy go tym samym
modelem, więc różnica kosztów to oszczędność wynikająca z planu, nie z różnic
między modelem a rzeczywistością."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Sequence

from .floor_model import FloorModel, cop
from .tariff import is_peak_hour

STEP_H = 0.25
DELTA_GRID: tuple[float, ...] = (0.0, -1.0, 1.0, -2.0, 2.0, -3.0, 3.0)   # kolejność = preferencja
PENALTY_PLN_PER_C2H = 20.0      # kara za wyjście z pasma: 1°C przez godzinę ≈ 20 zł
MAX_BLOCK_H = 8.0
MAX_SWEEPS = 4
_EPS = 1e-6


# ── krzywa grzewcza ──────────────────────────────────────────────────────

def curve_setpoint(ambient_c: Sequence[float | None], water_c: Sequence[float | None],
                   t_out_c: float) -> float | None:
    """Interpolacja liniowa punktów krzywej (poza zakresem: wartość skrajna).
    None gdy mniej niż 2 poprawne punkty."""
    pts = sorted((a, w) for a, w in zip(ambient_c, water_c) if a is not None and w is not None)
    if len(pts) < 2:
        return None
    if t_out_c <= pts[0][0]:
        return pts[0][1]
    if t_out_c >= pts[-1][0]:
        return pts[-1][1]
    for (a0, w0), (a1, w1) in zip(pts, pts[1:]):
        if a0 <= t_out_c <= a1:
            return w0 if a1 == a0 else w0 + (w1 - w0) * (t_out_c - a0) / (a1 - a0)
    return None


# ── oś czasu i bloki ─────────────────────────────────────────────────────

def floor_to_step(now: datetime, step_min: int = 15) -> datetime:
    return now.replace(minute=now.minute - now.minute % step_min, second=0, microsecond=0)


@dataclass(frozen=True)
class Block:
    i0: int                 # pierwszy krok (włącznie)
    i1: int                 # ostatni krok (wyłącznie)
    start: datetime
    end: datetime
    is_peak: bool
    is_day: bool
    price_pln_kwh: float


def build_blocks(t0: datetime, n_steps: int, is_workday: Callable[[object], bool],
                 day_start: int, day_end: int, peak_price: float, offpeak_price: float,
                 step_h: float = STEP_H, max_len_h: float = MAX_BLOCK_H) -> list[Block]:
    """Ciągłe odcinki o tym samym (szczyt, dzień); dłuższe niż `max_len_h` dzielone."""
    max_steps = max(1, int(round(max_len_h / step_h)))
    keys = []
    for i in range(n_steps):
        t = t0 + timedelta(hours=i * step_h)
        keys.append((is_peak_hour(t.hour, bool(is_workday(t.date()))),
                     day_start <= t.hour < day_end))
    blocks: list[Block] = []
    i = 0
    while i < n_steps:
        j = i + 1
        while j < n_steps and keys[j] == keys[i] and j - i < max_steps:
            j += 1
        peak, day = keys[i]
        blocks.append(Block(i0=i, i1=j, start=t0 + timedelta(hours=i * step_h),
                            end=t0 + timedelta(hours=j * step_h), is_peak=peak, is_day=day,
                            price_pln_kwh=peak_price if peak else offpeak_price))
        i = j
    return blocks


def step_prices(blocks: list[Block], n_steps: int) -> list[float]:
    prices = [0.0] * n_steps
    for b in blocks:
        for i in range(b.i0, b.i1):
            prices[i] = b.price_pln_kwh
    return prices


def step_bands(t0: datetime, n_steps: int, target_c: float, band_day_c: float,
               band_night_c: float, day_start: int, day_end: int,
               step_h: float = STEP_H) -> list[tuple[float, float]]:
    bands = []
    for i in range(n_steps):
        hour = (t0 + timedelta(hours=i * step_h)).hour
        band = band_day_c if day_start <= hour < day_end else band_night_c
        bands.append((target_c - band, target_c + band))
    return bands


def outdoor_steps(points: list[tuple[datetime, float]], t0: datetime, n_steps: int,
                  fallback_c: float, step_h: float = STEP_H) -> list[float]:
    """Prognoza godzinowa -> temperatura zewnętrzna w każdym kroku (interpolacja
    liniowa, poza zakresem wartość skrajna; brak prognozy = stała `fallback_c`)."""
    pts = sorted(points)
    if not pts:
        return [fallback_c] * n_steps
    out = []
    for i in range(n_steps):
        t = t0 + timedelta(hours=i * step_h)
        if t <= pts[0][0]:
            out.append(pts[0][1])
        elif t >= pts[-1][0]:
            out.append(pts[-1][1])
        else:
            for (ta, va), (tb, vb) in zip(pts, pts[1:]):
                if ta <= t <= tb:
                    span = (tb - ta).total_seconds()
                    out.append(va if span == 0 else va + (vb - va) * (t - ta).total_seconds() / span)
                    break
    return out


# ── optymalizacja ────────────────────────────────────────────────────────

@dataclass
class PlanInputs:
    model: FloorModel
    tr0_c: float
    qf0_kw: float
    t_out_c: list[float]
    base_c: list[float]                  # równoważnik krzywej w każdym kroku
    blocks: list[Block]
    bands: list[tuple[float, float]]
    target_c: float
    water_min_c: float = 20.0
    water_max_c: float = 32.0
    step_h: float = STEP_H
    offpeak_price: float = 0.6306
    fuse_active: bool = False            # któryś pokój poniżej minimum -> δ bloku 0 nie < 0
    hold_mean_c: float | None = None     # wymuś średnią nie niższą niż ta (oszczędność z samego przesunięcia)


@dataclass
class PlanResult:
    deltas: list[float]
    setpoints_c: list[float]             # nastawa w każdym kroku
    temps_c: list[float]                 # przewidywana średnia pokoi po każdym kroku
    energy_kwh: list[float]
    cost_pln: float                      # sam koszt energii w horyzoncie
    penalty_pln: float                   # kara za wyjście z pasma
    objective_pln: float

    def block_setpoint(self, block: Block) -> float:
        return self.setpoints_c[block.i0]


def evaluate(inp: PlanInputs, deltas: list[float]) -> PlanResult:
    n = len(inp.t_out_c)
    setpoints = [0.0] * n
    for b, d in zip(inp.blocks, deltas):
        for i in range(b.i0, b.i1):
            setpoints[i] = min(max(inp.base_c[i] + d, inp.water_min_c), inp.water_max_c)
    temps, energy = inp.model.simulate(inp.tr0_c, inp.qf0_kw, inp.t_out_c, setpoints, inp.step_h)
    prices = step_prices(inp.blocks, n)
    cost = sum(e * p for e, p in zip(energy, prices))
    viol2 = 0.0
    for t, (lo, hi) in zip(temps, inp.bands):
        v = max(lo - t, 0.0) + max(t - hi, 0.0)
        viol2 += v * v
    penalty = PENALTY_PLN_PER_C2H * viol2 * inp.step_h
    if inp.hold_mean_c is not None:
        deficit = max(inp.hold_mean_c - sum(temps) / n, 0.0)
        penalty += PENALTY_PLN_PER_C2H * deficit * deficit * n * inp.step_h
    # wartość zmagazynowanego ciepła: (Tk − cel) K × (1/g) kWh/K ÷ COP × cena taniej taryfy
    cop_ref = cop(sum(inp.t_out_c) / n, sum(inp.base_c) / n)
    stored = (temps[-1] - inp.target_c) / max(inp.model.g, 1e-3) / cop_ref * inp.offpeak_price
    return PlanResult(deltas=list(deltas), setpoints_c=setpoints, temps_c=temps,
                      energy_kwh=energy, cost_pln=cost, penalty_pln=penalty,
                      objective_pln=cost + penalty - stored)


def optimize(inp: PlanInputs) -> PlanResult:
    """Coordinate descent po blokach od δ = 0; bezpiecznik ogranicza blok 0 od dołu."""
    deltas = [0.0] * len(inp.blocks)
    best = evaluate(inp, deltas)
    for _ in range(MAX_SWEEPS):
        improved = False
        for k in range(len(inp.blocks)):
            for cand in DELTA_GRID:
                if cand == deltas[k] or (inp.fuse_active and k == 0 and cand < 0):
                    continue
                trial = list(deltas)
                trial[k] = cand
                res = evaluate(inp, trial)
                if res.objective_pln < best.objective_pln - _EPS:
                    best, deltas, improved = res, trial, True
        if not improved:
            break
    return best


def baseline(inp: PlanInputs) -> PlanResult:
    return evaluate(inp, [0.0] * len(inp.blocks))


def mean_temp(res: PlanResult) -> float:
    return sum(res.temps_c) / len(res.temps_c)


def plan_with_shift_only(inp: PlanInputs, base: PlanResult) -> PlanResult:
    """Ten sam plan, ale ze średnią temperaturą nie niższą niż baseline — czyli
    oszczędność wyłącznie z przesunięcia grzania w czasie, bez wychładzania domu."""
    held = PlanInputs(**{**inp.__dict__, "hold_mean_c": mean_temp(base)})
    return optimize(held)
