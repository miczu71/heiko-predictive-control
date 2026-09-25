from datetime import datetime

from heiko_predictive_control.cycle import (
    simulated_setpoint_c, simulate_cost_increment_pln, average_temp,
    attic_should_run,
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


def test_attic_should_run_inside_window_workday():
    now = datetime(2026, 1, 12, 9, 0)  # poniedziałek 9:00
    assert attic_should_run(now, work_start_hour=8, work_end_hour=16,
                             preheat_lead_min=45, is_workday=True) is True


def test_attic_should_run_preheat_lead():
    now = datetime(2026, 1, 12, 7, 30)  # 30 min przed 8:00, lead=45 min
    assert attic_should_run(now, work_start_hour=8, work_end_hour=16,
                             preheat_lead_min=45, is_workday=True) is True


def test_attic_should_run_before_lead_window():
    now = datetime(2026, 1, 12, 7, 0)  # 60 min przed 8:00, lead=45 min
    assert attic_should_run(now, work_start_hour=8, work_end_hour=16,
                             preheat_lead_min=45, is_workday=True) is False


def test_attic_should_run_after_work_end():
    now = datetime(2026, 1, 12, 16, 0)
    assert attic_should_run(now, work_start_hour=8, work_end_hour=16,
                             preheat_lead_min=45, is_workday=True) is False


def test_attic_should_run_weekend_never():
    now = datetime(2026, 1, 10, 10, 0)  # sobota
    assert attic_should_run(now, work_start_hour=8, work_end_hour=16,
                             preheat_lead_min=45, is_workday=False) is False
