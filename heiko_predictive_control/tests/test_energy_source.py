from heiko_predictive_control.energy_source import Step, attribute


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
