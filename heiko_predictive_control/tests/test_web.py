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


def _app_with_db(tmp_path, settings, rows=()):
    db_path = str(tmp_path / "t.db")
    conn = dbm.get_conn(db_path)
    dbm.migrate(conn)
    for row in rows:
        dbm.insert_cycle(conn, row)
    conn.close()
    return create_app(db_path, lambda: dict(settings), lambda k, v: None,
                      get_state=get_state, get_numeric=get_numeric, house=HOUSE)


def test_api_plan_without_data_returns_default_model(tmp_path):
    data = _app_with_db(tmp_path, SETTINGS).test_client().get("/api/plan").get_json()
    assert data["plan"] is None and data["live_rmse_c"] is None and data["live_samples"] == 0
    assert data["model"]["source"] == "domyślny" and data["bootstrap"] is None


def test_api_plan_reports_live_model_error_and_stored_plan(tmp_path):
    rows = [{"ts": f"2026-01-12T06:{15 * i:02d}:00", "loop": "heiko", "active_profile": "ekonomia",
             "write_enabled": 0, "model_err_c": err} for i, err in enumerate([0.1, -0.1, 0.2])]
    settings = {**SETTINGS, "heiko_plan": {"horizon_h": 36, "hours": [], "blocks": []},
                "floor_model_state": {"tau_h": 6.0, "g": 0.2, "c": 0.05, "e": 0.7, "source": "faza cienia"},
                "floor_bootstrap": {"ok": True, "reason": "ok"}}
    data = _app_with_db(tmp_path, settings, rows).test_client().get("/api/plan").get_json()
    assert data["plan"]["horizon_h"] == 36 and data["model"]["tau_h"] == 6.0
    assert data["live_samples"] == 3 and data["live_rmse_c"] == pytest.approx(0.1414, abs=1e-3)
    assert data["bootstrap"]["ok"] is True


def test_today_summary_reports_actual_cost_from_meter(tmp_path):
    from datetime import date
    today = date.today().isoformat()
    rows = [{"ts": f"{today}T10:{15 * i:02d}:00", "loop": "heiko", "active_profile": "ekonomia",
             "write_enabled": 0, "energy_kwh": 0.5, "price_pln_kwh": 1.2,
             "baseline_cost_today_pln": 0.6, "sim_cost_today_komfort_pln": 0.5,
             "sim_cost_today_ekonomia_pln": 0.4} for i in range(2)]
    data = _app_with_db(tmp_path, SETTINGS, rows).test_client().get("/api/today_summary/heiko").get_json()
    assert data["actual_pln"] == pytest.approx(1.2)
    assert data["baseline_pln"] == pytest.approx(1.2) and data["savings_ekonomia_pln"] == pytest.approx(0.4)


def test_dashboard_heiko_card_has_plan_containers_and_no_write_claims(tmp_path):
    html = _app_with_db(tmp_path, SETTINGS).test_client().get("/").get_data(as_text=True)
    for element_id in ("heiko-chart", "heiko-savings", "heiko-blocks", "heiko-model", "hp-fuse"):
        assert f'id="{element_id}"' in html
    assert "tylko obserwacja" in html and "niczego" in html and "poglądow" in html
    assert "steruje" not in html.split('data-card="loop_heiko"')[1].split("</section>")[0].replace("Grzaniem steruje pompa", "")


def test_api_plan_returns_kpi_with_baseline(tmp_path):
    from datetime import datetime
    now = datetime.now()
    rows = [{"ts": now.isoformat(), "loop": "heiko", "active_profile": "ekonomia", "write_enabled": 0,
             "energy_kwh": 3.0, "tariff_peak": 1, "outdoor_temp_c": 2.0},
            {"ts": now.isoformat(), "loop": "heiko", "active_profile": "ekonomia", "write_enabled": 0,
             "energy_kwh": 7.0, "tariff_peak": 0, "outdoor_temp_c": 2.0}]
    baseline = {"overall": 0.5, "hours": 4000, "kwh": 1800.0, "at": "2026-09-26T06:00",
                "by_class": {"3": {"share": 0.5, "hours": 500, "kwh": 200.0}}}
    data = _app_with_db(tmp_path, {**SETTINGS, "peak_baseline": baseline}, rows).test_client() \
        .get("/api/plan").get_json()
    assert data["kpi"]["share"] == pytest.approx(0.3) and data["kpi"]["delta_pp"] == pytest.approx(-20.0)
    assert data["kpi_baseline"]["overall"] == 0.5 and data["kpi_baseline"]["hours"] == 4000


def test_api_plan_kpi_is_none_without_baseline_or_energy(tmp_path):
    data = _app_with_db(tmp_path, SETTINGS).test_client().get("/api/plan").get_json()
    assert data["kpi"] is None and data["kpi_baseline"] is None


def test_live_shows_pump_water_target_and_reduced_state(tmp_path):
    from heiko_predictive_control.live import reduced_label
    assert reduced_label(1, 1) == "aktywna" and reduced_label(0, 1) == "nieaktywna"
    assert reduced_label(None, 0).startswith("nieznany") and reduced_label(None, None) == "—"
    STATES["sensor.heiko_heat_pump_water_temperature_setpoint"] = {"state": "22.5"}
    row = {"ts": "2026-01-12T06:15:00", "loop": "heiko", "active_profile": "ekonomia",
           "write_enabled": 0, "reduced_active": 1, "curve_on": 1}
    live_data = _app_with_db(tmp_path, SETTINGS, [row]).test_client().get("/api/live").get_json()
    assert live_data["heiko"]["setpoint"] == "22,5°C" and live_data["heiko"]["reduced"] == "aktywna"
    STATES.pop("sensor.heiko_heat_pump_water_temperature_setpoint")


def test_live_hides_dhw_target_and_labels_hot_water_mode(tmp_path):
    STATES["sensor.heiko_heat_pump_water_temperature_setpoint"] = {"state": "48.0"}
    old = STATES["sensor.heiko_heat_pump_working_mode_2"]
    STATES["sensor.heiko_heat_pump_working_mode_2"] = {"state": "Sanitary Hot Water"}
    try:
        heiko = _app_with_db(tmp_path, SETTINGS).test_client().get("/api/live").get_json()["heiko"]
    finally:
        STATES["sensor.heiko_heat_pump_working_mode_2"] = old
        STATES.pop("sensor.heiko_heat_pump_water_temperature_setpoint")
    assert heiko["mode"] == "CWU" and heiko["setpoint"] == "— (CWU)"


# ── Doradca D1: raport / katalog / dziennik zmian (tylko odczyt) ───────────────────────────────

def _advisor_client(tmp_path, settings=None, states=None, compute=None):
    db_path = str(tmp_path / "t.db")
    conn = dbm.get_conn(db_path)
    dbm.migrate(conn)
    conn.close()
    app = create_app(db_path, lambda: {**SETTINGS, **(settings or {})}, lambda k, v: None,
                     get_state=get_state, get_numeric=get_numeric, house=HOUSE,
                     get_states=lambda: states if states is not None else [],
                     compute_report=compute or (lambda conn, settings, now: {"created": "2026-09-26T12:00", "window": {}}))
    return app.test_client(), db_path


def test_report_page_and_nav(tmp_path):
    client, _ = _advisor_client(tmp_path)
    html = client.get("/report").get_data(as_text=True)
    assert "Raport doradcy" in html and 'href="/report"' in html


def test_catalog_api_marks_automation_managed(tmp_path):
    states = [{"entity_id": "number.heiko_heat_pump_dhw_setpoint", "state": "48", "last_changed": "2026-09-26T10:00:00+00:00"}]
    client, _ = _advisor_client(tmp_path, {"advisor_managed_keys": "dhw_setpoint, backup_heater"}, states)
    data = client.get("/api/catalog").get_json()
    row = next(p for p in data["params"] if p["key"] == "dhw_setpoint")
    assert row["state"] == "48" and row["managed_by_automation"] is True and row["cls"] == "A"
    assert row["class_a_range"] == [45, 55] and row["max_step"] == 2
    assert next(p for p in data["params"] if p["key"] == "curve_shift")["state"] is None       # brak encji w HA
    assert data["found"] == 1 and data["total"] > 20


def test_changes_api_shape_and_dashboard_counter(tmp_path):
    client, db_path = _advisor_client(tmp_path)
    conn = dbm.get_conn(db_path)
    conn.execute("INSERT INTO param_changes (ts, key, entity_id, old, new, source) "
                 "VALUES ('2026-09-01T10:00:00+00:00', 'dhw_setpoint', 'e', '48', '58', 'automatyzacja/skrypt')")
    conn.commit()
    conn.close()
    data = client.get("/api/changes").get_json()
    assert data["changes"][0]["key"] == "dhw_setpoint" and data["summary"]["total"] == 1
    assert data["telemetry"]["rows"] == 0 and data["telemetry"]["db_warn"] is False
    assert "Zmiany nastaw pompy" in client.get("/").get_data(as_text=True)


def test_report_api_refresh_runs_in_background_and_caches(tmp_path):
    import time
    client, _ = _advisor_client(tmp_path)
    first = client.get("/api/report").get_json()
    assert first["report"] is None and first["running"] is False
    client.get("/api/report?refresh=1")
    for _ in range(50):
        data = client.get("/api/report").get_json()
        if data["report"] and not data["running"]:
            break
        time.sleep(0.1)
    assert data["report"]["created"] == "2026-09-26T12:00" and data["error"] is None


def test_report_api_reports_failure_without_crashing(tmp_path):
    import time

    def boom(conn, settings, now):
        raise RuntimeError("LTS niedostępne")
    client, _ = _advisor_client(tmp_path, compute=boom)
    client.get("/api/report?refresh=1")
    for _ in range(50):
        data = client.get("/api/report").get_json()
        if not data["running"] and data["error"]:
            break
        time.sleep(0.1)
    assert data["report"] is None and "LTS niedostępne" in data["error"]
