"""MQTT discovery — urządzenie + kilka sensorów diagnostycznych.
Wzorzec jak w fuel_tracker/pv_roi_tracker: paho loop_start(), availability
topic z LWT, discovery retained."""
from __future__ import annotations

import json
import logging
from typing import NamedTuple, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

_DEVICE_ID = "heiko_predictive_control"
_AVAIL_TOPIC = "heiko_predictive_control/availability"
_STATE_PREFIX = "heiko_predictive_control/sensors"
_DISC_PREFIX = "homeassistant"


class _Sensor(NamedTuple):
    slug: str
    name: str
    unit: Optional[str]
    device_class: Optional[str]
    icon: Optional[str]
    is_binary: bool = False


_SENSORS: list[_Sensor] = [
    _Sensor("heiko_write_enabled", "Heiko: sterowanie aktywne", None, None,
            "mdi:radiator", is_binary=True),
    _Sensor("attic_write_enabled", "AC poddasze: sterowanie aktywne", None, None,
            "mdi:air-conditioner", is_binary=True),
    _Sensor("heiko_active_profile", "Heiko: aktywny profil", None, None, "mdi:tune"),
    _Sensor("attic_active_profile", "AC poddasze: aktywny profil", None, None, "mdi:tune"),
    _Sensor("heiko_setpoint_komfort", "Heiko: setpoint symulowany (Komfort)", "°C",
            "temperature", "mdi:thermometer"),
    _Sensor("heiko_setpoint_ekonomia", "Heiko: setpoint symulowany (Ekonomia)", "°C",
            "temperature", "mdi:thermometer"),
    _Sensor("heiko_savings_today_komfort", "Heiko: szac. oszczędność dziś (Komfort)",
            "PLN", "monetary", "mdi:cash-multiple"),
    _Sensor("heiko_savings_today_ekonomia", "Heiko: szac. oszczędność dziś (Ekonomia)",
            "PLN", "monetary", "mdi:cash-multiple"),
    _Sensor("heiko_model_k_loss", "Heiko: współczynnik strat (uczony)", None, None,
            "mdi:chart-line"),
    _Sensor("heiko_model_k_gain", "Heiko: współczynnik zysku (uczony)", None, None,
            "mdi:chart-line"),
    _Sensor("attic_target_komfort", "AC poddasze: cel symulowany (Komfort)", "°C",
            "temperature", "mdi:thermometer"),
    _Sensor("attic_target_ekonomia", "AC poddasze: cel symulowany (Ekonomia)", "°C",
            "temperature", "mdi:thermometer"),
    _Sensor("attic_phase", "AC poddasze: faza", None, None, "mdi:state-machine"),
    _Sensor("attic_owned", "AC poddasze: przejęte przez add-on", None, None,
            "mdi:robot", is_binary=True),
    _Sensor("attic_offset", "AC poddasze: offset nastawy (uczony)", "°C", None,
            "mdi:thermometer-plus"),
    _Sensor("attic_heat_rate", "AC poddasze: tempo dogrzewania (uczone)", "°C/h", None,
            "mdi:speedometer"),
    _Sensor("attic_ac_setpoint_cmd", "AC poddasze: ostatnia nastawa zlecona", "°C",
            "temperature", "mdi:thermometer-auto"),
    _Sensor("attic_planned_start", "AC poddasze: planowany start dogrzewania", None,
            "timestamp", "mdi:clock-start"),
    _Sensor("attic_energy_today", "AC poddasze: energia dziś", "kWh", "energy",
            "mdi:lightning-bolt"),
    _Sensor("attic_cost_today", "AC poddasze: koszt dziś", "PLN", "monetary",
            "mdi:cash"),
    _Sensor("attic_last_cycle_ts", "AC poddasze: ostatni cykl", None, "timestamp",
            "mdi:heart-pulse"),
    _Sensor("last_cycle_ts", "Ostatni cykl", None, "timestamp", "mdi:clock-outline"),
]


class MQTTPublisher:
    def __init__(self, host: str, port: int, user: str, password: str,
                 version: str) -> None:
        self._client = mqtt.Client(client_id=_DEVICE_ID)
        if user:
            self._client.username_pw_set(user, password)
        self._client.will_set(_AVAIL_TOPIC, payload="offline", retain=True)
        self._host, self._port = host, port
        self._version = version
        self._connected = False

    def connect(self) -> None:
        try:
            self._client.connect(self._host, self._port, keepalive=60)
            self._client.loop_start()
            self._connected = True
            self._client.publish(_AVAIL_TOPIC, "online", retain=True)
            self._publish_discovery()
        except Exception:
            logger.exception("Połączenie MQTT nieudane")

    def _publish_discovery(self) -> None:
        device = {
            "identifiers": [_DEVICE_ID],
            "name": "Heiko Predictive Control",
            "manufacturer": "miczu71",
            "model": "heiko-predictive-control",
            "sw_version": self._version,
        }
        for s in _SENSORS:
            domain = "binary_sensor" if s.is_binary else "sensor"
            topic = f"{_DISC_PREFIX}/{domain}/{_DEVICE_ID}/{s.slug}/config"
            payload = {
                "name": s.name,
                "unique_id": f"{_DEVICE_ID}_{s.slug}",
                "state_topic": f"{_STATE_PREFIX}/{s.slug}",
                "availability_topic": _AVAIL_TOPIC,
                "device": device,
                "icon": s.icon,
            }
            if s.unit:
                payload["unit_of_measurement"] = s.unit
            if s.device_class:
                payload["device_class"] = s.device_class
            if s.is_binary:
                payload["payload_on"] = "on"
                payload["payload_off"] = "off"
            self._client.publish(topic, json.dumps(payload), retain=True)

    def publish_values(self, values: dict[str, object]) -> None:
        if not self._connected:
            return
        for s in _SENSORS:
            if s.slug not in values or values[s.slug] is None:
                continue
            value = values[s.slug]
            if s.is_binary:
                value = "on" if value else "off"
            self._client.publish(f"{_STATE_PREFIX}/{s.slug}", str(value), retain=True)
