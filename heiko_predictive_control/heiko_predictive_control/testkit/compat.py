"""Adapter kształtu starych wyników (`legacy_oracle._p0_result` / `_result`) na katalog miar — dla E4 i odznak.

Werdykt P0 jest asymetryczny (5/5 albo 1/6) i liczony wzorem legacy 1:1, nie przez `verdict.decide`.
"""
from __future__ import annotations

import json
from datetime import datetime

from .. import experiments as ex
from .measures import CATALOG, Ctx, arm_days_from_steps, outdoor_c


def p0_result(conn, settings: dict, test, now: datetime, day_kwh=None) -> dict:
    ctx = Ctx(conn, settings, now, test, day_kwh)
    steps = json.loads(test["schedule"])
    by_arm: dict[str, list] = {}
    for d in arm_days_from_steps(steps, lambda s: ex._fmt((s["value"], s["stop"]))):
        by_arm.setdefault(d.arm, []).append(d)
    variants = {}
    for label, days in by_arm.items():
        spread, kwh = CATALOG["zone_spread"].fn(ctx, days), CATALOG["kwh_day"].fn(ctx, days)
        variants[label] = {"days": len(days), "spread_c": spread["spread_c"], "zones_avg_c": spread["zones_avg_c"],
                           "samples": spread["samples"], "kwh_day": kwh["value"], "outdoor_c": outdoor_c(ctx, days)}
    a, b = variants.get("1/6"), variants.get("5/5")
    result: dict = {"variants": variants, "verdict": None, "confidence": None}
    if a and b and a["spread_c"] is not None and b["spread_c"] is not None:
        gain = round(a["spread_c"] - b["spread_c"], 2)
        result["gain_c"] = gain
        result["verdict"] = "5/5" if gain >= ex.P0_MIN_GAIN_C and b["zones_avg_c"] >= a["zones_avg_c"] - ex.P0_AVG_TOLERANCE_C \
            else "1/6"
        if a["kwh_day"] is not None and b["kwh_day"] is not None:
            result["extra_kwh_day"] = round(b["kwh_day"] - a["kwh_day"], 2)
        weather = a["outdoor_c"] is not None and b["outdoor_c"] is not None
        result["confidence"] = "niska" if not weather or abs(a["outdoor_c"] - b["outdoor_c"]) > ex.P0_WEATHER_BAND_C \
            else "wysoka"
        result["outdoor_c"] = round((a["outdoor_c"] + b["outdoor_c"]) / 2, 1) if weather else None
    return result


def zapis_result(conn, test) -> dict:
    written = CATALOG["writes_confirmed"].fn(Ctx(conn, {}, None, test))
    return {k: written[k] for k in ("writes", "confirmed", "max_confirm_min")}


def inertia_result(conn, settings: dict, test, now: datetime) -> dict:
    ctx = Ctx(conn, settings, now, test)
    result = zapis_result(conn, test)
    exc = CATALOG["excitation"].fn(ctx)
    result["excitation"] = {**exc["detail"], "sufficient": exc["value"]}
    result["refit"] = CATALOG["inertia_refit"].fn(ctx)
    return result
