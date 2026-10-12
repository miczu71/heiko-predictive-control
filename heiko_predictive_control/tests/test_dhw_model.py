from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import dhw_model as dm

TZ = ZoneInfo("Europe/Warsaw")


def hp_cycle(outdoor, water):
    """Odcinek sprężarki zgodny z dokładnym modelem liniowym (bez szumu)."""
    return {"outdoor_c": outdoor, "hp_water_mid_c": water,
            "hp_rate_c_min": 0.6 + 0.01 * (outdoor - 10) - 0.004 * (water - 45),
            "hp_kw": 2.0 + 0.05 * (outdoor - 10) + 0.02 * (water - 45)}


GRID = [hp_cycle(o, w) for o in (0, 5, 10, 15) for w in (35, 45, 55)]      # 12 odcinków, temperatury niezależne


def price(local):
    return 1.0 if 6 <= local.hour < 22 else 0.5


def ah_cycle(**over):
    c = {"start": "2026-10-12T02:00:00+00:00", "ah_tick_min": 19.0, "outdoor_c": 10.0, "ah_water_mid_c": 45.0,
         "ah_rate_c_min": 0.85, "ah_total_kw": 5.4, "ah_seg_min": 6.0}
    c.update(over)
    return c


def test_fit_recovers_known_coefficients():
    m = dm.fit_model(GRID)
    assert m["ok"] and m["n"] == 12
    assert m["rate"]["coef"][1] == pytest.approx(0.01, abs=1e-4) and m["rate"]["coef"][2] == pytest.approx(-0.004, abs=1e-4)
    assert m["rate"]["r2"] == pytest.approx(1.0)
    rate, kw = dm.predict(m, 10.0, 45.0)
    assert rate == pytest.approx(0.6, abs=1e-4) and kw == pytest.approx(2.0, abs=1e-3)
    assert m["ranges"] == {"outdoor": [0, 15], "water": [35, 55]}


def test_fit_refuses_too_few_or_degenerate_segments():
    few = dm.fit_model(GRID[:5])
    assert not few["ok"] and few["n"] == 5 and "za mało" in few["reason"]
    same_temps = dm.fit_model([hp_cycle(10, 45)] * 10)
    assert not same_temps["ok"] and "różnią" in same_temps["reason"]


def test_fit_ignores_segments_without_compressor_work_or_missing_fields():
    bad = [{**hp_cycle(10, 45), "hp_kw": 0.1}, {**hp_cycle(10, 45), "hp_rate_c_min": None},
           {**hp_cycle(10, 45), "outdoor_c": None}]
    assert dm.fit_model(GRID + bad)["n"] == 12


def test_ah_effect_counterfactual_matches_hand_calculation():
    # 5,1 °C na odcinku AH; sama sprężarka 0,6 °C/min i 2,0 kW → 8,5 min i 0,2833 kWh.
    # AH: 5,4 kW · 6 min = 0,54 kWh → +0,2567 kWh, 2,5 min oszczędności; 04:19 lokalnie = poza szczytem.
    eff = dm.ah_effect(ah_cycle(), dm.fit_model(GRID), price, TZ)
    assert eff["ok"]
    assert eff["extra_kwh"] == pytest.approx(0.2567, abs=0.002) and eff["saved_min"] == pytest.approx(2.5, abs=0.05)
    assert eff["price_pln_kwh"] == 0.5 and eff["extra_pln"] == pytest.approx(0.13, abs=0.01)


def test_ah_effect_reports_reason_instead_of_guessing():
    m = dm.fit_model(GRID)
    assert dm.ah_effect(ah_cycle(), dm.fit_model(GRID[:3]), price, TZ)["reason"].startswith("za mało")
    assert dm.ah_effect(ah_cycle(ah_tick_min=None), m, price, TZ)["reason"] == "brak odcinka z AH"
    assert dm.ah_effect(ah_cycle(outdoor_c=-12.0), m, price, TZ)["reason"] == "warunki poza zakresem uczenia modelu"
    assert "niezmierzony" in dm.ah_effect(ah_cycle(ah_seg_min=None), m, price, TZ)["reason"]     # np. zapis sprzed 0.13.0
    assert "brak przyrostu" in dm.ah_effect(ah_cycle(ah_rate_c_min=-0.1), m, price, TZ)["reason"]


def test_summarize_totals_and_price_per_saved_minute():
    m = dm.fit_model(GRID)
    cycles = [ah_cycle(), ah_cycle(start="2026-10-13T02:00:00+00:00"), ah_cycle(outdoor_c=-12.0),
              {"start": "2026-10-14T02:00:00+00:00", "ah_tick_min": None}]
    s = dm.summarize(cycles, m, price, TZ)
    assert s["cycles_with_ah"] == 3 and s["evaluated"] == 2 and len(s["skipped"]) == 1
    assert s["extra_kwh"] == pytest.approx(0.513, abs=0.005) and s["saved_min"] == pytest.approx(5.0, abs=0.1)
    assert s["pln_per_saved_min"] == pytest.approx(s["extra_pln"] / s["saved_min"], abs=0.01)


def test_summarize_without_savings_gives_no_price_per_minute():
    s = dm.summarize([], dm.fit_model(GRID), price, TZ)
    assert s["cycles_with_ah"] == 0 and s["pln_per_saved_min"] is None
