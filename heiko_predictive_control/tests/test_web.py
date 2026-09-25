"""Render pulpitu i /api/live z fałszywym stanem HA — bez sieci, na przykładowym domu."""
import json

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control.layout import EXAMPLE_PATH, parse
from heiko_predictive_control.live import collect, ctrl_summary, fmt_temp
from heiko_predictive_control.web import create_app

HOUSE = parse(json.loads(EXAMPLE_PATH.read_text()), "example")

ZONE = ["sensor.salon_temperature", "sensor.kuchnia_temperature", "sensor.sypialnia_temperature",
        "sensor.pokoj_1_temperature", "sensor.pokoj_2_temperature"]
SETTINGS = {
    "day_zone_temp_entities": ",".join(ZONE),
    "attic_temp_entity": "sensor.poddasze_temperature",
    "attic_door_entity": "binary_sensor.drzwi",
    "tariff_state_entity": "sensor.tariff",
    "tariff_price_entity": "sensor.price",
    "heiko_setpoint_entity": "number.setpoint",
    "heiko_curve_switch_entity": "switch.curve",
    "outdoor_temp_entity": "sensor.outdoor",
    "heiko_enabled": False, "attic_enabled": False,
    "heiko_active_profile": "ekonomia", "attic_active_profile": "komfort",
    "attic_comfort_target_c": 22.0, "attic_economy_target_c": 20.5,
    "attic_work_start_hour": 8, "attic_work_end_hour": 16,
}

STATES = {r.entity: {"state": "21.5"} for r in HOUSE.rooms if r.entity}
STATES.update({
    "sensor.salon_temperature": {"state": "24.2"},
    "sensor.lazienka_temperature": {"state": "unavailable"},
    "climate.salon": {"state": "heat", "attributes": {"current_temperature": 23, "temperature": 24}},
    "climate.poddasze": {"state": "off", "attributes": {"current_temperature": 24.1, "temperature": 24}},
    "sensor.heiko_heat_pump_working_mode_2": {"state": "Standby"},
    "sensor.tariff": {"state": "True"},
    "sensor.price": {"state": "1.2304"},
    "number.setpoint": {"state": "20"},
    "switch.curve": {"state": "off"},
    "sensor.outdoor": {"state": "13.3"},
    "binary_sensor.drzwi": {"state": "on"},
})


def get_state(entity):
    return STATES.get(entity)


def get_numeric(entity):
    data = STATES.get(entity)
    try:
        return float(data["state"]) if data else None
    except (TypeError, ValueError):
        return None


def test_collect_values():
    live = collect(SETTINGS, get_state, get_numeric, HOUSE)
    assert live["rooms"]["salon"]["text"] == "24,2°C"
    assert live["rooms"]["salon"]["cls"] == "t-warm"
    assert live["rooms"]["salon"]["zone"] is True
    assert live["rooms"]["lazienka"]["text"] == "—"       # czujnik niedostępny
    assert live["rooms"]["lazienka"]["zone"] is False
    assert live["rooms"]["poddasze"]["zone"] is False
    assert live["equipment"]["ac_salon"] == {"state": "grzanie", "on": True,
                                             "current": "23,0°C", "target": "24,0°C"}
    assert live["equipment"]["ac_poddasze"]["on"] is False
    assert live["equipment"]["pump"] == {"state": "czuwanie", "on": False}
    assert live["heiko"]["mode"] == "czuwanie"
    assert live["tariff"] == {"peak": True, "label": "SZCZYT", "price": "1,23 PLN/kWh"}
    assert live["heiko"]["curve"] == "wyłączona"
    assert live["attic"]["door"] == "otwarte"
    # średnia tylko z pokoi stref dziennych: 24.2 + 4×21.5
    assert live["heiko"]["avg"] == fmt_temp((24.2 + 4 * 21.5) / 5)


def test_attic_temp_override_from_settings():
    settings = {**SETTINGS, "attic_temp_entity": "sensor.inny"}
    STATES["sensor.inny"] = {"state": "19.0"}
    try:
        assert collect(settings, get_state, get_numeric, HOUSE)["rooms"]["poddasze"]["text"] == "19,0°C"
    finally:
        del STATES["sensor.inny"]


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = dbm.get_conn(db_path)
    dbm.migrate(conn)
    conn.close()
    app = create_app(db_path, lambda: dict(SETTINGS), lambda k, v: None,
                     get_state=get_state, get_numeric=get_numeric, house=HOUSE)
    return app.test_client()


def test_dashboard_renders_model_with_all_hotspots(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<svg class="house"' in html
    for room in HOUSE.rooms:
        if room.entity:
            assert f'data-card="{room.card_key}"' in html, room.key
    for eq in HOUSE.equipment:
        assert f'data-on="{eq.key}"' in html
    for card in ("loop_heiko", "loop_attic", "ac_salon", "room-salon"):
        assert f'<section class="detail" data-card="{card}"' in html
    assert "SZCZYT" in html and "24,2°C" in html
    assert 'class="temp-pill' in html                     # salon ma realistyczną podłogę
    assert "Tu stoi jednostka wewnętrzna pompy ciepła." in html   # notatka pokoju z układu
    assert 'class="notice"' not in html                    # brak ostrzeżeń układu
    assert resp.headers["Cache-Control"] == "no-store"


def test_dashboard_shows_layout_warning(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = dbm.get_conn(db_path)
    dbm.migrate(conn)
    conn.close()
    app = create_app(db_path, lambda: dict(SETTINGS), lambda k, v: None,
                     get_state=get_state, get_numeric=get_numeric, house=HOUSE,
                     house_warnings=["Brak pliku układu domu /x — pokazuję przykładowy dom."])
    html = app.test_client().get("/").get_data(as_text=True)
    assert 'class="notice"' in html and "Brak pliku układu domu" in html


def test_api_live(client):
    resp = client.get("/api/live")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["rooms"]["sypialnia"]["text"] == "21,5°C"
    assert data["tariff"]["peak"] is True


def test_ctrl_summary_formats_controller_state():
    out = ctrl_summary(
        {"phase": "dogrzewanie", "planned_start": "2026-01-12T06:10:00",
         "ac_cmd_setpoint": 25.5, "window_open": 0},
        {"owned": True, "offset_c": 1.5}, {"pln": 1.234, "kwh": 2.5})
    assert out == {"phase": "dogrzewanie", "owned": "tak", "offset": "+1,5°C",
                   "planned_start": "06:10", "last_setpoint": "25,5°C",
                   "window": "zamknięte", "presence": "—", "today": "1,23 PLN · 2,50 kWh"}


def test_ctrl_summary_empty_is_dashes():
    out = ctrl_summary(None, None, None)
    assert out["phase"] == "—" and out["owned"] == "nie" and out["planned_start"] == "—"
    assert out["today"] == "—" and out["window"] == "—"


def test_dashboard_and_api_show_attic_controller_from_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = dbm.get_conn(db_path)
    dbm.migrate(conn)
    dbm.insert_cycle(conn, {
        "ts": "2026-01-12T06:15:00", "loop": "attic", "active_profile": "komfort",
        "write_enabled": 1, "phase": "pauza_okno", "planned_start": "2026-01-12T06:10:00",
        "ac_cmd_setpoint": 25.5, "window_open": 1, "attic_energy_kwh": 0.0, "attic_cost_pln": 0.0})
    conn.close()
    settings = {**SETTINGS, "attic_ctrl_state": {"owned": True, "offset_c": 2.0}}
    app = create_app(db_path, lambda: dict(settings), lambda k, v: None,
                     get_state=get_state, get_numeric=get_numeric, house=HOUSE)
    api = app.test_client().get("/api/live").get_json()["attic"]
    assert api["phase"] == "pauza — otwarte okno" and api["owned"] == "tak"
    assert api["offset"] == "+2,0°C" and api["window"] == "otwarte"
    html = app.test_client().get("/").get_data(as_text=True)
    assert 'data-live="attic.phase">pauza — otwarte okno<' in html


def test_attic_offset_shows_default_when_state_never_persisted(client):
    # dry-run nie utrwala stanu sterowania; pulpit ma pokazać domyślny offset jak MQTT
    assert client.get("/api/live").get_json()["attic"]["offset"] == "+1,5°C"


def test_ctrl_summary_labels_paused_phase():
    assert ctrl_summary({"phase": "wstrzymane"}, None, None)["phase"] == "wstrzymane (urlop / pauza)"


def test_ctrl_summary_presence_text():
    assert ctrl_summary({"presence": 1, "vacant_min": 0.0}, None, None)["presence"] == "jest"
    assert ctrl_summary({"presence": 0, "vacant_min": 47.6}, None, None)["presence"] == "brak od 48 min"
    assert ctrl_summary({"presence": 0, "vacant_min": 95.0}, None, None)["presence"] == "brak od 1 h 35 min"
    assert ctrl_summary({"presence": None}, None, None)["presence"] == "—"
    assert ctrl_summary({"phase": "pusto"}, None, None)["phase"] == "pusto — nikogo na poddaszu"
