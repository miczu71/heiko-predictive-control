import inspect
from datetime import date, datetime, timedelta

import pytest

from heiko_predictive_control import cycle, ha_client
from heiko_predictive_control import db as dbm
from heiko_predictive_control import floor_model as fm


def test_average_temp_skips_none():
    assert cycle.average_temp([20.0, None, 22.0]) == 21.0


def test_average_temp_all_none():
    assert cycle.average_temp([None, None]) is None


def test_mode_flags_reads_pump_working_mode_text():
    assert cycle.mode_flags("Heating") == (True, False)
    assert cycle.mode_flags("Sanitary Hot Water") == (False, True)
    assert cycle.mode_flags("Standby") == (False, False)
    assert cycle.mode_flags(None) == (False, False)
    assert cycle.mode_flags("2") == (True, False) and cycle.mode_flags("1") == (False, True)


def test_parse_forecast_converts_and_skips_bad_points():
    pts = cycle.parse_forecast([
        {"datetime": "2026-01-14T12:00:00", "temperature": 3.5},
        {"datetime": "2026-01-14T13:00:00", "temperature": None},
        {"datetime": "nonsense", "temperature": 1.0},
        {"datetime": "2026-01-14T11:00:00", "temperature": 2.0},
    ])
    assert [p[1] for p in pts] == [2.0, 3.5]                # posortowane, bez błędnych
    assert cycle.parse_forecast(None) == []


def test_workday_resolver_uses_sensor_for_today_service_for_others_weekday_fallback():
    calls = []

    def check(d):
        calls.append(d)
        return {"2026-11-11": False}.get(d)                 # święto; reszta „brak odpowiedzi”

    fn = cycle.workday_resolver(date(2026, 1, 14), False, check)   # środa, ale sensor mówi „wolne”
    assert fn(date(2026, 1, 14)) is False and calls == []
    assert fn(date(2026, 11, 11)) is False                  # z usługi
    assert fn(date(2026, 1, 17)) is False and fn(date(2026, 1, 15)) is True   # fallback pn–pt
    fn(date(2026, 1, 15))
    assert calls.count("2026-01-15") == 1                   # cache


# ── run_heiko_cycle ───────────────────────────────────────────────────────

NOW = datetime(2026, 1, 14, 10, 7)                          # środa
CURVE_A = {f"number.heiko_heat_pump_curve_ambient_temp_{i}": v for i, v in enumerate([-13, -7, 0, 7, 13], 1)}
CURVE_W = {f"number.heiko_heat_pump_curve_water_temp_{i}": v for i, v in enumerate([27, 26, 25, 24, 23], 1)}
SETTINGS = {
    "day_zone_temp_entities": "sensor.z1,sensor.z2,sensor.z3", "outdoor_temp_entity": "sensor.out",
    "tariff_state_entity": "sensor.tariff", "tariff_price_entity": "sensor.price",
    "pump_energy_entity": "sensor.energy", "weather_entity": "weather.home",
    "heiko_active_profile": "ekonomia", "heiko_enabled": False,
    "heiko_comfort_band_day_c": 1.0, "heiko_comfort_band_night_c": 1.5,
    "heiko_economy_band_day_c": 1.5, "heiko_economy_band_night_c": 2.0,
    "heiko_day_start_hour": 6, "heiko_day_end_hour": 22,
    "heiko_room_target_c": 20.6, "heiko_room_min_c": 18.5,
    "heiko_water_min_c": 20.0, "heiko_water_max_c": 32.0,
}


class World:
    def __init__(self, rooms=(20.4, 20.7, 20.6), energy=100.0, mode="Standby"):
        self.rooms = list(rooms)
        self.energy = energy
        self.mode = mode
        self.numeric = {"sensor.out": 2.0, "sensor.price": 1.2304, **CURVE_A, **CURVE_W}

    def get_state(self, entity):
        if entity.startswith("sensor.z"):
            return {"state": str(self.rooms[int(entity[-1]) - 1]),
                    "attributes": {"friendly_name": f"Pokój {entity[-1]}"}}
        if entity == "sensor.heiko_heat_pump_working_mode_2":
            return {"state": self.mode, "attributes": {}}
        return None

    def get_numeric(self, entity):
        if entity == "sensor.energy":
            return self.energy
        if entity == "sensor.heiko_heat_pump_condenser_temperature":
            return 26.0
        return self.numeric.get(entity)

    def get_bool(self, entity):
        return {"sensor.tariff": True, "binary_sensor.workday": True}.get(entity)

    def forecast(self, entity):
        return [{"datetime": (NOW + timedelta(hours=h)).isoformat(), "temperature": 2.0 - 0.1 * h}
                for h in range(48)]


def _run(conn, world, now, settings=None):
    return cycle.run_heiko_cycle(conn, settings or SETTINGS, now, get_state=world.get_state,
                                  get_numeric=world.get_numeric, get_bool=world.get_bool,
                                  get_forecast=world.forecast, check_workday=lambda d: None)


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    return c


@pytest.fixture(autouse=True)
def no_writes(monkeypatch):
    """Pętla A w fazie cienia NIGDY nie pisze do HA — każde wywołanie usługi wywraca test."""
    def boom(*a, **k):
        raise AssertionError("pętla A wywołała usługę HA")
    monkeypatch.setattr(ha_client, "call_service", boom)
    monkeypatch.setattr(ha_client, "notify", boom)


def test_first_cycle_stores_plan_for_both_profiles_and_row(conn):
    row = _run(conn, World(), NOW)
    assert row["indoor_temp_c"] == pytest.approx(20.5667, abs=1e-3)
    assert row["base_curve_c"] == pytest.approx(24.714, abs=0.01)    # krzywa przy +2°C
    assert row["setpoint_komfort"] is not None and row["setpoint_ekonomia"] is not None
    assert row["plan_setpoint_c"] == row["setpoint_ekonomia"]        # aktywny profil
    assert 20.0 <= row["plan_setpoint_c"] <= 32.0
    assert row["min_room_c"] == 20.4 and row["min_room_name"] == "Pokój 1"
    assert row["energy_kwh"] is None                                 # pierwszy cykl: nie ma różnicy
    plan = dbm.get_setting(conn, "heiko_plan")
    assert plan["horizon_h"] == 36 and len(plan["hours"]) == 36
    assert set(plan["summary"]) == {"komfort", "ekonomia"}
    assert plan["blocks"][0]["peak"] is True                          # środa 10:00 = szczyt
    assert plan["summary"]["ekonomia"]["saving_shift_pln"] <= plan["summary"]["ekonomia"]["saving_pln"] + 1e-6
    assert plan["fuse_active"] is False
    stored = dbm.latest_cycle(conn, "heiko")
    assert stored["write_enabled"] == 0 and stored["setpoint_ekonomia"] == row["setpoint_ekonomia"]


def test_second_cycle_measures_heat_and_model_error(conn):
    world = World(mode="Heating")
    _run(conn, world, NOW)
    world.energy += 0.5                                              # 0,5 kWh w 15 min
    world.rooms = [20.5, 20.8, 20.7]
    row = _run(conn, world, NOW + timedelta(minutes=15))
    assert row["energy_kwh"] == pytest.approx(0.5)
    assert row["heat_kw"] == pytest.approx(fm.measured_heat_kw(0.5, 0.25, 2.0, 26.0), rel=1e-3)
    assert row["heating_active"] == 1 and row["dhw_active"] == 0
    assert row["model_err_c"] is not None
    assert dbm.get_setting(conn, "floor_state")["qf"] > 0


def test_dhw_step_is_not_counted_as_room_heating(conn):
    world = World(mode="Standby")
    _run(conn, world, NOW)
    world.energy += 0.8
    world.mode = "Sanitary Hot Water"
    row = _run(conn, world, NOW + timedelta(minutes=15))
    assert row["energy_kwh"] == pytest.approx(0.8) and row["heat_kw"] == 0.0
    assert row["dhw_active"] == 1


def test_cold_room_activates_fuse(conn):
    _run(conn, World(rooms=(20.6, 18.0, 20.6)), NOW)
    plan = dbm.get_setting(conn, "heiko_plan")
    assert plan["fuse_active"] is True and plan["min_room_c"] == 18.0
    assert plan["blocks"][0]["ekonomia_delta"] >= 0                   # blok bieżący nie schodzi w dół


def test_unavailable_sensor_is_left_out_of_average(conn):
    world = World()
    world.get_state = lambda e, w=world: (None if e == "sensor.z2" else World.get_state(w, e))
    row = _run(conn, world, NOW)
    assert row["indoor_temp_c"] == pytest.approx(20.5)               # (20,4 + 20,6) / 2


def test_missing_outdoor_skips_cycle_but_records_row(conn):
    world = World()
    world.numeric.pop("sensor.out")
    row = _run(conn, world, NOW)
    assert "setpoint_komfort" not in row and "plan_setpoint_c" not in row
    assert dbm.get_setting(conn, "heiko_plan") is None
    assert dbm.latest_cycle(conn, "heiko") is not None


def test_missing_curve_points_skip_plan(conn):
    world = World()
    for k in CURVE_A:
        world.numeric.pop(k)
    _run(conn, world, NOW)
    assert dbm.get_setting(conn, "heiko_plan") is None


def test_old_ewma_state_is_ignored_and_default_model_is_used(conn):
    """Stary k_loss z EWMA jest obalony przez dane zimowe — nie zasila nowego modelu."""
    dbm.set_setting(conn, "heiko_model_state", {"k_loss": 0.0553, "k_gain": 0.15, "samples_seen": 40})
    _run(conn, World(), NOW)
    model = dbm.get_setting(conn, "heiko_plan")["model"]
    assert model["c"] == fm.DEFAULT_C and model["source"] == fm.SOURCE_DEFAULT


def test_loop_a_code_contains_no_write_calls():
    """Twarda gwarancja fazy cienia: w ciele pętli A nie ma wywołań usług ani powiadomień."""
    source = inspect.getsource(cycle.run_heiko_cycle)
    assert "call_service" not in source and "notify(" not in source
