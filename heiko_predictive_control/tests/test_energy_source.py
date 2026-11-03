import copy

import pytest

from heiko_predictive_control.energy_source import Step, attribute, mix_after, mix_cost, mix_shares


def _step(**kw):
    base = dict(load_kwh=0.5, import_kwh=0.0, charge_kwh=0.0, discharge_kwh=0.0, soc_kwh=2.0,
                grid_price=1.2, rce_price=0.3)
    return Step(**{**base, **kw})


def test_load_fully_from_grid_costs_tariff_price():
    r = attribute(_step(import_kwh=2.0), battery_cost=0.6)
    assert (r.grid_kwh, r.battery_kwh, r.pv_kwh) == (0.5, 0.0, 0.0)
    assert r.cost_pln == 0.5 * 1.2


def test_surplus_pv_costs_rce_and_partial_import_is_split():
    r = attribute(_step(import_kwh=0.2), battery_cost=0.6)
    assert (r.grid_kwh, r.battery_kwh, round(r.pv_kwh, 6)) == (0.2, 0.0, 0.3)
    assert round(r.cost_pln, 6) == round(0.2 * 1.2 + 0.3 * 0.3, 6)


def test_battery_discharge_covers_load_at_battery_average_cost():
    r = attribute(_step(import_kwh=0.1, discharge_kwh=0.3), battery_cost=0.6)
    assert (r.grid_kwh, r.battery_kwh, round(r.pv_kwh, 6)) == (0.1, 0.3, 0.1)
    assert round(r.cost_pln, 6) == round(0.1 * 1.2 + 0.3 * 0.6 + 0.1 * 0.3, 6)
    assert r.battery_cost == 0.6                     # rozładowanie nie zmienia średniej


def test_charging_moves_battery_average_by_source_of_charged_energy():
    # przed krokiem 1 kWh po 0,6; ładowanie 1 kWh: 0,4 z sieci (pobór po pompie) + 0,6 z PV
    r = attribute(_step(import_kwh=0.9, charge_kwh=1.0, soc_kwh=2.0), battery_cost=0.6)
    charged = 0.4 * 1.2 + 0.6 * 0.3
    assert round(r.battery_cost, 6) == round((1.0 * 0.6 + charged) / 2.0, 6)


def test_night_grid_charge_of_empty_battery_takes_tariff_price():
    r = attribute(_step(load_kwh=0.0, import_kwh=2.0, charge_kwh=1.5, soc_kwh=1.5, grid_price=0.63),
                  battery_cost=0.2)
    assert round(r.battery_cost, 6) == 0.63


# --- skład baterii (mix_after / mix_cost / mix_shares) ---


def test_night_grid_charge_of_empty_battery_is_all_grid_offpeak():
    step = _step(load_kwh=0.0, import_kwh=2.0, charge_kwh=1.5, soc_kwh=1.5, grid_price=0.63)
    mix = mix_after({}, step, battery_cost=0.2, zone="offpeak")
    assert mix.keys() == {"grid:offpeak"}
    assert mix["grid:offpeak"]["kwh"] == pytest.approx(1.5)
    assert mix["grid:offpeak"]["pln"] == pytest.approx(1.5 * 0.63)


def test_discharge_keeps_origin_shares_and_scales_kwh_to_kept():
    mix = {"grid:offpeak": {"kwh": 1.0, "pln": 0.6}, "pv": {"kwh": 1.0, "pln": 0.3}}
    step = _step(load_kwh=0.5, import_kwh=0.0, discharge_kwh=1.0, soc_kwh=1.0)   # kept = 1,0 z 2,0
    out = mix_after(mix, step, battery_cost=0.45, zone="peak")
    assert out["grid:offpeak"]["kwh"] == pytest.approx(0.5) and out["grid:offpeak"]["pln"] == pytest.approx(0.3)
    assert out["pv"]["kwh"] == pytest.approx(0.5) and out["pv"]["pln"] == pytest.approx(0.15)
    before, after = mix_shares(mix), mix_shares(out)
    for origin in mix:
        assert after[origin]["share"] == pytest.approx(before[origin]["share"])
        assert after[origin]["pln_kwh"] == pytest.approx(before[origin]["pln_kwh"])


def test_mixed_charge_splits_grid_part_by_zone_and_rest_as_pv():
    mix = {"grid:offpeak": {"kwh": 1.0, "pln": 0.6}}
    # pobór 0,5 kWh, pompa 0,2 → 0,3 kWh z sieci zostaje na ładowanie, 0,7 kWh to PV
    step = _step(load_kwh=0.2, import_kwh=0.5, charge_kwh=1.0, soc_kwh=2.0, grid_price=1.2, rce_price=0.3)
    out = mix_after(mix, step, battery_cost=0.6, zone="peak")
    assert out["grid:offpeak"] == pytest.approx({"kwh": 1.0, "pln": 0.6})
    assert out["grid:peak"]["kwh"] == pytest.approx(0.3) and out["grid:peak"]["pln"] == pytest.approx(0.3 * 1.2)
    assert out["pv"]["kwh"] == pytest.approx(0.7) and out["pv"]["pln"] == pytest.approx(0.7 * 0.3)
    assert sum(v["kwh"] for v in out.values()) == pytest.approx(2.0)


def test_negative_rce_charge_from_pv_is_free():
    step = _step(load_kwh=0.0, import_kwh=0.0, charge_kwh=0.5, soc_kwh=0.5, rce_price=-0.2)
    out = mix_after({}, step, battery_cost=0.4, zone="offpeak")
    assert out == {"pv": {"kwh": pytest.approx(0.5), "pln": 0.0}}


def test_unknown_content_without_mix_is_priced_at_battery_cost():
    step = _step(load_kwh=0.0, import_kwh=0.0, soc_kwh=1.0)                    # kept = 1,0, mix pusty
    out = mix_after({}, step, battery_cost=0.4, zone="offpeak")
    assert out == {"unknown": {"kwh": pytest.approx(1.0), "pln": pytest.approx(0.4)}}


def test_fully_discharged_battery_leaves_empty_mix():
    mix = {"grid:offpeak": {"kwh": 1.0, "pln": 0.6}}
    step = _step(load_kwh=1.0, import_kwh=0.0, discharge_kwh=1.0, soc_kwh=0.0)  # kept = 0
    assert mix_after(mix, step, battery_cost=0.6, zone="peak") == {}


def test_tiny_origin_is_dropped_from_result():
    mix = {"grid:offpeak": {"kwh": 1.0, "pln": 0.6}, "pv": {"kwh": 0.0, "pln": 0.0}}
    step = _step(load_kwh=0.5, import_kwh=0.0, discharge_kwh=0.5, soc_kwh=0.5)
    assert mix_after(mix, step, battery_cost=0.6, zone="peak").keys() == {"grid:offpeak"}


def test_mix_after_does_not_mutate_input():
    mix = {"grid:offpeak": {"kwh": 2.0, "pln": 1.2}}
    snapshot = copy.deepcopy(mix)
    mix_after(mix, _step(import_kwh=1.0, charge_kwh=1.0, soc_kwh=3.0), battery_cost=0.6, zone="offpeak")
    assert mix == snapshot


@pytest.mark.parametrize("step", [
    _step(load_kwh=0.5, import_kwh=1.0, charge_kwh=1.0, soc_kwh=3.0),                # ładowanie
    _step(load_kwh=0.5, import_kwh=0.0, discharge_kwh=1.0, soc_kwh=1.0),             # rozładowanie
])
def test_mix_cost_after_matches_battery_cost_of_attribution(step):
    mix = {"grid:offpeak": {"kwh": 2.0, "pln": 1.2}}                                 # Σkwh = kept, koszt 0,6
    out = mix_after(mix, step, battery_cost=0.6, zone="offpeak")
    assert mix_cost(out) == pytest.approx(attribute(step, battery_cost=0.6).battery_cost, abs=1e-9)


def test_mix_cost_and_shares_of_empty_mix():
    assert mix_cost({}) is None
    assert mix_shares({}) == {}


def test_mix_shares_gives_kwh_share_and_price_per_kwh():
    mix = {"grid:offpeak": {"kwh": 1.0, "pln": 0.6}, "pv": {"kwh": 3.0, "pln": 0.9}}
    shares = mix_shares(mix)
    assert shares["grid:offpeak"] == pytest.approx({"share": 0.25, "pln_kwh": 0.6})
    assert shares["pv"] == pytest.approx({"share": 0.75, "pln_kwh": 0.3})
    assert mix_cost(mix) == pytest.approx(1.5 / 4.0)
