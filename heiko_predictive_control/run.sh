#!/bin/sh
set -e

CONFIG="/data/options.json"

export HOUSE_LAYOUT_FILE=$(jq -r '.house_layout_file // ""' "$CONFIG")
export DAY_ZONE_TEMP_ENTITIES=$(jq -r '.day_zone_temp_entities // ""' "$CONFIG")
export OUTDOOR_TEMP_ENTITY=$(jq -r '.outdoor_temp_entity // ""' "$CONFIG")
export WEATHER_ENTITY=$(jq -r '.weather_entity // ""' "$CONFIG")
export SOLCAST_REMAINING_TODAY_ENTITY=$(jq -r '.solcast_remaining_today_entity // ""' "$CONFIG")
export TARIFF_STATE_ENTITY=$(jq -r '.tariff_state_entity // ""' "$CONFIG")
export TARIFF_PRICE_ENTITY=$(jq -r '.tariff_price_entity // ""' "$CONFIG")
export PUMP_ENERGY_ENTITY=$(jq -r '.pump_energy_entity // ""' "$CONFIG")
export HEIKO_SETPOINT_ENTITY=$(jq -r '.heiko_setpoint_entity // ""' "$CONFIG")
export HEIKO_CURVE_SWITCH_ENTITY=$(jq -r '.heiko_curve_switch_entity // ""' "$CONFIG")
export HEIKO_COMFORT_BAND_DAY_C=$(jq -r '.heiko_comfort_band_day_c // "1.0"' "$CONFIG")
export HEIKO_COMFORT_BAND_NIGHT_C=$(jq -r '.heiko_comfort_band_night_c // "1.5"' "$CONFIG")
export HEIKO_ECONOMY_BAND_DAY_C=$(jq -r '.heiko_economy_band_day_c // "1.5"' "$CONFIG")
export HEIKO_ECONOMY_BAND_NIGHT_C=$(jq -r '.heiko_economy_band_night_c // "2.0"' "$CONFIG")
export HEIKO_DAY_START_HOUR=$(jq -r '.heiko_day_start_hour // "6"' "$CONFIG")
export HEIKO_DAY_END_HOUR=$(jq -r '.heiko_day_end_hour // "22"' "$CONFIG")
export HEIKO_WRITE_THROTTLE_MIN=$(jq -r '.heiko_write_throttle_min // "15"' "$CONFIG")
export HEIKO_ENABLED=$(jq -r '.heiko_enabled // "false"' "$CONFIG")
export HEIKO_ACTIVE_PROFILE=$(jq -r '.heiko_active_profile // "ekonomia"' "$CONFIG")
export ATTIC_AC_ENTITY=$(jq -r '.attic_ac_entity // ""' "$CONFIG")
export ATTIC_TEMP_ENTITY=$(jq -r '.attic_temp_entity // ""' "$CONFIG")
export ATTIC_DOOR_ENTITY=$(jq -r '.attic_door_entity // ""' "$CONFIG")
export ATTIC_POWER_ENTITY=$(jq -r '.attic_power_entity // ""' "$CONFIG")
export ATTIC_WORK_START_HOUR=$(jq -r '.attic_work_start_hour // "8"' "$CONFIG")
export ATTIC_WORK_END_HOUR=$(jq -r '.attic_work_end_hour // "16"' "$CONFIG")
export ATTIC_COMFORT_TARGET_C=$(jq -r '.attic_comfort_target_c // "22.0"' "$CONFIG")
export ATTIC_ECONOMY_TARGET_C=$(jq -r '.attic_economy_target_c // "20.5"' "$CONFIG")
export ATTIC_PREHEAT_LEAD_MIN=$(jq -r '.attic_preheat_lead_min // "45"' "$CONFIG")
export ATTIC_ENABLED=$(jq -r '.attic_enabled // "false"' "$CONFIG")
export ATTIC_ACTIVE_PROFILE=$(jq -r '.attic_active_profile // "ekonomia"' "$CONFIG")
export CYCLE_INTERVAL_MIN=$(jq -r '.cycle_interval_min // "15"' "$CONFIG")
export NOTIFY_SERVICE=$(jq -r '.notify_service // ""' "$CONFIG")
export MQTT_HOST=$(jq -r '.mqtt_host // "core-mosquitto"' "$CONFIG")
export MQTT_PORT=$(jq -r '.mqtt_port // "1883"' "$CONFIG")
export MQTT_USER=$(jq -r '.mqtt_user // ""' "$CONFIG")
export MQTT_PASSWORD=$(jq -r '.mqtt_password // ""' "$CONFIG")
export LOG_LEVEL=$(jq -r '.log_level // "info"' "$CONFIG")
export TZ=$(jq -r '.timezone // "Europe/Warsaw"' "$CONFIG")

export DB_PATH="/data/heiko_predictive_control.db"

exec python3 -m heiko_predictive_control.main
