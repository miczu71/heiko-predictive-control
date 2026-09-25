"""Punkt startowy add-onu: baza, MQTT, cykl decyzyjny (APScheduler), Flask.

Pętla A (Heiko) jest nadal WYŁĄCZNIE odczytem — `cycle.run_heiko_cycle` nigdy
nie woła `ha_client.call_service`. Pętla B (AC poddasza, od 0.4.0) zapisuje do
klimatyzatora w `cycle.run_attic_cycle`, tylko przy `attic_enabled`. Obie pętle
mają osobne zadania harmonogramu (A: `cycle_interval_min`, B: `attic_cycle_interval_min`)."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from . import __version__, cycle, ha_client, layout
from . import attic as attic_mod
from . import db as dbm
from .model import ThermalModel
from .publisher import MQTTPublisher
from .web import create_app

logger = logging.getLogger("heiko_predictive_control")

_BOOL_KEYS = {"heiko_enabled", "attic_enabled"}
_FLOAT_KEYS = {
    "heiko_comfort_band_day_c", "heiko_comfort_band_night_c",
    "heiko_economy_band_day_c", "heiko_economy_band_night_c",
    "attic_comfort_target_c", "attic_economy_target_c",
}
_INT_KEYS = {
    "heiko_day_start_hour", "heiko_day_end_hour", "heiko_write_throttle_min",
    "attic_work_start_hour", "attic_work_end_hour", "attic_preheat_lead_min",
    "attic_preheat_max_min", "attic_cycle_interval_min", "cycle_interval_min",
}


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    return default if v in ("", "null", "None") else v


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name, str(default)).strip().lower()
    return v in ("true", "1", "yes", "on")


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, _env("LOG_LEVEL", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _options_from_env() -> dict:
    """Odczytuje wszystkie opcje Supervisora z env (wystawionych przez
    run.sh) jako typy Pythona — startowy seed dla tabeli `settings`."""
    opts = {
        "day_zone_temp_entities": _env("DAY_ZONE_TEMP_ENTITIES"),
        "outdoor_temp_entity": _env("OUTDOOR_TEMP_ENTITY"),
        "weather_entity": _env("WEATHER_ENTITY"),
        "solcast_remaining_today_entity": _env("SOLCAST_REMAINING_TODAY_ENTITY"),
        "tariff_state_entity": _env("TARIFF_STATE_ENTITY"),
        "tariff_price_entity": _env("TARIFF_PRICE_ENTITY"),
        "pump_energy_entity": _env("PUMP_ENERGY_ENTITY"),
        "heiko_setpoint_entity": _env("HEIKO_SETPOINT_ENTITY"),
        "heiko_curve_switch_entity": _env("HEIKO_CURVE_SWITCH_ENTITY"),
        "heiko_active_profile": _env("HEIKO_ACTIVE_PROFILE", "ekonomia"),
        "attic_ac_entity": _env("ATTIC_AC_ENTITY"),
        "attic_temp_entity": _env("ATTIC_TEMP_ENTITY"),
        "attic_door_entity": _env("ATTIC_DOOR_ENTITY"),
        "attic_window_entities": _env("ATTIC_WINDOW_ENTITIES"),
        "attic_vacation_entity": _env("ATTIC_VACATION_ENTITY"),
        "attic_power_entity": _env("ATTIC_POWER_ENTITY"),
        "attic_active_profile": _env("ATTIC_ACTIVE_PROFILE", "ekonomia"),
        "notify_service": _env("NOTIFY_SERVICE"),
    }
    for key in _BOOL_KEYS:
        opts[key] = _env_bool(key.upper(), False)
    for key in _FLOAT_KEYS:
        try:
            opts[key] = float(_env(key.upper(), "0") or 0)
        except ValueError:
            opts[key] = 0.0
    for key in _INT_KEYS:
        try:
            opts[key] = int(_env(key.upper(), "0") or 0)
        except ValueError:
            opts[key] = 0
    return opts


def main() -> None:
    _setup_logging()
    logger.info("Heiko Predictive Control %s startuje", __version__)

    db_path = _env("DB_PATH", "/data/heiko_predictive_control.db")
    conn = dbm.get_conn(db_path)
    dbm.migrate(conn)
    dbm.seed_from_options(conn, _options_from_env())
    conn.close()

    def get_settings() -> dict:
        c = dbm.get_conn(db_path)
        try:
            return dbm.get_all_settings(c)
        finally:
            c.close()

    def set_setting(key: str, value) -> None:
        c = dbm.get_conn(db_path)
        try:
            dbm.set_setting(c, key, value)
        finally:
            c.close()

    mqtt_host = _env("MQTT_HOST", "core-mosquitto")
    mqtt_port = int(_env("MQTT_PORT", "1883") or 1883)
    mqtt_user = _env("MQTT_USER")
    mqtt_password = _env("MQTT_PASSWORD")
    if not mqtt_user:
        svc = ha_client.get_mqtt_service()
        if svc:
            mqtt_host = svc.get("host") or mqtt_host
            mqtt_port = int(svc.get("port") or mqtt_port)
            mqtt_user = svc.get("username") or ""
            mqtt_password = svc.get("password") or ""
            logger.info("MQTT: dane brokera z usługi Supervisora (%s)", mqtt_host)

    mqtt_pub = MQTTPublisher(host=mqtt_host, port=mqtt_port, user=mqtt_user,
                              password=mqtt_password, version=__version__)
    mqtt_pub.connect()

    heiko_model = ThermalModel.from_dict(get_settings().get("heiko_model_state"))

    def run_heiko() -> None:
        c = dbm.get_conn(db_path)
        try:
            settings = dbm.get_all_settings(c)
            now = datetime.now()
            heiko = cycle.run_heiko_cycle(c, settings, heiko_model, now)
            dbm.set_setting(c, "heiko_model_state", heiko_model.as_dict())
            mqtt_pub.publish_values({
                "heiko_write_enabled": settings.get("heiko_enabled"),
                "heiko_active_profile": heiko.get("active_profile"),
                "heiko_setpoint_komfort": heiko.get("setpoint_komfort"),
                "heiko_setpoint_ekonomia": heiko.get("setpoint_ekonomia"),
                "heiko_model_k_loss": round(heiko_model.k_loss, 4),
                "heiko_model_k_gain": round(heiko_model.k_gain, 4),
                "last_cycle_ts": now.isoformat(),
            })
        except Exception:
            logger.exception("Cykl pętli A (Heiko) nieudany")
        finally:
            c.close()

    def run_attic() -> None:
        c = dbm.get_conn(db_path)
        try:
            settings = dbm.get_all_settings(c)
            now = datetime.now()
            is_workday = ha_client.get_bool_state("binary_sensor.workday")
            vacation_entity = settings.get("attic_vacation_entity")
            on_vacation = ha_client.get_bool_state(vacation_entity) if vacation_entity else False
            attic = cycle.run_attic_cycle(c, settings, now, is_workday, on_vacation)
            today = dbm.attic_today_totals(c, now.date().isoformat())
            state = attic_mod.AtticState.from_dict(dbm.get_setting(c, "attic_ctrl_state"))
            planned = attic.get("planned_start")
            mqtt_pub.publish_values({
                "attic_write_enabled": settings.get("attic_enabled"),
                "attic_active_profile": attic.get("active_profile"),
                "attic_target_komfort": attic.get("setpoint_komfort"),
                "attic_target_ekonomia": attic.get("setpoint_ekonomia"),
                "attic_phase": attic.get("phase"),
                "attic_owned": state.owned,
                "attic_offset": round(state.offset_c, 2),
                "attic_heat_rate": round(state.heat_rate_c_h, 2),
                "attic_ac_setpoint_cmd": attic.get("ac_cmd_setpoint"),
                "attic_planned_start": (datetime.fromisoformat(planned).astimezone().isoformat()
                                        if planned else None),
                "attic_energy_today": round(today["kwh"], 3),
                "attic_cost_today": round(today["pln"], 2),
            })
        except Exception:
            logger.exception("Cykl pętli B (AC poddasza) nieudany")
        finally:
            c.close()

    initial = get_settings()
    scheduler = BackgroundScheduler(timezone=_env("TZ", "Europe/Warsaw"))
    scheduler.add_job(run_heiko, "interval", minutes=initial.get("cycle_interval_min") or 15,
                       next_run_time=datetime.now(), max_instances=1, coalesce=True)
    scheduler.add_job(run_attic, "interval", minutes=initial.get("attic_cycle_interval_min") or 5,
                       next_run_time=datetime.now(), max_instances=1, coalesce=True)
    scheduler.start()

    # Układ domu z prywatnej konfiguracji HA (repo add-onu jest publiczne).
    house, house_warnings = layout.load_house(_env("HOUSE_LAYOUT_FILE"))
    logger.info("Układ domu: %s (%d pomieszczeń)", house.source, len(house.rooms))

    app = create_app(db_path=db_path, get_settings=get_settings,
                      set_setting=set_setting, house=house, house_warnings=house_warnings)
    logger.info("Web UI nasłuchuje na :8101 (ingress)")
    app.run(host="0.0.0.0", port=8101, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
