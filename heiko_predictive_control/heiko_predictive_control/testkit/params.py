"""Rejestr nastaw pompy i opcji, które definicja testu może nadpisać (czysty moduł, bez I/O).

Reguły zapisu są importowane z `experiments._WRITE_RULES` (ta sama funkcja, nie kopia). Zakresy opcji są importowane
z `web.SETTING_RANGES`; wyjątek: `g13_peak_min_room_c` nie ma tam wpisu, więc zakres jest tutaj.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .. import experiments as ex
from ..web import SETTING_RANGES


@dataclass(frozen=True)
class Param:
    suffix: str                          # końcówka `entity_id` (jak w experiments)
    kind: str                            # zawsze "number": `_write` wymaga domeny `number.`
    check: Callable[[float, float], bool]  # (stary, nowy) → dozwolone; ta sama funkcja co w `_write`


PUMP_PARAMS: dict[str, Param] = {
    "curve_shift": Param(ex.ALLOWED_ENTITY_SUFFIX, "number", ex._WRITE_RULES[ex.ALLOWED_ENTITY_SUFFIX]),
    "p0_run": Param(ex.P0_RUN_SUFFIX, "number", ex._WRITE_RULES[ex.P0_RUN_SUFFIX]),
    "p0_stop": Param(ex.P0_STOP_SUFFIX, "number", ex._WRITE_RULES[ex.P0_STOP_SUFFIX]),
    "slot52": Param(ex.SLOT52_SUFFIX, "number", ex._WRITE_RULES[ex.SLOT52_SUFFIX]),
    "dhw": Param(ex.DHW_SUFFIX, "number", ex._WRITE_RULES[ex.DHW_SUFFIX]),
}

_OVERRIDE_KEYS = ("g13_peak_preheat_shift", "g13_peak_peak_shift", "tail_margin_k", "tail_delay_min",
                  "tail_min_pause_min", "tail_min_room_c", "tail_dhw_hold_min")

# Opcje, które definicja może nadpisać w dobie testu, z zakresem (włącznie).
OVERRIDABLE: dict[str, tuple[float, float] | None] = {k: SETTING_RANGES[k] for k in _OVERRIDE_KEYS}
OVERRIDABLE["g13_peak_min_room_c"] = (10, 25)

# Nazwy mierzalnych wielkości (implementacja w E3).
MEASURES = frozenset({"floor_cost", "kwh_day", "min_room_at", "safety_trips", "zone_spread", "excitation",
                      "inertia_refit", "writes_confirmed"})

# Parametry miar: nazwa → {parametr: wymagany?}. Miary spoza słownika nie przyjmują parametrów.
MEASURE_PARAMS: dict[str, dict[str, bool]] = {
    "floor_cost": {"window": True, "per": False},
    "min_room_at": {"at": True},
}
