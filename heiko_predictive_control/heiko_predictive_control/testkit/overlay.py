"""Nakładka ustawień na bieżącą dobę testu (czysta funkcja, bez I/O: nie zna bazy ani zegara)."""
from __future__ import annotations

from .params import OVERRIDABLE


def effective_settings(settings: dict, active: dict | None) -> dict:
    """Zwraca NOWĄ kopię `settings` z nałożonymi `overrides` z `active` (`{"overrides": {...}}` albo None).

    Klucze spoza `OVERRIDABLE` są pomijane i wymieniane w `_overlay_rejected` (obrona w głębi, mimo walidacji).
    """
    out = dict(settings)
    overrides = active.get("overrides") if isinstance(active, dict) else None
    if not isinstance(overrides, dict) or not overrides:
        return out
    rejected = []
    for key, value in overrides.items():
        if key in OVERRIDABLE:
            out[key] = value
        else:
            rejected.append(key)
    if rejected:
        out["_overlay_rejected"] = rejected
    return out
