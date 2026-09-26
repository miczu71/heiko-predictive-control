"""Analizatory doradcy (D2) — funkcje czyste: migawka danych -> szkice propozycji.

Analizator niczego nie zapisuje i nie czyta z HA: dostaje `Snapshot` (zbudowany przez `advisor`),
zwraca listę `Draft`. Decyzja, co z tym zrobić (deduplikacja, TTL, powiadomienie), należy do silnika."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

LENS_COMFORT, LENS_ECONOMY, LENS_BOTH = "komfort", "ekonomia", "obie"
KIND_CHANGE, KIND_EXPERIMENT, KIND_ALERT = "zmiana", "eksperyment", "alert"
CONF_LOW, CONF_MEDIUM, CONF_HIGH = "niska", "średnia", "wysoka"


@dataclass
class Snapshot:
    now: datetime
    target_c: float                                   # cel temperatury stref
    room_min_c: float                                 # minimum pokoju (bezpiecznik)
    avg24_c: float | None = None                      # średnia stref z 24 h
    cold24_c: float | None = None                     # 5. percentyl najzimniejszego z pokoi wybranych do minimum (24 h)
    coldest_name: str | None = None
    curve_on: bool | None = None
    params: dict[str, float] = field(default_factory=dict)      # bieżące wartości liczbowych parametrów katalogu
    managed: frozenset[str] = frozenset()             # parametry prowadzone przez automatyzacje HA
    model_identified: bool = False
    uniform: dict | None = None                       # skutki jednolitego przesunięcia krzywej (z planu), klucze "-1", "+1"
    time_shift: dict | None = None                    # oszczędność z samego przesuwania w czasie, zł/dobę per soczewka
    summaries: dict[str, dict[str, dict]] = field(default_factory=dict)   # doba -> temat -> dane
    dhw_peak: dict | None = None                      # {"share": udział CWU w szczycie, "samples": liczba próbek}
    alerts_on: list[tuple[str, str]] = field(default_factory=list)        # (entity_id, nazwa) aktywnych czujników ostrzeżeń


@dataclass
class Draft:
    analyzer: str                                     # curve | dhw | anomaly
    dedupe_key: str                                   # ta sama para = ta sama propozycja (aktualizacja zamiast duplikatu)
    lens: str
    kind: str
    reason: str
    confidence: str
    ttl_h: int
    param_key: str | None = None
    from_value: float | None = None
    to_value: float | None = None
    evidence: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)
