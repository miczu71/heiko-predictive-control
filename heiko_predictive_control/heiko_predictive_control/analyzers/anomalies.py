"""Analizator anomalii — same alerty, bez propozycji zmian nastaw.

Dwa źródła: (1) czujniki ostrzegawcze z pakietu HA `heatpump_efficiency` (dryf przegrzania, sufit Td, EEV,
Pd/Ps, skok grzałki) — ich logiki nie liczymy drugi raz; (2) skoki w własnych streszczeniach dobowych
(krótkie cykle sprężarki, minuty grzałek AH i HBH, cykle CWU, impulsy P0) wobec mediany z poprzednich dób (odporny
z-score: mediana i MAD)."""
from __future__ import annotations

from statistics import median

from . import CONF_HIGH, CONF_LOW, KIND_ALERT, LENS_BOTH, Draft, Snapshot

MIN_HISTORY_DAYS = 7
HISTORY_DAYS = 14
Z_LIMIT = 3.5
TTL_SENSOR_H = 24
TTL_SUMMARY_H = 72

# (temat, pole, opis, minimalny bezwzględny przyrost, żeby w ogóle alarmować)
METRICS: tuple[tuple[str, str, str, float], ...] = (
    ("compressor", "short_cycles", "krótkie cykle sprężarki", 3),
    ("backup", "hbh_min", "praca grzałki HBH (min)", 10),
    ("backup", "ah_min", "praca grzałki AH (min)", 10),
    ("modes", "dhw_cycles", "cykle CWU", 2),
    ("p0", "pulses", "impulsy pompy obiegowej P0", 40),
)


def robust_threshold(history: list[float], min_delta: float) -> float:
    """Wartość, powyżej której dobę uznajemy za anomalię: mediana + max(minimalny przyrost, 3,5 · 1,4826 · MAD)."""
    med = median(history)
    mad = median(abs(x - med) for x in history)
    return med + max(min_delta, Z_LIMIT * 1.4826 * mad)


def analyze(snap: Snapshot) -> list[Draft]:
    drafts: list[Draft] = []
    for entity, name in snap.alerts_on:
        drafts.append(Draft(
            analyzer="anomaly", dedupe_key=f"anomaly:{entity}", lens=LENS_BOTH, kind=KIND_ALERT, ttl_h=TTL_SENSOR_H,
            confidence=CONF_HIGH, reason=f"Czujnik ostrzegawczy „{name}” jest aktywny (pakiet heatpump_efficiency w HA).",
            evidence={"entity": entity}))
    days = sorted(snap.summaries)
    if len(days) < MIN_HISTORY_DAYS + 1:
        return drafts
    last, history_days = days[-1], days[-1 - HISTORY_DAYS:-1]
    for topic, field, label, min_delta in METRICS:
        value = (snap.summaries[last].get(topic) or {}).get(field)
        history = [v for d in history_days if (v := (snap.summaries[d].get(topic) or {}).get(field)) is not None]
        if value is None or len(history) < MIN_HISTORY_DAYS:
            continue
        limit = robust_threshold(history, min_delta)
        if value > limit:
            drafts.append(Draft(
                analyzer="anomaly", dedupe_key=f"anomaly:{topic}.{field}", lens=LENS_BOTH, kind=KIND_ALERT,
                ttl_h=TTL_SUMMARY_H, confidence=CONF_LOW,
                reason=(f"Doba {last}: {label} = {value:g}, zwykle około {median(history):g} (próg alarmu {limit:.1f}). "
                        "Sprawdź, czy to CWU 58°C, mróz albo usterka."),
                evidence={"day": last, "value": value, "median": median(history), "limit": round(limit, 2),
                          "history_days": len(history)}))
    return drafts
