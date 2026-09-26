from datetime import datetime, timedelta

from heiko_predictive_control import floor_plan as fp
from heiko_predictive_control.floor_model import FloorModel

WED_3AM = datetime(2026, 1, 14, 3, 0)     # środa
SAT_3AM = datetime(2026, 1, 17, 3, 0)


def _workday(d):
    return d.weekday() < 5


def _blocks(t0, hours=36, **kw):
    return fp.build_blocks(t0, int(hours / fp.STEP_H), _workday, 6, 22,
                           peak_price=1.2304, offpeak_price=0.6306, **kw)


def test_curve_interpolates_clamps_and_needs_two_points():
    amb = [-13, -7, 0, 7, 13]
    wat = [27, 26, 25, 24, 23]
    assert fp.curve_setpoint(amb, wat, -20) == 27
    assert fp.curve_setpoint(amb, wat, 20) == 23
    assert fp.curve_setpoint(amb, wat, 3.5) == 24.5
    assert fp.curve_setpoint(amb, wat, 0) == 25
    assert fp.curve_setpoint([0, None], [25, 24], 3) is None


def test_curve_sorts_unordered_points():
    assert fp.curve_setpoint([13, -13, 0], [23, 27, 25], -6.5) == 26.0


def test_blocks_follow_g12w_on_workday_and_are_contiguous():
    blocks = _blocks(WED_3AM, hours=24)
    assert blocks[0].i0 == 0 and blocks[-1].i1 == 96
    assert all(a.i1 == b.i0 for a, b in zip(blocks, blocks[1:]))
    peak_hours = {(b.start.hour, b.end.hour % 24) for b in blocks if b.is_peak}
    assert (6, 13) in peak_hours and (15, 22) in peak_hours
    cheap = [b for b in blocks if not b.is_peak]
    assert any(b.start.hour == 13 and b.end.hour == 15 for b in cheap)
    assert all(b.price_pln_kwh == (1.2304 if b.is_peak else 0.6306) for b in blocks)


def test_blocks_are_capped_and_split_day_night_on_weekend():
    blocks = _blocks(SAT_3AM, hours=30)
    assert not any(b.is_peak for b in blocks)                 # weekend: cała doba tania
    assert all((b.i1 - b.i0) * fp.STEP_H <= fp.MAX_BLOCK_H for b in blocks)
    assert any(b.start.hour == 6 for b in blocks) and any(b.start.hour == 22 for b in blocks)


def test_step_prices_and_bands():
    blocks = _blocks(WED_3AM, hours=12)
    prices = fp.step_prices(blocks, 48)
    assert prices[0] == 0.6306 and prices[4 * 4] == 1.2304   # 07:00 to szczyt
    bands = fp.step_bands(WED_3AM, 48, 20.6, 1.0, 2.0, 6, 22)
    assert bands[0] == (18.6, 22.6)                          # 03:00 noc: ±2
    assert bands[4 * 4] == (19.6, 21.6)                      # 07:00 dzień: ±1


def test_outdoor_steps_interpolates_and_holds_ends():
    pts = [(WED_3AM, 0.0), (WED_3AM + timedelta(hours=2), 4.0)]
    out = fp.outdoor_steps(pts, WED_3AM - timedelta(hours=1), 16, fallback_c=9.9)
    assert out[0] == 0.0                                     # przed prognozą: pierwsza wartość
    assert out[4 + 4] == 2.0                                 # 04:00, w połowie
    assert out[-1] == 4.0                                    # po prognozie: ostatnia wartość
    assert fp.outdoor_steps([], WED_3AM, 3, fallback_c=7.0) == [7.0, 7.0, 7.0]


def _inputs(t0=WED_3AM, band_day=1.5, band_night=2.0, fuse=False, t_out=0.0,
            model=None):
    model = model or FloorModel(tau_h=4.0, g=0.28, c=0.055, e=0.5)
    n = int(36 / fp.STEP_H)
    tr0 = 20.6
    q_ss = model.c * (tr0 - t_out) / model.g               # stan ustalony pod natywną krzywą
    base = tr0 + q_ss / model.e
    return fp.PlanInputs(
        model=model, tr0_c=tr0, qf0_kw=q_ss, t_out_c=[t_out] * n, base_c=[base] * n,
        blocks=_blocks(t0), bands=fp.step_bands(t0, n, 20.6, band_day, band_night, 6, 22),
        target_c=20.6, water_min_c=20.0, water_max_c=32.0, fuse_active=fuse)


def test_baseline_at_steady_state_holds_temperature():
    inp = _inputs()
    res = fp.baseline(inp)
    assert max(abs(t - 20.6) for t in res.temps_c) < 0.05
    assert res.penalty_pln == 0


def test_plan_shifts_heat_to_cheap_blocks_and_saves_money():
    inp = _inputs()
    base, plan = fp.baseline(inp), fp.optimize(inp)
    assert plan.cost_pln < base.cost_pln
    peak = [d for b, d in zip(inp.blocks, plan.deltas) if b.is_peak]
    cheap = [d for b, d in zip(inp.blocks, plan.deltas) if not b.is_peak]
    assert sum(peak) / len(peak) < sum(cheap) / len(cheap)
    lo = min(lo for lo, _ in inp.bands)
    assert min(plan.temps_c) >= lo - 0.3                    # pasmo komfortu jest twarde
    assert all(20.0 <= s <= 32.0 for s in plan.setpoints_c)


def test_tight_band_leaves_little_to_optimize():
    wide, tight = _inputs(), _inputs(band_day=0.1, band_night=0.1)
    saving_wide = fp.baseline(wide).cost_pln - fp.optimize(wide).cost_pln
    saving_tight = fp.baseline(tight).cost_pln - fp.optimize(tight).cost_pln
    assert saving_tight < saving_wide


def test_fuse_forbids_lowering_the_current_block():
    inp = _inputs(fuse=True)
    assert fp.optimize(inp).deltas[0] >= 0


def test_water_setpoint_respects_absolute_limits():
    inp = _inputs()
    inp.water_max_c = 25.0
    res = fp.optimize(inp)
    assert max(res.setpoints_c) <= 25.0 and min(res.setpoints_c) >= 20.0


def test_optimize_never_worse_than_baseline_objective():
    inp = _inputs()
    assert fp.optimize(inp).objective_pln <= fp.baseline(inp).objective_pln + 1e-9


def test_shift_only_plan_holds_mean_temperature_and_saves_less():
    inp = _inputs()
    base, plan = fp.baseline(inp), fp.optimize(inp)
    held = fp.plan_with_shift_only(inp, base)
    assert fp.mean_temp(held) >= fp.mean_temp(base) - 0.15
    assert fp.mean_temp(plan) < fp.mean_temp(base)          # pełny plan wychładza dom
    assert base.cost_pln - held.cost_pln <= base.cost_pln - plan.cost_pln + 1e-9
    assert held.cost_pln <= base.cost_pln + 0.05
