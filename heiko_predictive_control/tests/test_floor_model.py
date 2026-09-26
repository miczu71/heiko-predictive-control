import math
import random
from datetime import datetime

import pytest

from heiko_predictive_control import floor_model as fm


def _synthetic(tau=4.0, g=0.3, c=0.06, dt=0.25, n=1200, noise=0.0, seed=1):
    """Dane z znanego modelu: moc grzania włączana blokami (jak taryfa)."""
    rng = random.Random(seed)
    truth = fm.FloorModel(tau_h=tau, g=g, c=c, e=0.5)
    a = 1 - math.exp(-dt / tau)
    tr, qf = 20.0, 2.0
    trs, tos, qs = [], [], []
    for i in range(n):
        hour = (i * dt) % 24
        to = 3.0 + 4.0 * math.sin(2 * math.pi * hour / 24)
        q = 3.5 if (hour < 6 or 13 <= hour < 15 or hour >= 22) else 0.4
        trs.append(tr + rng.gauss(0, noise))
        tos.append(to)
        qs.append(q)
        tr += dt * (truth.g * qf - truth.c * (tr - to))
        qf += a * (q - qf)
    return fm.Segment(trs, tos, qs)


def test_cop_carnot_fraction_and_clipping():
    assert round(fm.cop(0.0, 27.0), 2) == 5.0
    assert fm.cop(20.0, 20.5) == fm.COP_MAX          # prawie zerowy skok
    assert fm.cop(-25.0, 55.0) >= fm.COP_MIN


def test_fit_recovers_known_parameters_without_noise():
    fit = fm.fit_room([_synthetic()], dt_h=0.25)
    assert fit is not None
    assert fit.tau_h == 4.0
    assert abs(fit.g - 0.3) < 0.02
    assert abs(fit.c - 0.06) < 0.005
    assert fit.rmse_c < 0.05
    assert fm.accept_fit(fit)


def test_fit_tolerates_sensor_noise():
    fit = fm.fit_room([_synthetic(noise=0.05)], dt_h=0.25)
    assert fit is not None
    assert fit.tau_h in (3.0, 4.0, 6.0)
    assert abs(fit.g - 0.3) < 0.08
    assert abs(fit.c - 0.06) < 0.02
    assert fm.accept_fit(fit)


def test_fit_pools_segments_and_survives_gap():
    one = _synthetic(n=500)
    two = _synthetic(n=500, seed=2)
    fit = fm.fit_room([one, two], dt_h=0.25)
    assert fit is not None and abs(fit.c - 0.06) < 0.01


def test_fit_needs_enough_data():
    assert fm.fit_room([_synthetic(n=60)], dt_h=0.25) is None


def test_accept_fit_rejects_inaccurate_or_worse_than_persistence():
    bad = fm.FloorModel(rmse_c=0.9, persist_c=1.2)
    assert not fm.accept_fit(bad)
    worse = fm.FloorModel(rmse_c=0.3, persist_c=0.2)
    assert not fm.accept_fit(worse)
    assert not fm.accept_fit(None)


def test_simulate_matches_step_and_cools_without_heat():
    model = fm.FloorModel(tau_h=4.0, g=0.3, c=0.06, e=0.5)
    temps, energy = model.simulate(21.0, 0.0, [5.0] * 8, [5.0] * 8, dt_h=0.5)
    assert all(e == 0 for e in energy)                 # nastawa poniżej pokoju: pompa stoi
    assert temps[-1] < 21.0 and temps == sorted(temps, reverse=True)


def test_simulate_heating_costs_energy_and_warms_with_lag():
    model = fm.FloorModel(tau_h=4.0, g=0.3, c=0.06, e=0.5)
    temps, energy = model.simulate(19.0, 0.0, [2.0] * 60, [27.0] * 60, dt_h=0.25)
    assert sum(energy) > 0
    # bezwładność wylewki: na początku pokój jeszcze stygnie, dopiero potem się grzeje
    assert temps[0] < 19.0
    assert temps[-1] > 19.0


def test_measured_heat_uses_cop_times_electric_power():
    q = fm.measured_heat_kw(el_kwh=0.5, dt_h=0.25, t_out_c=0.0, t_water_c=27.0)
    assert round(q, 2) == round(fm.cop(0.0, 27.0) * 2.0, 2)
    assert fm.measured_heat_kw(0.0, 0.25, 0.0, 27.0) == 0.0
    assert fm.measured_heat_kw(0.5, 0.0, 0.0, 27.0) == 0.0


def test_fit_energy_gain_recovers_slope():
    room = [20.0] * 40
    water = [26.0 + (i % 5) for i in range(40)]
    q = [0.6 * (w - r) for w, r in zip(water, room)]
    e, n = fm.fit_energy_gain(q, room, water)
    assert round(e, 3) == 0.6 and n == 40


def test_fit_energy_gain_needs_heating_samples():
    assert fm.fit_energy_gain([0.0] * 50, [20.0] * 50, [27.0] * 50) is None


def test_from_dict_roundtrip_and_bad_data_falls_back_to_default():
    model = fm.FloorModel(tau_h=6.0, g=0.2, c=0.05, e=0.7, rmse_c=0.21, persist_c=0.4,
                          samples=300, source=fm.SOURCE_SHADOW, balance_r=7.9)
    assert fm.FloorModel.from_dict(model.as_dict()).as_dict() == model.as_dict()
    assert fm.FloorModel.from_dict(None).source == fm.SOURCE_DEFAULT
    assert fm.FloorModel.from_dict({"tau_h": "abc"}).source == fm.SOURCE_DEFAULT


def _closed_loop(tau=4.0, g=0.05, c=0.006, dt=1.0, n=1500, seed=5):
    """Dom regulowany termostatem: moc grzania dopasowuje się do strat — dane, w których
    regresja swobodna jest prawie współliniowa (tak jak w prawdziwej zimie)."""
    rng = random.Random(seed)
    a = fm.alpha(dt, tau)
    tr, qf = 21.0, 2.0
    trs, tos, qs = [], [], []
    for i in range(n):
        to = 3.0 + 5.0 * math.sin(2 * math.pi * i * dt / 24) + 4.0 * math.sin(2 * math.pi * i * dt / 200)
        q = max(0.0, c / g * (tr - to) + 0.15 * (21.0 - tr) / g * c + rng.gauss(0, 0.15))
        trs.append(tr + rng.gauss(0, 0.02))
        tos.append(to)
        qs.append(q)
        tr += dt * (g * qf - c * (tr - to))
        qf += a * (q - qf)
    return fm.Segment(trs, tos, qs)


def test_balance_ratio_is_mean_drive_over_mean_heat():
    seg = fm.Segment(tr=[20.0, 20.0], t_out=[0.0, 10.0], q_kw=[2.0, 1.0])
    assert fm.balance_ratio([seg]) == pytest.approx(15.0 / 1.5)
    assert fm.balance_ratio([fm.Segment([20.0], [19.0], [3.0])]) is None       # napęd < 3 K
    assert fm.balance_ratio([fm.Segment([20.0], [0.0], [0.05])]) is None       # prawie brak grzania


def test_fit_balanced_ties_g_to_c_and_picks_conservative_inertia():
    seg = _closed_loop()
    fit = fm.fit_balanced([seg], dt_h=1.0)
    assert fit is not None and fit.balance_r is not None
    assert fit.g == pytest.approx(fit.balance_r * fit.c)              # spójny bilans energii
    assert fit.rmse_c <= fit.persist_c * 1.2
    # konserwatywnie: nie mniejsze c niż najlepsze na siatce, w zakresie wiarygodnym
    assert fm.C_GRID[0] <= fit.c <= 0.02


def test_fit_best_falls_back_to_balanced_fit_when_free_regression_fails(monkeypatch):
    monkeypatch.setattr(fm, "fit_room", lambda *a, **k: None)          # jak na prawdziwej zimie
    fit = fm.fit_best([_closed_loop()], dt_h=1.0)
    assert fit is not None and fm.accept_fit(fit)
    assert fit.source == fm.SOURCE_BALANCE


def test_fit_best_returns_accepted_model_on_closed_loop_data():
    fit = fm.fit_best([_closed_loop()], dt_h=1.0)
    assert fit is not None and fm.accept_fit(fit)


def test_stamp_keeps_balance_label():
    m = fm.FloorModel(source=fm.SOURCE_BALANCE)
    assert fm.stamp(m, fm.SOURCE_BOOTSTRAP, datetime(2026, 1, 1)).source == fm.SOURCE_BALANCE
    assert fm.stamp(fm.FloorModel(), fm.SOURCE_SHADOW, datetime(2026, 1, 1)).source == fm.SOURCE_SHADOW


def test_defaults_are_a_well_insulated_inert_house():
    m = fm.FloorModel()
    assert m.c <= 0.02 and 1 / m.g >= 10           # pojemność co najmniej ~10 kWh/K
