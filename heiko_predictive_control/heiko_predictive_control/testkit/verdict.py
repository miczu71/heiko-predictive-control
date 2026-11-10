"""Werdykt generyczny testu A/B (czysty moduł, bez I/O): `decide(spec, per_arm)`.

`spec` = `defn["verdict"]` (`primary`, `better`, `min_gain`, `weather_band_c`, `min_n`, `guards`).
`per_arm` = {wariant: {"value", "outdoor_c", "n", "measures": {nazwa: {"value": …}}}}.
"""
from __future__ import annotations

_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}


def _outdoor(per_arm: dict) -> float | None:
    temps = [d.get("outdoor_c") for d in per_arm.values()]
    return round(sum(temps) / len(temps), 1) if temps and None not in temps else None


def _confidence(spec: dict, valid: dict) -> str:
    min_n, band = spec.get("min_n") or 0, spec.get("weather_band_c")
    temps = [d.get("outdoor_c") for d in valid.values()]
    if any((d.get("n") or 0) < min_n for d in valid.values()) or None in temps:
        return "niska"
    if band is not None and abs(temps[0] - temps[1]) > band:
        return "niska"
    return "wysoka"


def decide(spec: dict, per_arm: dict) -> dict:
    """Werdykt A/B. Brak `better` = „higher” (schema `verdict.better` jest wymagane, więc to tylko obrona)."""
    valid = {arm: d for arm, d in per_arm.items() if d.get("value") is not None}
    outdoor = _outdoor(valid) if len(valid) == 2 else None
    if len(valid) != 2:
        return {"winner": None, "gain": None, "confidence": None, "reason": "za mało danych", "outdoor_c": None}
    (arm_a, a), (arm_b, b) = valid.items()
    gain = round(abs(a["value"] - b["value"]), 4)
    confidence = _confidence(spec, valid)
    lower = spec.get("better") == "lower"
    if a["value"] == b["value"]:
        winner = None
    else:
        winner = arm_a if (a["value"] < b["value"]) == lower else arm_b

    def result(win, reason):
        return {"winner": win, "gain": gain, "confidence": confidence, "reason": reason, "outdoor_c": outdoor}

    if winner is None:
        return result(None, "remis")
    if gain < (spec.get("min_gain") or 0):
        return result(None, f"różnica {gain:g} poniżej progu {spec['min_gain']:g}")
    for guard in spec.get("guards") or []:
        value = (valid[winner].get("measures", {}).get(guard["measure"]) or {}).get("value")
        if value is None or not _OPS[guard["op"]](value, guard["value"]):
            shown = "brak wartości" if value is None else f"{value:g}"
            return result(None, f"strażnik {guard['measure']} {guard['op']} {guard['value']:g} niespełniony "
                                f"przez {winner} ({shown})")
    return result(winner, f"{winner} lepszy o {gain:g}")
