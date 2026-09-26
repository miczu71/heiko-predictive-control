"""Analizator „krzywa i taryfa” — propozycja przesunięcia krzywej grzewczej o ±1.

Reguła zapasu komfortu na statystykach z 24 h (nie na chwilowej temperaturze, która skacze w ciągu doby):
  • zapas (średnia stref ≥ cel + margines, a najzimniejszy wybrany pokój z zapasem nad minimum PO przewidywanym
    ostygnięciu domu) → EKSPERYMENT −1 (soczewka Oszczędność) — ocena skutków z planera, pewność niska, dopóki
    model nie ma zidentyfikowanej bezwładności;
  • za zimno (średnia poniżej celu albo najzimniejszy pokój blisko minimum) → zmiana +1 (soczewka Komfort).
Działa tylko przy włączonej krzywej — bez niej przesunięcie nie ma efektu.

Przesuwanie grzania w czasie (taryfa) nie mieści się w „1 zatwierdzenie = 1 zmiana”, więc pokazujemy je
tylko informacyjnie (`time_shift_pln_day`); pakiety zmian dopiero w D5."""
from __future__ import annotations

from .. import catalog
from .. import floor_plan as fp
from ..floor_model import cop
from . import (CONF_LOW, CONF_MEDIUM, KIND_CHANGE, KIND_EXPERIMENT, LENS_COMFORT, LENS_ECONOMY,
               Draft, Snapshot)

KEY = "curve_shift"
DOWN_AVG_MARGIN_C = 0.3      # średnia stref co najmniej tyle powyżej celu
DOWN_MIN_MARGIN_C = 0.5      # a najzimniejszy pokój po ostygnięciu domu co najmniej tyle powyżej minimum (0,3 przerwy nad progiem „+1”)
FALLBACK_COOLING_C = 0.7     # ostygnięcie po −1, gdy brak oceny z planera (model: ok. 0,65°C po 36 h)
UP_AVG_MARGIN_C = 0.3        # średnia stref co najmniej tyle poniżej celu…
UP_MIN_MARGIN_C = 0.2        # …albo najzimniejszy pokój mniej niż tyle powyżej minimum
TTL_H = 6                    # decyzja 4 z wywiadu: krzywa 6 h


def uniform_effects(inp: fp.PlanInputs, base: fp.PlanResult | None = None,
                    deltas: tuple[int, ...] = (-1, 1)) -> dict:
    """Skutki jednolitego przesunięcia nastawy wody o δ we wszystkich blokach względem natywnej krzywej,
    przeliczone na dobę: zmiana kosztu [zł], energii [kWh], średniej temperatury oraz najniższa
    przewidywana średnia. Ten sam planer i model co plan blokowy, więc różnice są spójne z planem.

    Koszt i energia są skorygowane o „dług cieplny”: obniżenie nastawy oszczędza w horyzoncie głównie ciepło
    wyjęte z zasobnika domu, które trzeba oddać, żeby wrócić do temperatury bazowej na końcu. Bez tej korekty
    oszczędność wychodziła 3–8× za duża i nie zależała od pogody. Korekta jest wyceniana tak samo jak w celu
    planera (`floor_plan.evaluate`): ΔT_końcowa / g / COP × cena taniej taryfy. Surowa energia horyzontu zostaje
    w `energy_horizon_delta_kwh` dla przejrzystości."""
    base = base or fp.baseline(inp)
    n = len(inp.t_out_c)
    day = 24.0 / (n * inp.step_h)
    ref_cop = cop(sum(inp.t_out_c) / n, sum(inp.base_c) / n)
    debt_kwh_per_k = 1.0 / max(inp.model.g, 1e-3) / ref_cop            # prąd na 1 K różnicy temperatury końcowej
    out: dict = {"horizon_h": round(n * inp.step_h, 1),
                 "base": {"mean_temp_c": round(fp.mean_temp(base), 2), "min_temp_c": round(min(base.temps_c), 2)}}
    for d in deltas:
        res = fp.evaluate(inp, [float(d)] * len(inp.blocks))
        end_gap = res.temps_c[-1] - base.temps_c[-1]
        energy_h = sum(res.energy_kwh) - sum(base.energy_kwh)
        out[f"{d:+d}"] = {
            "cost_day_delta_pln": round((res.cost_pln - base.cost_pln - end_gap * debt_kwh_per_k * inp.offpeak_price) * day, 3),
            "energy_day_delta_kwh": round((energy_h - end_gap * debt_kwh_per_k) * day, 3),
            "energy_horizon_delta_kwh": round(energy_h * day, 3),
            "end_temp_delta_c": round(end_gap, 2),
            "mean_temp_delta_c": round(fp.mean_temp(res) - fp.mean_temp(base), 2),
            "min_temp_c": round(min(res.temps_c), 2),
        }
    return out


def _effects(snap: Snapshot, delta_key: str, lens: str) -> dict:
    eff = dict((snap.uniform or {}).get(delta_key) or {})
    if snap.time_shift and snap.time_shift.get(lens) is not None:
        eff["time_shift_pln_day"] = snap.time_shift[lens]
    return eff


def analyze(snap: Snapshot) -> list[Draft]:
    if snap.curve_on is not True:
        return []
    cur = snap.params.get(KEY)
    if cur is None or snap.avg24_c is None or snap.cold24_c is None:
        return []
    evidence = {"avg24_c": round(snap.avg24_c, 2), "target_c": snap.target_c,
                "cold24_c": round(snap.cold24_c, 2), "coldest_room": snap.coldest_name,
                "room_min_c": snap.room_min_c, "model_identified": snap.model_identified}
    room = snap.coldest_name or "najzimniejszy pokój"
    drafts: list[Draft] = []

    cooling = abs(((snap.uniform or {}).get("-1") or {}).get("end_temp_delta_c", FALLBACK_COOLING_C))
    evidence["cooling_after_c"] = round(cooling, 2)
    if snap.avg24_c >= snap.target_c + DOWN_AVG_MARGIN_C and snap.cold24_c - cooling >= snap.room_min_c + DOWN_MIN_MARGIN_C:
        to = cur - 1
        if catalog.check_class_a(KEY, cur, to)[0]:
            drafts.append(Draft(
                analyzer="curve", dedupe_key=f"curve:{KEY}", lens=LENS_ECONOMY, kind=KIND_EXPERIMENT,
                param_key=KEY, from_value=cur, to_value=to, ttl_h=TTL_H,
                confidence=CONF_MEDIUM if snap.model_identified else CONF_LOW,
                reason=(f"Dom ma zapas komfortu: średnia stref z 24 h to {snap.avg24_c:.1f}°C (cel {snap.target_c:.1f}°C), "
                        f"a {room} ma w 5. percentylu {snap.cold24_c:.1f}°C (po przewidywanym ostygnięciu o {cooling:.1f}°C "
                        f"nadal powyżej minimum {snap.room_min_c:.1f}°C). Eksperyment na 24–48 h: przesunięcie krzywej z {cur:g} na {to:g}. Skutków nie da się jeszcze "
                        "przewidzieć pewnie — zatwierdzony eksperyment da doradcy pomiar reakcji domu."),
                evidence=evidence, effects=_effects(snap, "-1", LENS_ECONOMY)))
    elif snap.avg24_c <= snap.target_c - UP_AVG_MARGIN_C or snap.cold24_c < snap.room_min_c + UP_MIN_MARGIN_C:
        to = cur + 1
        if catalog.check_class_a(KEY, cur, to)[0]:
            why = (f"{room} ma w 5. percentylu {snap.cold24_c:.1f}°C, blisko minimum {snap.room_min_c:.1f}°C"
                   if snap.cold24_c < snap.room_min_c + UP_MIN_MARGIN_C
                   else f"średnia stref z 24 h to {snap.avg24_c:.1f}°C, poniżej celu {snap.target_c:.1f}°C")
            drafts.append(Draft(
                analyzer="curve", dedupe_key=f"curve:{KEY}", lens=LENS_COMFORT, kind=KIND_CHANGE,
                param_key=KEY, from_value=cur, to_value=to, ttl_h=TTL_H, confidence=CONF_MEDIUM,
                reason=f"Za zimno: {why}. Podniesienie przesunięcia krzywej z {cur:g} na {to:g}.",
                evidence=evidence, effects=_effects(snap, "+1", LENS_COMFORT)))
    return drafts
