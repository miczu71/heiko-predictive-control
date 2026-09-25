from heiko_predictive_control.cycle import (
    simulated_setpoint_c, simulate_cost_increment_pln, average_temp,
)


def test_simulated_setpoint_peak_lowers():
    assert simulated_setpoint_c(baseline_c=22, band_c=1.5, is_peak=True) == 20.5


def test_simulated_setpoint_offpeak_raises():
    assert simulated_setpoint_c(baseline_c=22, band_c=1.5, is_peak=False) == 23.5


def test_simulate_cost_increment_scales_with_drive_ratio():
    # baseline drive = 22-0 = 22, sim drive = 20.5-0 = 20.5 -> scale ~0.9318
    cost = simulate_cost_increment_pln(
        observed_energy_kwh=2.0, price_pln_kwh=1.0, outdoor_c=0.0,
        baseline_setpoint_c=22.0, sim_setpoint_c=20.5,
    )
    expected = 2.0 * (20.5 / 22.0) * 1.0
    assert round(cost, 6) == round(expected, 6)
    assert cost < 2.0  # niższy setpoint -> taniej


def test_simulate_cost_increment_offpeak_boost_can_cost_more():
    cost = simulate_cost_increment_pln(
        observed_energy_kwh=2.0, price_pln_kwh=1.0, outdoor_c=0.0,
        baseline_setpoint_c=22.0, sim_setpoint_c=23.5,
    )
    assert cost > 2.0


def test_simulate_cost_increment_guards_zero_baseline_drive():
    cost = simulate_cost_increment_pln(
        observed_energy_kwh=2.0, price_pln_kwh=1.0, outdoor_c=22.0,
        baseline_setpoint_c=22.2, sim_setpoint_c=20.0,
    )
    assert cost == 0.0


def test_simulate_cost_increment_no_energy_observed():
    cost = simulate_cost_increment_pln(
        observed_energy_kwh=0.0, price_pln_kwh=1.0, outdoor_c=0.0,
        baseline_setpoint_c=22.0, sim_setpoint_c=20.0,
    )
    assert cost == 0.0


def test_average_temp_skips_none():
    assert average_temp([20.0, None, 22.0]) == 21.0


def test_average_temp_all_none():
    assert average_temp([None, None]) is None
