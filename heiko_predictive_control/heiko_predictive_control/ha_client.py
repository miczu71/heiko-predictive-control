"""Dostęp do Home Assistant przez Supervisor API (SUPERVISOR_TOKEN).

Odczyt (get_state, get_forecast, check_workday, get_statistics, get_mqtt_service)
oraz zapis (call_service, notify). Zapis do urządzeń woła dziś wyłącznie pętla B (cycle.run_attic_cycle,
klimatyzacja poddasza); pętla A (Heiko) nadal niczego nie zapisuje."""
from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

_BASE = "http://supervisor/core/api"
_WS_TIMEOUT_S = 180


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}",
        "Content-Type": "application/json",
    }


def get_state(entity_id: str) -> dict | None:
    """Stan encji HA albo None (brak encji / brak połączenia)."""
    if not entity_id:
        return None
    try:
        resp = requests.get(f"{_BASE}/states/{entity_id}", headers=_headers(),
                             timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("HA states/%s -> HTTP %d", entity_id, resp.status_code)
    except requests.RequestException as exc:
        logger.warning("HA API niedostępne (states/%s): %s", entity_id, exc)
    return None


def get_numeric_state(entity_id: str) -> float | None:
    data = get_state(entity_id)
    if not data:
        return None
    try:
        return float(data["state"])
    except (KeyError, TypeError, ValueError):
        return None


def get_bool_state(entity_id: str) -> bool | None:
    data = get_state(entity_id)
    if not data:
        return None
    state = str(data.get("state", "")).strip().lower()
    if state in ("true", "on", "1"):
        return True
    if state in ("false", "off", "0"):
        return False
    return None


def get_forecast(entity_id: str, forecast_type: str = "hourly") -> list[dict] | None:
    """Prognoza pogody przez usługę weather.get_forecasts (return_response).
    None przy błędzie/braku encji — wołający ma zawsze fallback (kontynuacja
    ostatniej znanej wartości), pogoda nie jest krytyczna dla Etapu 1."""
    if not entity_id:
        return None
    try:
        resp = requests.post(
            f"{_BASE}/services/weather/get_forecasts?return_response=true",
            headers=_headers(),
            json={"entity_id": entity_id, "type": forecast_type},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("weather.get_forecasts -> HTTP %d", resp.status_code)
            return None
        data = resp.json()
        entry = (data.get("service_response") or {}).get(entity_id, {})
        return entry.get("forecast")
    except requests.RequestException as exc:
        logger.warning("Prognoza pogody niedostępna: %s", exc)
        return None
    except (KeyError, ValueError) as exc:
        logger.warning("Nieoczekiwana odpowiedź weather.get_forecasts: %s", exc)
        return None


def check_workday(date_iso: str, entity_id: str = "binary_sensor.workday") -> bool | None:
    """Czy dany dzień jest roboczy wg integracji workday (uwzględnia święta).
    None przy błędzie — wołający ma fallback (pn–pt)."""
    try:
        resp = requests.post(
            f"{_BASE}/services/workday/check_date?return_response=true",
            headers=_headers(), json={"entity_id": entity_id, "check_date": date_iso},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        value = ((resp.json().get("service_response") or {}).get(entity_id) or {}).get("workday")
        return value if isinstance(value, bool) else None
    except (requests.RequestException, ValueError) as exc:
        logger.warning("workday.check_date(%s) niedostępne: %s", date_iso, exc)
        return None


def get_statistics(statistic_ids: list[str], start_iso: str, end_iso: str,
                   period: str = "hour", types: tuple[str, ...] = ("mean", "change")
                   ) -> dict[str, list[dict]] | None:
    """Statystyki długoterminowe (LTS) — dostępne tylko przez WebSocket API
    (REST ich nie wystawia). Zapytania idą po jednej encji (jedno duże trwa dłużej
    niż limit czasu), w jednym połączeniu. None przy błędzie."""
    try:
        import websocket   # websocket-client; import leniwy, żeby testy nie wymagały biblioteki
    except ImportError:
        logger.warning("Brak biblioteki websocket-client — statystyki LTS niedostępne")
        return None
    ws = None
    try:
        ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=_WS_TIMEOUT_S)
        ws.recv()                                                   # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": os.environ.get("SUPERVISOR_TOKEN", "")}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            logger.warning("WebSocket HA: autoryzacja odrzucona")
            return None
        out: dict[str, list[dict]] = {}
        for msg_id, entity in enumerate(statistic_ids, start=1):
            ws.send(json.dumps({
                "id": msg_id, "type": "recorder/statistics_during_period",
                "start_time": start_iso, "end_time": end_iso,
                "statistic_ids": [entity], "period": period, "types": list(types)}))
            reply = json.loads(ws.recv())
            if not reply.get("success"):
                logger.warning("statistics_during_period(%s) nieudane: %s", entity, reply.get("error"))
                return None
            out.update(reply.get("result") or {})
        return out
    except Exception as exc:                                         # WebSocketException, OSError, ValueError
        logger.warning("Statystyki LTS niedostępne: %s", exc)
        return None
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def call_service(domain: str, service: str, data: dict) -> bool:
    """Wywołanie usługi HA; True gdy HA przyjęło (HTTP < 400)."""
    try:
        resp = requests.post(
            f"{_BASE}/services/{domain}/{service}", headers=_headers(),
            json=data, timeout=10,
        )
        return resp.status_code < 400
    except requests.RequestException as exc:
        logger.warning("call_service %s.%s nieudane: %s", domain, service, exc)
        return False


def notify(service: str, title: str, message: str) -> bool:
    if not service:
        return False
    domain_service = service.replace("notify.", "", 1) if service.startswith(
        "notify.") else service
    return call_service("notify", domain_service, {"title": title, "message": message})


def get_mqtt_service() -> dict | None:
    """Dane brokera MQTT z usługi Supervisora (host/port/username/password)."""
    try:
        resp = requests.get(
            "http://supervisor/services/mqtt",
            headers=_headers(), timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("data")
    except requests.RequestException as exc:
        logger.warning("Usługa MQTT niedostępna: %s", exc)
    return None
