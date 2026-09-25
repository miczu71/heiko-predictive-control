"""Rdzeń cyklu decyzyjnego — funkcje czyste (testowalne bez HA) + cienki
orkiestrator I/O. Etap 1: WYŁĄCZNIE dry-run. `run_cycle()` nigdy nie woła
`ha_client.call_service` — nie ma tu w ogóle takiej ścieżki kodu."""
from __future__ import annotations

import logging
from datetime import datetime

from . import db as dbm
from . import ha_client
from .model import ThermalModel
from .profiles import AtticProfiles, HeikoProfiles
from .tariff import DEFAULT_OFFPEAK_PRICE_PLN, DEFAULT_PEAK_PRICE_PLN, price_for

logger = logging.getLogger(__name__)


# ── Pętla A (Heiko / podłogówka) — funkcje czyste ───────────────────────────

def simulated_setpoint_c(baseline_c: float, band_c: float, is_peak: bool) -> float:
    """W szczycie zbij o pełne pasmo, poza szczytem podbij o pełne pasmo —
    najprostsza reguła, która wykorzystuje całe dostępne pasmo komfortu w obu
    kierunkach (nie tylko połowę). Model termiczny (predict_delta_c) mówi,
    ile to naprawdę zmieni temperaturę — to tu tylko wybór punktu w paśmie."""
    return baseline_c - band_c if is_peak else baseline_c + band_c


def simulate_cost_increment_pln(observed_energy_kwh: float, price_pln_kwh: float,
                                 outdoor_c: float, baseline_setpoint_c: float,
                                 sim_setpoint_c: float) -> float:
    """Pierwszy-rzut proxy: energia pompy ciepła w przybliżeniu proporcjonalna
    do różnicy stopni-grzania (setpoint - outdoor), skalujemy zaobserwowane
    zużycie tego cyklu tym stosunkiem. Świadome uproszczenie na Etap 1 —
    dokładniejsze dopiero gdy Etap 3 zacznie realnie sterować (obserwacja
    rzeczywistej reakcji zamiast ekstrapolacji). Chronione przed dzieleniem
    przez ~0 (baseline blisko outdoor = pompa i tak prawie nie grzeje)."""
    baseline_drive = baseline_setpoint_c - outdoor_c
    if baseline_drive <= 0.5 or observed_energy_kwh <= 0:
        return 0.0
    sim_drive = max(sim_setpoint_c - outdoor_c, 0.0)
    scale = sim_drive / baseline_drive
    return observed_energy_kwh * scale * price_pln_kwh


def average_temp(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


# ── Pętla B (AC poddasze) — funkcje czyste ──────────────────────────────────

def attic_should_run(now: datetime, work_start_hour: int, work_end_hour: int,
                      preheat_lead_min: int, is_workday: bool) -> bool:
    """True w oknie [work_start - lead, work_end) w dzień roboczy."""
    if not is_workday:
        return False
    lead_hours = preheat_lead_min / 60.0
    start = now.replace(hour=work_start_hour, minute=0, second=0, microsecond=0)
    start -= _timedelta_hours(lead_hours)
    end = now.replace(hour=work_end_hour, minute=0, second=0, microsecond=0)
    return start <= now < end


def _timedelta_hours(hours: float):
    from datetime import timedelta
    return timedelta(hours=hours)


# ── Orkiestrator I/O (Etap 1: tylko odczyt + zapis do własnej bazy) ─────────

def run_heiko_cycle(conn, settings: dict, model: ThermalModel,
                     now: datetime,
                     get_state=ha_client.get_state,
                     get_numeric=ha_client.get_numeric_state,
                     get_bool=ha_client.get_bool_state) -> dict:
    day_entities = [e.strip() for e in
                    str(settings.get("day_zone_temp_entities", "")).split(",") if e.strip()]
    indoor_c = average_temp([get_numeric(e) for e in day_entities])
    outdoor_c = get_numeric(settings.get("outdoor_temp_entity", ""))
    baseline_setpoint_c = get_numeric(settings.get("heiko_setpoint_entity", ""))
    is_peak = get_bool(settings.get("tariff_state_entity", ""))
    price = get_numeric(settings.get("tariff_price_entity", ""))
    if price is None:
        price = price_for(bool(is_peak), DEFAULT_PEAK_PRICE_PLN, DEFAULT_OFFPEAK_PRICE_PLN)

    energy_now = get_numeric(settings.get("pump_energy_entity", ""))

    result = {
        "ts": now.isoformat(), "loop": "heiko",
        "active_profile": settings.get("heiko_active_profile", "ekonomia"),
        "tariff_peak": int(bool(is_peak)) if is_peak is not None else None,
        "price_pln_kwh": price, "outdoor_temp_c": outdoor_c,
        "indoor_temp_c": indoor_c, "write_enabled": int(bool(settings.get("heiko_enabled"))),
    }

    if indoor_c is None or outdoor_c is None or baseline_setpoint_c is None or is_peak is None:
        logger.warning("Heiko: brakujące dane wejściowe, pomijam cykl (indoor=%s outdoor=%s "
                        "setpoint=%s peak=%s)", indoor_c, outdoor_c, baseline_setpoint_c, is_peak)
        dbm.insert_cycle(conn, result)
        return result

    profiles = HeikoProfiles.from_settings(settings)
    hour = now.hour
    day_start = int(settings.get("heiko_day_start_hour", 6))
    day_end = int(settings.get("heiko_day_end_hour", 22))

    prev_row = dbm.latest_cycle(conn, "heiko")
    prev_energy = _get_last_energy(conn)

    cycle_interval_min = float(settings.get("cycle_interval_min", 15))
    dt_hours = cycle_interval_min / 60.0

    for profile_name, band in (("komfort", profiles.komfort), ("ekonomia", profiles.ekonomia)):
        band_c = band.for_hour(hour, day_start, day_end)
        sim_setpoint = simulated_setpoint_c(baseline_setpoint_c, band_c, bool(is_peak))
        result[f"setpoint_{profile_name}"] = round(sim_setpoint, 2)

    observed_energy_kwh = 0.0
    if energy_now is not None and prev_energy is not None and energy_now >= prev_energy:
        observed_energy_kwh = energy_now - prev_energy
    baseline_cost = observed_energy_kwh * price
    result["baseline_cost_today_pln"] = baseline_cost

    for profile_name in ("komfort", "ekonomia"):
        sim_setpoint = result[f"setpoint_{profile_name}"]
        result[f"sim_cost_today_{profile_name}_pln"] = simulate_cost_increment_pln(
            observed_energy_kwh, price, outdoor_c, baseline_setpoint_c, sim_setpoint)

    # Aktualizacja modelu z NATURALNEJ reakcji domu na krzywą natywną
    # (heating_active z working_mode, nie z naszego sterowania — Etap 1
    # niczego nie zapisuje, patrz docstring modułu).
    heating_active = _is_heating_active(get_state)
    if prev_row is not None and prev_row["indoor_temp_c"] is not None:
        model.update(
            indoor_before_c=prev_row["indoor_temp_c"], indoor_after_c=indoor_c,
            outdoor_c=outdoor_c, setpoint_c=baseline_setpoint_c,
            dt_hours=dt_hours, heating_active=heating_active,
        )
    result["coefficient"] = model.k_loss if not heating_active else model.k_gain

    dbm.insert_cycle(conn, result)
    if energy_now is not None:
        dbm.set_setting(conn, "last_pump_energy_kwh", energy_now)
    return result


def _get_last_energy(conn) -> float | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'last_pump_energy_kwh'").fetchone()
    return float(row["value"]) if row else None


def _is_heating_active(get_state) -> bool:
    data = get_state("sensor.heiko_heat_pump_working_mode_2")
    if not data:
        return False
    return str(data.get("state", "")).strip().lower() in ("2", "heating")


def run_attic_cycle(conn, settings: dict, now: datetime,
                     is_workday: bool,
                     get_state=ha_client.get_state,
                     get_numeric=ha_client.get_numeric_state) -> dict:
    profiles = AtticProfiles.from_settings(settings)
    indoor_c = get_numeric(settings.get("attic_temp_entity", ""))
    should_run = attic_should_run(
        now, int(settings.get("attic_work_start_hour", 8)),
        int(settings.get("attic_work_end_hour", 16)),
        int(settings.get("attic_preheat_lead_min", 45)), is_workday,
    )
    result = {
        "ts": now.isoformat(), "loop": "attic",
        "active_profile": settings.get("attic_active_profile", "ekonomia"),
        "tariff_peak": None, "price_pln_kwh": None, "outdoor_temp_c": None,
        "indoor_temp_c": indoor_c,
        "write_enabled": int(bool(settings.get("attic_enabled"))),
        "setpoint_komfort": profiles.komfort_target_c if should_run else None,
        "setpoint_ekonomia": profiles.ekonomia_target_c if should_run else None,
        "coefficient": None, "baseline_cost_today_pln": None,
        "sim_cost_today_komfort_pln": None, "sim_cost_today_ekonomia_pln": None,
    }
    dbm.insert_cycle(conn, result)
    return result
