"""Analizator „pompa obiegowa P0” — propozycje nastawy czas pracy/postój (1/6 albo 5/5) i testu A/B.

Doradca niczego nie zapisuje: „zmiana” to sugestia, którą user wykonuje sam w HA; zapis do pompy robi tylko
uruchomiony przez usera test (experiments). Reguły (od najwyższego priorytetu):
  • krzywa wyłączona (lato) i P0 ≠ 1/6 → zmiana na 1/6 (P0 5/5 kosztuje ok. 15 W bez korzyści, gdy podłogówka nie grzeje);
  • krzywa włączona, jest werdykt testu, a 3-dobowa średnia temp. zewn. wyszła poza pasmo testu ±3°C → propozycja
    ponownego testu (werdykt jest nieaktualny, więc nie proponujemy na jego podstawie zmiany);
  • krzywa włączona, werdykt aktualny i P0 ≠ werdykt → zmiana na werdykt;
  • krzywa włączona, brak werdyktu → propozycja testu p0_ab (o ile da się go uruchomić: D4b zakończony, P0 = 1/6 albo 5/5).
W trakcie testu analizator milczy (nastawą steruje test)."""
from __future__ import annotations

from .. import catalog
from . import CONF_LOW, CONF_MEDIUM, KIND_CHANGE, KIND_TEST, LENS_BOTH, Draft, Snapshot

RUN_KEY, STOP_KEY = "p0_run_time", "p0_stop_time"
DEFAULT_PAIR = (1.0, 6.0)
PAIRS = {"1/6": (1.0, 6.0), "5/5": (5.0, 5.0)}
WEATHER_BAND_C = 3.0                   # jak P0_WEATHER_BAND_C w experiments: pasmo temp. zewn. ważności werdyktu
TTL_H = 24                             # ustawienie rzadko zmienne — doba wystarczy na reakcję usera


def _label(pair: tuple[float, float]) -> str:
    return f"{pair[0]:g}/{pair[1]:g}"


def _change(cur: tuple[float, float], target: tuple[float, float], reason: str, confidence: str,
            evidence: dict) -> list[Draft]:
    if not catalog.check_class_a(RUN_KEY, cur[0], target[0])[0]:
        return []
    return [Draft(
        analyzer="p0", dedupe_key="p0:pair", lens=LENS_BOTH, kind=KIND_CHANGE, param_key=RUN_KEY,
        from_value=cur[0], to_value=target[0], ttl_h=TTL_H, confidence=confidence,
        reason=f"{reason} Ustaw czas pracy/postoju P0 na {_label(target)} (z {_label(cur)}) w HA — doradca niczego nie zmienia sam.",
        evidence={**evidence, "from_stop": cur[1], "to_stop": target[1]})]


def _test(reason: str, evidence: dict) -> list[Draft]:
    return [Draft(
        analyzer="p0", dedupe_key="p0:test", lens=LENS_BOTH, kind=KIND_TEST, ttl_h=TTL_H, confidence=CONF_LOW,
        evidence={**evidence, "test_kind": "p0_ab"},
        reason=f"{reason} Test „Pompa obiegowa P0: 1/6 vs 5/5” trwa 6 dób (zmiana o północy), uruchamiasz go w zakładce Testy; "
               "add-on pilnuje komfortu i po teście wraca do wartości wyjściowej.")]


def analyze(snap: Snapshot) -> list[Draft]:
    run, stop = snap.params.get(RUN_KEY), snap.params.get(STOP_KEY)
    if run is None or stop is None or snap.test_running or snap.curve_on is None:
        return []
    cur = (run, stop)
    evidence = {"p0": _label(cur), "curve_on": snap.curve_on, "outdoor3d_c": snap.outdoor3d_c}
    if not snap.curve_on:
        if cur == DEFAULT_PAIR:
            return []
        return _change(cur, DEFAULT_PAIR, "Krzywa grzewcza jest wyłączona, więc P0 5/5 tylko zużywa prąd (ok. 15 W) "
                       "bez poprawy rozkładu ciepła.", CONF_MEDIUM, evidence)
    verdict = snap.p0_verdict
    if verdict and verdict.get("verdict") in PAIRS:
        evidence = {**evidence, "verdict": verdict["verdict"], "test_outdoor_c": verdict.get("outdoor_c"),
                    "test_confidence": verdict.get("confidence")}
        test_out = verdict.get("outdoor_c")
        if snap.outdoor3d_c is not None and test_out is not None and abs(snap.outdoor3d_c - test_out) > WEATHER_BAND_C:
            if not snap.p0_test_ready:
                return []
            return _test(f"Ostatni test P0 robiono przy średniej {test_out:.1f}°C, a ostatnie 3 doby to {snap.outdoor3d_c:.1f}°C "
                         f"— werdykt {verdict['verdict']} jest poza pasmem ±{WEATHER_BAND_C:g}°C.", evidence)
        target = PAIRS[verdict["verdict"]]
        if cur != target:
            return _change(cur, target, f"Test A/B wskazał {verdict['verdict']} (pewność {verdict.get('confidence') or '—'}).",
                           CONF_MEDIUM if verdict.get("confidence") == "wysoka" else CONF_LOW, evidence)
        return []
    if snap.p0_test_ready and cur in PAIRS.values():
        return _test("Nie wiadomo, czy P0 5/5 wyrównuje temperatury pokoi lepiej niż 1/6 — za to kosztuje ok. 15 W.", evidence)
    return []
