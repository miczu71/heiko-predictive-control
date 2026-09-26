from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from heiko_predictive_control import analysis as an
from heiko_predictive_control import db as dbm

TZ = ZoneInfo("Europe/Warsaw")
SETTINGS = {"heiko_room_target_c": 20.6, "heiko_comfort_band_day_c": 1.0, "heiko_comfort_band_night_c": 1.5,
            "heiko_room_min_c": 18.5, "heiko_day_start_hour": 6, "heiko_day_end_hour": 22}


def ms(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp() * 1000)


def hours(day_from=1, day_to=3, month=12):
    return [ms(2025, month, d, h) for d in range(day_from, day_to) for h in range(24)]


def test_percentile():
    assert an.percentile([], 0.5) is None
    assert an.percentile([1.0], 0.9) == 1.0
    assert an.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5


def test_comfort_time_in_band_and_coldest_room():
    hs = hours()
    stats = {"out": [{"start": s, "mean": 0.0} for s in hs],
             "a": [{"start": s, "mean": 20.6} for s in hs],                       # zawsze w paśmie
             "b": [{"start": s, "mean": 20.6 if i % 2 else 22.5} for i, s in enumerate(hs)]}   # co druga godzina poza pasmem
    out = an.comfort(stats, ["a", "b"], "out", SETTINGS, TZ)
    assert out["hours"] == len(hs)
    assert out["rooms"]["a"]["in_band"] == 1.0 and out["rooms"]["a"]["sd"] == 0.0
    assert out["rooms"]["b"]["in_band"] == 0.5
    assert out["coldest"]["min"] == 20.6 and out["coldest"]["below_room_min_h"] == 0
    assert out["rooms"]["a"]["offset"] == 0.0 and out["rooms"]["b"]["offset"] == round(out["rooms"]["b"]["mean"] - 20.6, 2)
    assert out["average"]["in_band"] == 1.0
    assert out["average"]["mean"] == pytest.approx((20.6 + (22.5 + 20.6) / 2) / 2, abs=0.01)     # średnia z pokoi a i b


def test_comfort_counts_hours_below_room_min_and_skips_warm_and_summer():
    hs = hours()
    stats = {"out": [{"start": s, "mean": 0.0} for s in hs],
             "a": [{"start": s, "mean": 18.0} for s in hs]}
    assert an.comfort(stats, ["a"], "out", SETTINGS, TZ)["coldest"]["below_room_min_h"] == len(hs)
    warm = {"out": [{"start": s, "mean": 20.0} for s in hs], "a": stats["a"]}      # powyżej progu grzania
    assert an.comfort(warm, ["a"], "out", SETTINGS, TZ) is None
    summer = {"out": [{"start": s, "mean": 0.0} for s in hours(month=7)], "a": [{"start": s, "mean": 20.0} for s in hours(month=7)]}
    assert an.comfort(summer, ["a"], "out", SETTINGS, TZ) is None


def test_energy_months_hdd_and_peak_share():
    hs = hours(1, 2)                                                                # 24 h grudnia 2025, wtorek
    stats = {"out": [{"start": s, "mean": 4.0} for s in hs],
             "e": [{"start": s, "change": 1.0} for s in hs]}
    out = an.energy(stats, "out", "e", TZ)
    m = out["months"]["2025-12"]
    assert m["kwh"] == 24.0 and m["hours"] == 24
    assert m["hdd"] == round((14.0 - 4.0), 1)                                         # 24 h × 10 K / 24
    assert m["kwh_per_hdd"] == 2.4
    assert 0.5 < out["peak_share"]["overall"] < 0.7                                   # 13 z 24 h to szczyt w dzień roboczy
    assert an.energy({"out": [], "e": []}, "out", "e", TZ) is None


def test_pump_hours_by_class_uses_daily_max():
    day_ms = [ms(2025, 12, 1, 23) + i * 86400000 for i in range(3)]                  # lokalny początek dób 2.–4.12
    daily = {"heating": [{"start": s, "max": 9.0} for s in day_ms],
             "hot_water": [{"start": s, "max": 1.0} for s in day_ms],
             "off": [{"start": s, "max": 14.0} for s in day_ms]}
    outdoor = [{"start": ms(2025, 12, d, h), "mean": -1.0} for d in range(1, 6) for h in range(24)]
    out = an.pump_hours(daily, outdoor, TZ)
    v = out["by_class"]["0"]                                                          # −1°C → klasa 0
    assert v == {"days": 3, "heating_h": 9.0, "dhw_h": 1.0, "standby_h": 14.0}
    assert out["by_month"]["2025-12"]["days"] == 3
    impossible = {**daily, "off": [{"start": s, "max": 40.0} for s in day_ms]}        # suma > 26 h → odrzucone
    assert an.pump_hours(impossible, outdoor, TZ) is None


def test_cop_by_class_filters_nonsense():
    hs = hours(1, 2)
    stats = {"out": [{"start": s, "mean": 5.0} for s in hs],
             "cop": [{"start": hs[0], "mean": 3.0}, {"start": hs[1], "mean": 5.0},
                     {"start": hs[2], "mean": 0.0}, {"start": hs[3], "mean": 40.0}]}
    out = an.cop_by_class(stats, "cop", "out")
    assert out["6"] == {"hours": 2, "mean": 4.0, "min": 3.0, "max": 5.0}


def _report(offset=1.2, in_band=0.5, below=0):
    return {"comfort": {"room_min": 18.5, "hours": 1000, "average": {"in_band": in_band, "offset": offset},
                        "rooms": {"sensor.x_salon": {"offset": 1.4}, "sensor.x_kitchen": {"offset": -0.2}},
                        "coldest": {"below_room_min_h": below}},
            "energy": {"peak_share": {"overall": 0.41}},
            "own": {"days": 3, "avg_per_day": {"short_cycles": 5, "dhw_cycles": 4, "ah_min": 9.0, "hwtbh_min": 30.0}},
            "pump_hours": {"by_class": {"-3": {"days": 10, "heating_h": 13.3, "dhw_h": 1.3},
                                        "6": {"days": 20, "heating_h": 5.9, "dhw_h": 1.4}}}}


def test_ideas_direction_follows_sign_of_offset_not_a_fixed_direction():
    warm = " ".join(an.ideas(_report(offset=1.2)))
    assert "za ciepło" in warm and "za zimno" not in warm
    assert "salon (+1.4°C)" in warm and "lazienka" not in warm        # tylko pokoje powyżej celu o >1°C
    cold = " ".join(an.ideas(_report(offset=-1.3)))
    assert "za zimno" in cold


def test_ideas_offpeak_ceiling_and_rule_based_text():
    ideas = an.ideas(_report(below=242))
    joined = " ".join(ideas)
    assert "Sufit przesuwania" in joined and "13 h/dobę" in joined and "10 h" in joined     # 24 − 14 h szczytu
    assert "242 godz." in joined and "41%" in joined
    assert "krótkich cykli" in joined and "cykli CWU" in joined
    assert "AH 9 min/dobę" in joined and "HWTBH 30 min/dobę" in joined and "HBH" not in joined     # tylko niezerowe liczniki
    assert not any("Sufit" in i for i in an.ideas({**_report(), "pump_hours": {"by_class": {"6": {"days": 20, "heating_h": 5.9}}}}))
    assert an.ideas({}) == []


def test_gaps_flag_unrealistic_cop_and_short_own_history():
    report = {**_report(), "cop": {"9": {"hours": 17, "mean": 8.95}, "12": {"hours": 18, "mean": 9.9}}}
    gaps = an.data_gaps(report, datetime(2026, 9, 26))
    assert any("2026-04-12" in g for g in gaps) and any("3 dni" in g for g in gaps)
    assert any("nierealne" in g for g in gaps)
    assert not any("nierealne" in g for g in an.data_gaps({**_report(), "cop": {"9": {"mean": 3.1}}}, datetime(2026, 9, 26)))


def test_compute_report_survives_missing_statistics(tmp_path):
    conn = dbm.get_conn(str(tmp_path / "t.db"))
    dbm.migrate(conn)
    settings = {**SETTINGS, "day_zone_temp_entities": "a,b", "outdoor_temp_entity": "out", "pump_energy_entity": "e",
                "timezone": "Europe/Warsaw"}
    report = an.compute_report(conn, settings, datetime(2026, 9, 26, 12, 0),
                               get_statistics=lambda *a, **k: None, get_states=lambda: [])
    assert report["window"] == {"start": "2025-10-15", "end": "2026-04-15"}
    assert report["own"]["days"] == 0 and report["data_gaps"]
    an.save_report(conn, report)
    assert an.load_report(conn)["created"] == report["created"]
    conn.close()
