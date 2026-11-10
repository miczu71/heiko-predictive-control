"""Wbudowane definicje testów jako dane (E2): `zapis`, `bezwladnosc`, `p0_ab`.

Teksty (klucz, tytuł, opis) i stałe są importowane z `experiments`, nie kopiowane. Definicje muszą przechodzić
`schema.validate` i odtwarzać harmonogramy `experiments.build_schedule` (złote testy w tests/test_testkit_schedule.py).
"""
from __future__ import annotations

from .. import experiments as ex


def _span(a: int, b: int) -> str:
    return f"{a:02d}:00→{b:02d}:00"


BUILTIN: dict[str, dict] = {
    "zapis": {
        "key": "zapis",
        "title": ex.KINDS["zapis"].title,
        "description": ex.KINDS["zapis"].description,
        "requires": {"curve_on": False, "heiko_enabled": True},
        "days": {"filter": "all", "count": 1},
        "design": "single",
        "arms": {"A": {"writes": [{"param": "curve_shift", "value": "base~1",
                                   "from": "+0m", "to": f"+{ex.CONFIRM_MIN}m"}]}},
        "measures": ["writes_confirmed"],
        "verdict": {"primary": "writes_confirmed", "better": "higher"},
    },
    "bezwladnosc": {
        "key": "bezwladnosc",
        "title": ex.KINDS["bezwladnosc"].title,
        "description": ex.KINDS["bezwladnosc"].description,
        "requires": {"curve_on": True, "heiko_enabled": True},
        "days": {"filter": "workday", "count": ex.INERTIA_DAYS},
        "design": "single",
        "windows": ["zone:peak"],
        "windows_fallback": [_span(a, b) for a, b in ex.INERTIA_WINDOWS],
        "min_window_h": ex.INERTIA_MIN_WINDOW_H,
        "arms": {"A": {"writes": [{"param": "curve_shift", "value": "base-1"}]}},
        "measures": ["excitation", "inertia_refit"],
        "verdict": {"primary": "excitation", "better": "higher"},
    },
    "p0_ab": {
        "key": "p0_ab",
        "title": ex.KINDS["p0_ab"].title,
        "description": ex.KINDS["p0_ab"].description,
        "requires": {"curve_on": True, "heiko_enabled": True, "after": ["bezwladnosc"]},
        "days": {"filter": "all", "count": ex.P0_DAYS},
        "design": "alternate",
        "order": ["A", "B"],
        "arms": {
            "A": {"writes": [{"param": "p0_run", "value": "base"}, {"param": "p0_stop", "value": "base"}]},
            "B": {"writes": [{"param": "p0_run", "value": "other"}, {"param": "p0_stop", "value": "other"}]},
        },
        "measures": ["zone_spread", "kwh_day"],
        "verdict": {"primary": "zone_spread", "better": "lower", "min_gain": ex.P0_MIN_GAIN_C,
                    "weather_band_c": ex.P0_WEATHER_BAND_C, "min_n": ex.P0_DAYS // 2},
    },
}
