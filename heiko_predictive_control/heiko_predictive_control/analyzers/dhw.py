"""Analizator CWU — histereza CWU (parametr klasy A) i wskazówka o porze CWU względem taryfy.

Nastawę CWU (48↔58 wg nadwyżki PV), grzałki i anty-legionellę prowadzą automatyzacje HA, więc o nich
analizator mówi tylko tekstem (kind=alert, bez wartości do zapisu). Zapisać można co najwyżej histerezę."""
from __future__ import annotations

from .. import catalog
from . import CONF_LOW, KIND_ALERT, KIND_CHANGE, LENS_ECONOMY, Draft, Snapshot

KEY_HYSTERESIS = "dhw_restart_dt"
MIN_DAYS = 5                     # tyle dób streszczeń, żeby ufać średniej liczby cykli
MANY_CYCLES_PER_DAY = 4.0
PEAK_HINT_SHARE = 0.4            # od tylu procent czasu CWU w szczycie G12w pokazujemy wskazówkę
PEAK_HINT_MIN_SAMPLES = 20       # próbek trybu pracy (15 min) w oknie
TTL_H = 72                       # decyzja 4: histerezy/CWU/P0 72 h


def analyze(snap: Snapshot) -> list[Draft]:
    drafts: list[Draft] = []
    days = [d["modes"] for d in snap.summaries.values() if d.get("modes")]
    cur = snap.params.get(KEY_HYSTERESIS)
    if len(days) >= MIN_DAYS and cur is not None:
        cycles = sum(d.get("dhw_cycles") or 0 for d in days)
        minutes = sum(d.get("dhw_min") or 0 for d in days)
        per_day = cycles / len(days)
        if per_day >= MANY_CYCLES_PER_DAY:
            to = cur + 1
            if catalog.check_class_a(KEY_HYSTERESIS, cur, to)[0]:
                drafts.append(Draft(
                    analyzer="dhw", dedupe_key=f"dhw:{KEY_HYSTERESIS}", lens=LENS_ECONOMY, kind=KIND_CHANGE,
                    param_key=KEY_HYSTERESIS, from_value=cur, to_value=to, ttl_h=TTL_H, confidence=CONF_LOW,
                    reason=(f"Średnio {per_day:.1f} cykli CWU na dobę (średni cykl {minutes / cycles:.0f} min). "
                            f"Większa histereza CWU (z {cur:g} na {to:g} K) ogranicza liczbę startów kosztem większej "
                            "huśtawki temperatury wody w zasobniku."),
                    evidence={"days": len(days), "cycles_per_day": round(per_day, 2),
                              "mean_cycle_min": round(minutes / cycles, 1)}))
    peak = snap.dhw_peak or {}
    if (peak.get("samples") or 0) >= PEAK_HINT_MIN_SAMPLES and (peak.get("share") or 0) >= PEAK_HINT_SHARE:
        drafts.append(Draft(
            analyzer="dhw", dedupe_key="dhw:peak_hint", lens=LENS_ECONOMY, kind=KIND_ALERT,
            param_key="dhw_setpoint", ttl_h=TTL_H, confidence=CONF_LOW,
            reason=(f"{peak['share'] * 100:.0f}% czasu podgrzewania CWU przypada na szczyt G12w. Nastawą CWU zarządzają "
                    "automatyzacje HA, więc doradca niczego nie zmienia — jeśli chcesz, przesuń podgrzew CWU poza szczyt "
                    "w automatyzacji (progi 48/58°C)."),
            evidence={"peak_share": round(peak["share"], 3), "samples": peak["samples"]}))
    return drafts
