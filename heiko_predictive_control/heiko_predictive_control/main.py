"""Punkt startowy add-onu: baza, MQTT, cykl decyzyjny (APScheduler), Flask.

Pętla A (Heiko) jest nadal WYŁĄCZNIE obserwacją (od 0.6.0 plan blokowy na modelu
podłogówki) — `cycle.run_heiko_cycle` nigdy nie woła `ha_client.call_service`. Od 0.8.0 (Doradca, D1)
dochodzi telemetria pompy, dziennik zmian parametrów i dobowe streszczenia — wyłącznie odczyt. Pętla B (AC poddasza, od 0.4.0) zapisuje do
klimatyzatora w `cycle.run_attic_cycle`, tylko przy `attic_enabled`. Obie pętle
mają osobne zadania harmonogramu (A: `cycle_interval_min`, B: `attic_cycle_interval_min`)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import __version__, comfort, cycle, floor_learn, ha_client, kpi, layout, summaries, telemetry
from . import attic as attic_mod
from . import db as dbm
from .publisher import MQTTPublisher
from .web import create_app

logger = logging.getLogger("heiko_predictive_control")

_BOOL_KEYS = {"heiko_enabled", "attic_enabled"}
_FLOAT_KEYS = {
    "heiko_comfort_band_day_c", "heiko_comfort_band_night_c",
    "heiko_economy_band_day_c", "heiko_economy_band_night_c",
    "attic_comfort_target_c", "attic_economy_target_c",
    "heiko_room_target_c", "heiko_room_min_c", "heiko_water_min_c", "heiko_water_max_c",
}
_INT_KEYS = {
    "heiko_day_start_hour", "heiko_day_end_hour", "heiko_write_throttle_min",
    "attic_work_start_hour", "attic_work_end_hour", "attic_preheat_lead_min",
    "attic_preheat_max_min", "attic_cycle_interval_min", "attic_vacant_after_min",
    "cycle_interval_min",
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
        "heiko_curve_ambient_entities": _env("HEIKO_CURVE_AMBIENT_ENTITIES"),
        "heiko_curve_water_entities": _env("HEIKO_CURVE_WATER_ENTITIES"),
        "heiko_water_temp_entity": _env("HEIKO_WATER_TEMP_ENTITY"),
        "heiko_working_mode_entity": _env("HEIKO_WORKING_MODE_ENTITY"),
        "heiko_bootstrap_outdoor_entity": _env("HEIKO_BOOTSTRAP_OUTDOOR_ENTITY"),
        "heiko_water_setpoint_entity": _env("HEIKO_WATER_SETPOINT_ENTITY"),
        "heiko_curve_shift_entity": _env("HEIKO_CURVE_SHIFT_ENTITY"),
        "heiko_active_profile": _env("HEIKO_ACTIVE_PROFILE", "ekonomia"),
        "attic_ac_entity": _env("ATTIC_AC_ENTITY"),
        "attic_temp_entity": _env("ATTIC_TEMP_ENTITY"),
        "attic_door_entity": _env("ATTIC_DOOR_ENTITY"),
        "attic_window_entities": _env("ATTIC_WINDOW_ENTITIES"),
        "attic_vacation_entity": _env("ATTIC_VACATION_ENTITY"),
        "attic_pause_entity": _env("ATTIC_PAUSE_ENTITY"),
        "attic_presence_entity": _env("ATTIC_PRESENCE_ENTITY"),
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

    def run_heiko() -> None:
        c = dbm.get_conn(db_path)
        try:
            settings = dbm.get_all_settings(c)
            now = datetime.now()
            heiko = cycle.run_heiko_cycle(c, settings, now)
            plan = dbm.get_setting(c, "heiko_plan") or {}
            summary = plan.get("summary") or {}
            model = floor_learn.load_model(c)
            comfort.comfort_alarm(c, settings, now)                  # tylko powiadomienie, bez zapisu do pompy
            recent = kpi.recent_kpi(c, now, settings.get("peak_baseline"))
            mqtt_pub.publish_values({
                "heiko_write_enabled": settings.get("heiko_enabled"),
                "heiko_active_profile": heiko.get("active_profile"),
                "heiko_setpoint_komfort": heiko.get("setpoint_komfort"),
                "heiko_setpoint_ekonomia": heiko.get("setpoint_ekonomia"),
                "heiko_savings_today_komfort": (summary.get("komfort") or {}).get("saving_pln"),
                "heiko_savings_today_ekonomia": (summary.get("ekonomia") or {}).get("saving_pln"),
                "heiko_savings_shift_komfort": (summary.get("komfort") or {}).get("saving_shift_pln"),
                "heiko_savings_shift_ekonomia": (summary.get("ekonomia") or {}).get("saving_shift_pln"),
                "heiko_model_k_loss": round(model.c, 4),
                "heiko_model_k_gain": round(model.g, 4),
                "heiko_model_tau": round(model.tau_h, 2),
                "heiko_model_rmse": model.rmse_c,
                "heiko_model_source": model.source,
                "heiko_min_room": heiko.get("min_room_c"),
                "heiko_fuse": plan.get("fuse_active", False),
                "heiko_water_setpoint": heiko.get("water_setpoint_c"),
                "heiko_reduced_state": {1: "aktywna", 0: "nieaktywna"}.get(heiko.get("reduced_active"), "nieznany"),
                "heiko_peak_share": None if not recent else round(recent["share"] * 100, 1),
                "heiko_peak_share_baseline": (None if not recent or recent["baseline_share"] is None
                                              else round(recent["baseline_share"] * 100, 1)),
                "last_cycle_ts": now.astimezone().isoformat(),   # HA odrzuca timestamp bez strefy
            })
        except Exception:
            logger.exception("Cykl pętli A (Heiko) nieudany")
        finally:
            c.close()

    def bootstrap_model() -> None:
        """Jednorazowo (do skutku) dopasuj model na LTS zeszłej zimy."""
        c = dbm.get_conn(db_path)
        try:
            if dbm.get_setting(c, "floor_bootstrap") is None:
                floor_learn.bootstrap(c, dbm.get_all_settings(c), datetime.now())
        except Exception:
            logger.exception("Bootstrap modelu podłogówki nieudany")
        finally:
            c.close()

    def peak_baseline() -> None:
        """Jednorazowo (do skutku) policz bazę udziału szczytu z LTS ostatniego sezonu."""
        c = dbm.get_conn(db_path)
        try:
            kpi.ensure_peak_baseline(c, dbm.get_all_settings(c), datetime.now())
        except Exception:
            logger.exception("Baza udziału szczytu nieudana")
        finally:
            c.close()

    def refit_model() -> None:
        """Dobowe dopasowanie modelu na własnych cyklach (faza cienia)."""
        c = dbm.get_conn(db_path)
        try:
            floor_learn.refit_from_cycles(c, dbm.get_all_settings(c), datetime.now())
        except Exception:
            logger.exception("Dobowe dopasowanie modelu podłogówki nieudane")
        finally:
            c.close()

    def run_telemetry() -> None:
        """Próbka telemetrii pompy + dziennik zmian parametrów (tylko odczyt z HA)."""
        c = dbm.get_conn(db_path)
        try:
            telemetry.run(c, dbm.get_all_settings(c), datetime.now())
        except Exception:
            logger.exception("Telemetria pompy nieudana")
        finally:
            c.close()

    def daily_summaries() -> None:
        """Streszczenia dobowe (sprężarka, CWU, HBH, P0, energia) z historii HA — brakujące doby z ostatnich 7."""
        c = dbm.get_conn(db_path)
        try:
            now = datetime.now()
            settings = dbm.get_all_settings(c)
            states = ha_client.get_all_states() or []
            workday_cache: dict = {}

            def is_workday(d) -> bool:
                if d not in workday_cache:
                    verdict = ha_client.check_workday(d.isoformat())
                    workday_cache[d] = d.weekday() < 5 if verdict is None else verdict
                return workday_cache[d]

            for day in summaries.missing_days(c, now):
                done = summaries.store_day(c, settings, day, states, now, is_workday=is_workday)
                logger.info("Streszczenie doby %s: %s", day, sorted(done) or "brak danych")
        except Exception:
            logger.exception("Streszczenia dobowe nieudane")
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
                "attic_last_cycle_ts": now.astimezone().isoformat(),
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
    scheduler.add_job(run_telemetry, "interval", minutes=initial.get("cycle_interval_min") or 15,
                       next_run_time=datetime.now() + timedelta(seconds=10), max_instances=1, coalesce=True)
    scheduler.add_job(daily_summaries, "cron", hour=0, minute=20, max_instances=1, coalesce=True)
    scheduler.add_job(daily_summaries, "date", run_date=datetime.now() + timedelta(seconds=60))
    scheduler.add_job(refit_model, "cron", hour=4, minute=30, max_instances=1, coalesce=True)
    scheduler.add_job(bootstrap_model, "date", run_date=datetime.now() + timedelta(seconds=20))
    scheduler.add_job(peak_baseline, "date", run_date=datetime.now() + timedelta(seconds=45))
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
