"""Analizatory doradcy — funkcje czyste na syntetycznej migawce (bez HA, bez bazy)."""
from datetime import datetime, timedelta

import pytest

from heiko_predictive_control import floor_plan as fp
from heiko_predictive_control.analyzers import Snapshot, anomalies, curve, dhw
from heiko_predictive_control.floor_model import FloorModel

NOW = datetime(2026, 1, 14, 7, 0)
UNIFORM = {"-1": {"cost_day_delta_pln": -0.4, "energy_day_delta_kwh": -0.5, "mean_temp_delta_c": -0.2, "min_temp_c": 20.0},
           "+1": {"cost_day_delta_pln": 0.4, "energy_day_delta_kwh": 0.5, "mean_temp_delta_c": 0.2, "min_temp_c": 21.0}}


def snap(**kw) -> Snapshot:
    base = dict(now=NOW, target_c=20.6, room_min_c=18.5, avg24_c=21.2, cold24_c=20.2, coldest_name="Pokój 1",
                curve_on=True, params={"curve_shift": 0.0}, uniform=UNIFORM, time_shift={"ekonomia": 0.3, "komfort": 0.1})
    base.update(kw)
    return Snapshot(**base)


# ── krzywa ───────────────────────────────────────────────────────────────────

def test_curve_slack_proposes_experiment_down_with_effects_and_low_confidence():
    (d,) = curve.analyze(snap())
    assert (d.kind, d.lens, d.param_key, d.from_value, d.to_value) == ("eksperyment", "ekonomia", "curve_shift", 0.0, -1.0)
    assert d.confidence == "niska" and d.ttl_h == 6
    assert d.effects["cost_day_delta_pln"] == -0.4 and d.effects["time_shift_pln_day"] == 0.3
    assert d.evidence["avg24_c"] == 21.2 and "Pokój 1" in d.reason
    assert d.evidence["cooling_after_c"] == curve.FALLBACK_COOLING_C          # UNIFORM w teście nie ma end_temp_delta_c


def test_curve_identified_model_raises_confidence():
    (d,) = curve.analyze(snap(model_identified=True))
    assert d.confidence == "średnia"


@pytest.mark.parametrize("kw", [
    dict(curve_on=False), dict(curve_on=None), dict(avg24_c=None), dict(cold24_c=None), dict(params={}),
    dict(avg24_c=20.8),                              # średnia za blisko celu
    dict(cold24_c=18.9),                             # najzimniejszy pokój za blisko minimum
])
def test_curve_no_proposal_without_slack_or_when_curve_off(kw):
    assert curve.analyze(snap(**kw)) == []


def test_curve_down_needs_room_margin_after_predicted_cooling():
    """Zapas nad minimum liczy się PO ostygnięciu domu wg planera: 19,5 − 0,65 = 18,85 < 19,0 → brak; 19,7 → jest."""
    uniform = {**UNIFORM, "-1": {**UNIFORM["-1"], "end_temp_delta_c": -0.65}}
    assert curve.analyze(snap(cold24_c=19.5, uniform=uniform)) == []
    (d,) = curve.analyze(snap(cold24_c=19.7, uniform=uniform))
    assert d.to_value == -1.0 and d.evidence["cooling_after_c"] == 0.65 and "po przewidywanym ostygnięciu" in d.reason


def test_curve_never_proposes_down_when_the_result_would_immediately_call_for_up():
    """Ping-pong: po −1 (średnia − mean_temp_delta, najzimniejszy pokój − ostygnięcie) reguła nie może chcieć +1."""
    uniform = {**UNIFORM, "-1": {**UNIFORM["-1"], "end_temp_delta_c": -0.65, "mean_temp_delta_c": -0.38}}
    checked = 0
    for avg in [20.0 + 0.1 * i for i in range(0, 30)]:
        for cold in [18.0 + 0.1 * i for i in range(0, 30)]:
            if not any(d.to_value == -1.0 for d in curve.analyze(snap(avg24_c=avg, cold24_c=cold, uniform=uniform))):
                continue                                                        # interesują nas tylko propozycje „−1”
            checked += 1
            after = curve.analyze(snap(avg24_c=avg - 0.38, cold24_c=cold - 0.65, uniform=uniform, params={"curve_shift": -1.0}))
            assert not any(d.to_value == 0.0 for d in after), (avg, cold)       # nie proponuje powrotu w górę
            assert not any(d.kind == "zmiana" and d.lens == "komfort" for d in after), (avg, cold)
    assert checked > 20                                                         # siatka faktycznie trafia w obszar „−1”


def test_curve_too_cold_room_proposes_plus_one_for_comfort():
    (d,) = curve.analyze(snap(avg24_c=20.7, cold24_c=18.6))
    assert (d.kind, d.lens, d.to_value) == ("zmiana", "komfort", 1.0) and d.confidence == "średnia"
    assert "blisko minimum" in d.reason and d.effects["mean_temp_delta_c"] == 0.2


def test_curve_average_below_target_proposes_plus_one():
    (d,) = curve.analyze(snap(avg24_c=20.2, cold24_c=19.8))
    assert d.to_value == 1.0 and "poniżej celu" in d.reason


def test_curve_respects_class_a_range():
    assert curve.analyze(snap(params={"curve_shift": -4.0})) == []                    # −5 poza zakresem klasy A
    assert curve.analyze(snap(avg24_c=20.0, cold24_c=18.0, params={"curve_shift": 4.0})) == []   # +5 poza zakresem


def test_uniform_effects_cheaper_and_colder_when_water_setpoint_lowered():
    t0 = datetime(2026, 1, 14, 7, 0)
    n = int(36 / fp.STEP_H)
    blocks = fp.build_blocks(t0, n, lambda d: d.weekday() < 5, 6, 22, 1.2304, 0.6306)
    inp = fp.PlanInputs(model=FloorModel(), tr0_c=20.8, qf0_kw=1.0, t_out_c=[2.0] * n, base_c=[26.0] * n, blocks=blocks,
                        bands=[(19.6, 21.6)] * n, target_c=20.6)
    eff = curve.uniform_effects(inp)
    assert eff["-1"]["energy_day_delta_kwh"] < 0 < eff["+1"]["energy_day_delta_kwh"]
    assert eff["-1"]["cost_day_delta_pln"] < 0 < eff["+1"]["cost_day_delta_pln"]
    assert eff["-1"]["mean_temp_delta_c"] < 0 < eff["+1"]["mean_temp_delta_c"]
    assert eff["horizon_h"] == 36.0


def test_uniform_effects_price_the_heat_debt_so_saving_is_not_overstated():
    """Obniżenie nastawy oszczędza w horyzoncie głównie ciepło z zasobnika domu; po wycenie długu efekt jest
    kilkukrotnie mniejszy niż surowa różnica energii i zależy od pogody (regresja: 0.9.0 pokazywało stałe ~1,8 kWh/dobę)."""
    t0 = datetime(2026, 1, 14, 7, 0)
    n = int(36 / fp.STEP_H)
    blocks = fp.build_blocks(t0, n, lambda d: d.weekday() < 5, 6, 22, 1.2304, 0.6306)
    model = FloorModel(tau_h=1.5, g=0.04721, c=0.006, e=0.70902)

    def effects(out_c, base_c):
        inp = fp.PlanInputs(model=model, tr0_c=21.5, qf0_kw=max(0.0, model.c * (21.5 - out_c) / model.g), t_out_c=[out_c] * n,
                            base_c=[base_c] * n, blocks=blocks, bands=[(19.6, 21.6)] * n, target_c=20.6)
        return curve.uniform_effects(inp)["-1"]
    mild, cold = effects(8.0, 23.5), effects(-5.0, 26.5)
    for eff in (mild, cold):
        assert eff["end_temp_delta_c"] < 0                                           # dom kończy horyzont chłodniejszy
        assert eff["energy_horizon_delta_kwh"] < eff["energy_day_delta_kwh"] < 0     # dług zmniejsza „oszczędność”, ale nie znosi jej
        assert abs(eff["energy_day_delta_kwh"]) < 0.5 * abs(eff["energy_horizon_delta_kwh"])
    assert cold["energy_day_delta_kwh"] < mild["energy_day_delta_kwh"]               # w mrozie oszczędność jest większa


# ── CWU ──────────────────────────────────────────────────────────────────────

def _days(n, cycles, minutes=50.0):
    return {(NOW.date() - timedelta(days=i)).isoformat(): {"modes": {"dhw_cycles": cycles, "dhw_min": minutes * cycles}}
            for i in range(1, n + 1)}


def test_dhw_many_cycles_propose_higher_hysteresis():
    (d,) = dhw.analyze(snap(summaries=_days(6, 5), params={"dhw_restart_dt": 5.0}))
    assert (d.param_key, d.from_value, d.to_value, d.kind) == ("dhw_restart_dt", 5.0, 6.0, "zmiana")
    assert d.ttl_h == 72 and d.evidence["cycles_per_day"] == 5.0


@pytest.mark.parametrize("kw", [
    dict(summaries=_days(4, 6), params={"dhw_restart_dt": 5.0}),        # za mało dób
    dict(summaries=_days(6, 2), params={"dhw_restart_dt": 5.0}),        # mało cykli
    dict(summaries=_days(6, 6), params={}),                              # brak parametru w HA
    dict(summaries=_days(6, 6), params={"dhw_restart_dt": 10.0}),       # sufiks zakresu klasy A
])
def test_dhw_no_hysteresis_proposal(kw):
    assert dhw.analyze(snap(**kw)) == []


def test_dhw_peak_hint_is_text_only_about_managed_setpoint():
    (d,) = dhw.analyze(snap(dhw_peak={"share": 0.55, "samples": 40}))
    assert d.kind == "alert" and d.to_value is None and d.param_key == "dhw_setpoint"
    assert dhw.analyze(snap(dhw_peak={"share": 0.55, "samples": 10})) == []
    assert dhw.analyze(snap(dhw_peak={"share": 0.2, "samples": 100})) == []


# ── anomalie ─────────────────────────────────────────────────────────────────

def _series(values, topic="backup", field="hbh_min"):
    start = NOW.date() - timedelta(days=len(values))
    return {(start + timedelta(days=i)).isoformat(): {topic: {field: v}} for i, v in enumerate(values)}


def test_anomaly_flags_a_spike_against_median_and_mad():
    (d,) = anomalies.analyze(snap(summaries=_series([0, 0, 0, 0, 0, 0, 0, 0, 60])))
    assert d.kind == "alert" and d.lens == "obie" and d.dedupe_key == "anomaly:backup.hbh_min"
    assert d.evidence["value"] == 60 and d.evidence["median"] == 0


def test_anomaly_ignores_normal_variation_and_short_history():
    assert anomalies.analyze(snap(summaries=_series([30, 32, 28, 31, 29, 33, 30, 34]))) == []
    assert anomalies.analyze(snap(summaries=_series([0, 0, 0, 60]))) == []            # za krótka historia
    assert anomalies.analyze(snap(summaries=_series([0, 0, 0, 0, 0, 0, 0, 0, 5]))) == []   # poniżej minimalnego przyrostu


def test_anomaly_reports_active_warning_sensors():
    (d,) = anomalies.analyze(snap(alerts_on=[("binary_sensor.eev", "EEV zablokowany")]))
    assert d.confidence == "wysoka" and "EEV zablokowany" in d.reason and d.ttl_h == 24


def test_robust_threshold_scales_with_noise():
    quiet = anomalies.robust_threshold([10, 10, 10, 10, 10, 10, 10], 3)
    noisy = anomalies.robust_threshold([2, 18, 5, 15, 8, 12, 10], 3)
    assert quiet == 13 and noisy > quiet


def test_hwtbh_counter_is_not_an_alarm_source_the_heater_does_not_exist():
    """Licznik HWTBH liczy urojone minuty (do 94/dobę) — nie może wywoływać alarmu „grzałka pracuje”."""
    from heiko_predictive_control.analyzers import anomalies
    assert not [m for m in anomalies.METRICS if m[1] == "hwtbh_min"]
    assert {m[1] for m in anomalies.METRICS if m[0] == "backup"} == {"hbh_min", "ah_min"}
