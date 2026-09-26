"""Model termiczny podłogówki — dwa stany (magazyn ciepła w wylewce + pokój),
uczony metodą najmniejszych kwadratów. Zastępuje jednowspółczynnikowy EWMA z
`model.py`, który nie widział bezwładności wylewki, a na niej opiera się cała
oszczędność (grzanie w taniej taryfie, „wybieg” w szczycie).

Ciepło z pompy `Q` przechodzi przez filtr pierwszego rzędu o stałej czasowej
`tau` (wylewka), a dopiero potem grzeje pokój:

    Qf[t+1] = Qf[t] + a · (Q[t] − Qf[t]),     a = 1 − exp(−dt/tau)
    Tr[t+1] = Tr[t] + dt · (g · Qf[t] − c · (Tr[t] − To[t]))

`g` [°C/h na kW] to odwrotność pojemności cieplnej domu, `c` [1/h] — strata do
zewnątrz. Do planowania potrzebny jest jeszcze związek nastawy wody z mocą:

    Q = e · max(T_nastawa − Tr, 0)           [kW ciepła]
    E_el = Q / COP(To, T_nastawa)            [kW energii elektrycznej]

Funkcje czyste, bez I/O i bez zależności poza stdlib.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

ETA_CARNOT = 0.45
COP_MIN, COP_MAX = 1.5, 6.0

# Wartości startowe dla dobrze ocieplonego domu z wylewką (strata ~0,1 kW/K, pojemność
# ~13 kWh/K) — do czasu pierwszego dopasowania. Lekki dom (c ≥ 0,05) jest dla tego
# typu budynku obalony przez dane; patrz `fit_balanced`.
DEFAULT_TAU_H = 4.0
DEFAULT_G = 0.08
DEFAULT_C = 0.01
DEFAULT_E = 0.5

TAU_GRID_H = (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0)
C_GRID = (0.003, 0.004, 0.006, 0.008, 0.010, 0.013, 0.017, 0.022, 0.030, 0.050)
CONSERVATIVE_RMSE_TOLERANCE = 1.10
FREE_FIT_MIN_SKILL = 0.90             # swobodna regresja: błąd ≤ 90% błędu „nic się nie zmieni”
_G_BOUNDS = (0.01, 2.0)
_C_BOUNDS = (0.002, 0.5)
_E_BOUNDS = (0.05, 5.0)

SOURCE_DEFAULT = "domyślny"
SOURCE_BOOTSTRAP = "bootstrap z zimy"
SOURCE_SHADOW = "faza cienia"
SOURCE_BALANCE = "bilans energii + inercja z ograniczeń"
SOURCE_IDENTIFIED = "regresja z wymuszeniem (natywne ograniczenie)"


def cop(t_out_c: float, t_water_c: float, eta: float = ETA_CARNOT) -> float:
    """COP jako ułamek Carnota, ograniczony do sensownego zakresu."""
    lift = t_water_c - t_out_c
    if lift <= 1.0:
        return COP_MAX
    return max(COP_MIN, min(COP_MAX, eta * (t_water_c + 273.15) / lift))


@dataclass
class FloorModel:
    tau_h: float = DEFAULT_TAU_H
    g: float = DEFAULT_G
    c: float = DEFAULT_C
    e: float = DEFAULT_E
    rmse_c: float | None = None        # błąd prognozy 6 h na danych dopasowania
    persist_c: float | None = None     # ten sam błąd dla „nic się nie zmieni”
    samples: int = 0
    source: str = SOURCE_DEFAULT
    fitted_at: str | None = None
    e_samples: int = 0
    balance_r: float | None = None     # R = g/c [K/kW] z bilansu energii (odwrotność UA)
    identified: bool = False           # bezwładność (c, τ) zmierzona z wymuszenia, nie z ograniczeń

    # ── fizyka ──────────────────────────────────────────────────────────
    def heat_kw(self, t_set_c: float, tr_c: float) -> float:
        return self.e * max(t_set_c - tr_c, 0.0)

    def step(self, tr_c: float, qf_kw: float, t_out_c: float, t_set_c: float,
             dt_h: float) -> tuple[float, float, float]:
        """Jeden krok symulacji: (Tr', Qf', energia elektryczna w kroku [kWh])."""
        q = self.heat_kw(t_set_c, tr_c)
        el_kwh = q / cop(t_out_c, t_set_c) * dt_h
        tr_next = tr_c + dt_h * (self.g * qf_kw - self.c * (tr_c - t_out_c))
        qf_next = qf_kw + alpha(dt_h, self.tau_h) * (q - qf_kw)
        return tr_next, qf_next, el_kwh

    def simulate(self, tr0_c: float, qf0_kw: float, t_out_c: list[float],
                 t_set_c: list[float], dt_h: float) -> tuple[list[float], list[float]]:
        """Trajektoria: Tr po każdym kroku (długość n) i energia [kWh] w krokach."""
        tr, qf = tr0_c, qf0_kw
        temps, energy = [], []
        for to, ts in zip(t_out_c, t_set_c):
            tr, qf, el = self.step(tr, qf, to, ts, dt_h)
            temps.append(tr)
            energy.append(el)
        return temps, energy

    # ── serializacja ───────────────────────────────────────────────────
    def as_dict(self) -> dict:
        return {
            "tau_h": round(self.tau_h, 3), "g": round(self.g, 5), "c": round(self.c, 5),
            "e": round(self.e, 5),
            "rmse_c": None if self.rmse_c is None else round(self.rmse_c, 3),
            "persist_c": None if self.persist_c is None else round(self.persist_c, 3),
            "samples": self.samples, "source": self.source,
            "fitted_at": self.fitted_at, "e_samples": self.e_samples,
            "balance_r": None if self.balance_r is None else round(self.balance_r, 3),
            "identified": self.identified,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "FloorModel":
        model = cls()
        if not data:
            return model
        try:
            return cls(
                tau_h=float(data.get("tau_h", model.tau_h)), g=float(data.get("g", model.g)),
                c=float(data.get("c", model.c)), e=float(data.get("e", model.e)),
                rmse_c=_opt_float(data.get("rmse_c")), persist_c=_opt_float(data.get("persist_c")),
                samples=int(data.get("samples", 0)), source=str(data.get("source", SOURCE_DEFAULT)),
                fitted_at=data.get("fitted_at"), e_samples=int(data.get("e_samples", 0)),
                balance_r=_opt_float(data.get("balance_r")),
                identified=bool(data.get("identified", False)),
            )
        except (TypeError, ValueError):
            return model


def _opt_float(value) -> float | None:
    return None if value is None else float(value)


def alpha(dt_h: float, tau_h: float) -> float:
    return 1.0 - math.exp(-dt_h / tau_h)


# ── dane pomiarowe → moc cieplna ────────────────────────────────────────

def measured_heat_kw(el_kwh: float, dt_h: float, t_out_c: float, t_water_c: float) -> float:
    """Moc cieplna oddana przez pompę = COP × moc elektryczna z licznika."""
    if dt_h <= 0 or el_kwh <= 0:
        return 0.0
    return cop(t_out_c, max(t_water_c, t_out_c + 3.0)) * el_kwh / dt_h


def filter_states(q_kw: list[float], tau_h: float, dt_h: float,
                  qf0_kw: float | None = None) -> list[float]:
    """Stan magazynu Qf[t] przed każdym krokiem (Qf[0] = qf0 albo Q[0])."""
    a = alpha(dt_h, tau_h)
    qf = q_kw[0] if qf0_kw is None and q_kw else (qf0_kw or 0.0)
    out = []
    for q in q_kw:
        out.append(qf)
        qf += a * (q - qf)
    return out


# ── dopasowanie ──────────────────────────────────────────────────────────

@dataclass
class Segment:
    """Ciągły odcinek próbek o stałym kroku: temperatura pokoi, zewnętrzna i
    moc cieplna oddana w kroku (Q[t] dotyczy przedziału [t, t+1))."""
    tr: list[float]
    t_out: list[float]
    q_kw: list[float]


def fit_room(segments: list[Segment], dt_h: float, min_points: int = 72,
             horizon_h: float = 6.0, lag_h: float = 2.0) -> FloorModel | None:
    """Dopasowanie (tau, g, c) — dla każdego tau regresja liniowa na (g, c),
    wybór tau o najmniejszej sumie kwadratów. Regresja idzie po przyrostach
    `lag_h` (nie 1-krokowych): różniczkowanie zaszumionego czujnika co 15 min
    zabijałoby sygnał. Jakość podaje błąd prognozy `horizon_h` naprzód."""
    k = max(1, int(round(lag_h / dt_h)))
    best: tuple[float, float, float, float] | None = None   # (sse, tau, g, c)
    total = 0
    for tau in TAU_GRID_H:
        warm = max(1, int(round(2.0 * tau / dt_h)))
        sxx = sxz = szz = sxy = szy = 0.0
        n = 0
        for x, z, y in _lagged_rows(segments, tau, dt_h, warm, k):
            sxx += x * x
            sxz += x * z
            szz += z * z
            sxy += x * y
            szy += z * y
            n += 1
        total = max(total, n)
        if n < min_points:
            continue
        det = sxx * szz - sxz * sxz
        if det <= 1e-12:
            continue
        g = (sxy * szz - szy * sxz) / det
        c = (szy * sxx - sxy * sxz) / det
        if not (_G_BOUNDS[0] <= g <= _G_BOUNDS[1] and _C_BOUNDS[0] <= c <= _C_BOUNDS[1]):
            continue
        sse = sum((y - g * x - c * z) ** 2
                  for x, z, y in _lagged_rows(segments, tau, dt_h, warm, k))
        if best is None or sse < best[0]:
            best = (sse, tau, g, c)
    if best is None:
        return None
    _, tau, g, c = best
    model = FloorModel(tau_h=tau, g=g, c=c, samples=total)
    model.rmse_c, model.persist_c = horizon_errors(model, segments, dt_h, horizon_h)
    return model


def _lagged_rows(segments: list[Segment], tau: float, dt_h: float, warm: int, k: int):
    """(x, z, y) dla okien k kroków: y = Tr[t+k]−Tr[t], x = dt·ΣQf, z = −dt·Σ(Tr−To)."""
    for seg in segments:
        qf = filter_states(seg.q_kw, tau, dt_h)
        for t in range(warm, len(seg.tr) - k):
            x = dt_h * sum(qf[t:t + k])
            z = -dt_h * sum(seg.tr[j] - seg.t_out[j] for j in range(t, t + k))
            yield x, z, seg.tr[t + k] - seg.tr[t]


def horizon_errors(model: FloorModel, segments: list[Segment], dt_h: float,
                   horizon_h: float = 6.0) -> tuple[float | None, float | None]:
    """RMSE prognozy `horizon_h` naprzód przy znanej mocy (model) oraz RMSE
    prognozy „temperatura się nie zmieni” (punkt odniesienia)."""
    steps = max(1, int(round(horizon_h / dt_h)))
    stride = max(1, steps // 2)
    warm = max(1, int(round(2.0 * model.tau_h / dt_h)))
    se_model = se_persist = 0.0
    n = 0
    for seg in segments:
        qf = filter_states(seg.q_kw, model.tau_h, dt_h)
        for t0 in range(warm, len(seg.tr) - steps, stride):
            tr, f = seg.tr[t0], qf[t0]
            a = alpha(dt_h, model.tau_h)
            for k in range(steps):
                t = t0 + k
                tr += dt_h * (model.g * f - model.c * (tr - seg.t_out[t]))
                f += a * (seg.q_kw[t] - f)
            se_model += (tr - seg.tr[t0 + steps]) ** 2
            se_persist += (seg.tr[t0] - seg.tr[t0 + steps]) ** 2
            n += 1
    if n == 0:
        return None, None
    return math.sqrt(se_model / n), math.sqrt(se_persist / n)


def balance_ratio(segments: list[Segment], min_heat_kw: float = 0.2, min_drive_k: float = 3.0
                  ) -> float | None:
    """R = średni napęd (Tr−To) / średnia moc cieplna [K/kW] — odwrotność strat UA.
    To jedyna wielkość stała, którą zimowe dane w pętli zamkniętej wyznaczają
    pewnie; bezwładność (c, τ) z nich nie wynika (regresory są skorelowane −0,9…−1)."""
    q_sum = drive_sum = 0.0
    n = 0
    for seg in segments:
        q_sum += sum(seg.q_kw)
        drive_sum += sum(t - o for t, o in zip(seg.tr, seg.t_out))
        n += len(seg.tr)
    if n == 0 or q_sum / n < min_heat_kw or drive_sum / n < min_drive_k:
        return None
    return (drive_sum / n) / (q_sum / n)


def fit_balanced(segments: list[Segment], dt_h: float, horizon_h: float = 6.0
                 ) -> FloorModel | None:
    """Dopasowanie przy spójnym bilansie energii: g = R·c, a szukana jest tylko
    inercja (c, τ) — siatka po błędzie prognozy `horizon_h`. Ponieważ zyski
    słoneczne i wewnętrzne zaniżają c, do planu bierzemy c KONSERWATYWNE: największe,
    którego błąd mieści się w 10% od najlepszego (mniej inercji = ostrożniejszy plan)."""
    r = balance_ratio(segments)
    if r is None:
        return None
    scored = []
    for tau in TAU_GRID_H:
        for c in C_GRID:
            g = r * c
            if not (_G_BOUNDS[0] <= g <= _G_BOUNDS[1]):
                continue
            rmse, persist = horizon_errors(FloorModel(tau_h=tau, g=g, c=c), segments, dt_h, horizon_h)
            if rmse is not None:
                scored.append((rmse, persist, tau, c))
    if not scored:
        return None
    best = min(scored)
    limit = best[0] * CONSERVATIVE_RMSE_TOLERANCE
    rmse, persist, tau, c = max((s for s in scored if s[2] == best[2] and s[0] <= limit),
                                key=lambda s: s[3])
    return FloorModel(tau_h=tau, g=r * c, c=c, rmse_c=rmse, persist_c=persist,
                      samples=sum(len(s.tr) for s in segments), balance_r=r)


def fit_best(segments: list[Segment], dt_h: float, min_points: int = 72,
             allow_free: bool = True) -> FloorModel | None:
    """Regresja swobodna (tylko gdy dane mają wymuszenia — `allow_free`) albo
    dopasowanie z bilansem; wygrywa akceptowalny model z mniejszym błędem.
    Bramkę jakości stosuje wołający."""
    candidates = []
    free = fit_room(segments, dt_h, min_points=min_points) if allow_free else None
    if accept_fit(free) and free.rmse_c <= FREE_FIT_MIN_SKILL * (free.persist_c or float("inf")):
        free.identified = True
        free.source = SOURCE_IDENTIFIED
        candidates.append(free)
    balanced = fit_balanced(segments, dt_h)
    if accept_fit(balanced):
        balanced.source = SOURCE_BALANCE
        candidates.append(balanced)
    if candidates:
        return min(candidates, key=lambda m: m.rmse_c)
    return balanced or free


def fit_energy_gain(q_kw: list[float], t_room: list[float], t_water: list[float]
                    ) -> tuple[float, int] | None:
    """`e` [kW/K] z regresji przez zero: Q ≈ e · (T_woda − T_pokój), po godzinach
    z grzaniem. Zwraca (e, liczba próbek) albo None gdy za mało grzania."""
    num = den = 0.0
    n = 0
    for q, tr, tw in zip(q_kw, t_room, t_water):
        d = tw - tr
        if q <= 0.05 or d < 0.5:
            continue
        num += q * d
        den += d * d
        n += 1
    if n < 24 or den <= 0:
        return None
    e = num / den
    if not (_E_BOUNDS[0] <= e <= _E_BOUNDS[1]):
        return None
    return e, n


def accept_fit(fit: FloorModel | None, max_rmse_c: float = 0.5) -> bool:
    """Dopasowanie przyjmujemy tylko gdy prognoza 6 h jest dokładna i lepsza
    od „nic się nie zmieni” — inaczej zostaje poprzedni model."""
    if fit is None or fit.rmse_c is None:
        return False
    if fit.rmse_c > max_rmse_c:
        return False
    return fit.persist_c is None or fit.rmse_c <= fit.persist_c


def stamp(model: FloorModel, source: str, now: datetime) -> FloorModel:
    if model.source not in (SOURCE_BALANCE, SOURCE_IDENTIFIED):     # te dopasowania mają własną etykietę
        model.source = source
    model.fitted_at = now.isoformat(timespec="minutes")
    return model
