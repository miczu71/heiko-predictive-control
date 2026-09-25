"""Harmonogram taryfy G12w — deterministyczny, znany z wyprzedzeniem (nie
trzeba go prognozować, w przeciwieństwie do cen giełdowych). Replikuje logikę
`sensor.power_tauron_g12w_current_tariff` z packages/energy_simulation.yaml
tego configu — ta sama definicja szczytu, żeby projekcja kosztu na resztę dnia
się zgadzała z tym, co user widzi w HA."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# (godzina_start, godzina_stop) — przedział [start, stop), jak w Jinja
# energy_simulation.yaml. Dwa okna szczytu w dni robocze.
PEAK_WINDOWS: tuple[tuple[int, int], ...] = ((6, 13), (15, 22))

DEFAULT_PEAK_PRICE_PLN = 1.2304
DEFAULT_OFFPEAK_PRICE_PLN = 0.6306


def is_peak_hour(hour: int, is_workday: bool) -> bool:
    if not is_workday:
        return False
    return any(start <= hour < stop for start, stop in PEAK_WINDOWS)


def price_for(is_peak: bool, peak_price: float = DEFAULT_PEAK_PRICE_PLN,
              offpeak_price: float = DEFAULT_OFFPEAK_PRICE_PLN) -> float:
    return peak_price if is_peak else offpeak_price


@dataclass
class HourSlot:
    hour_start: datetime
    is_workday: bool
    is_peak: bool
    price_pln_kwh: float


def schedule(from_dt: datetime, hours_ahead: int, is_workday_fn,
             peak_price: float = DEFAULT_PEAK_PRICE_PLN,
             offpeak_price: float = DEFAULT_OFFPEAK_PRICE_PLN) -> list[HourSlot]:
    """Harmonogram godzina-po-godzinie od `from_dt` (zaokrąglone w dół do
    pełnej godziny) na `hours_ahead` godzin naprzód.

    `is_workday_fn(date) -> bool` wstrzykiwane z zewnątrz (HA
    binary_sensor.workday zna święta/kalendarz — tu nie replikujemy tej
    wiedzy, tylko schemat godzinowy szczytu)."""
    start = from_dt.replace(minute=0, second=0, microsecond=0)
    slots: list[HourSlot] = []
    for i in range(hours_ahead):
        hour_dt = start + timedelta(hours=i)
        workday = is_workday_fn(hour_dt.date())
        peak = is_peak_hour(hour_dt.hour, workday)
        slots.append(HourSlot(
            hour_start=hour_dt, is_workday=workday, is_peak=peak,
            price_pln_kwh=price_for(peak, peak_price, offpeak_price),
        ))
    return slots


def peak_offpeak_hour_counts(from_dt: datetime, hours_ahead: int,
                              is_workday_fn) -> tuple[int, int]:
    """(godziny_szczyt, godziny_poza_szczytem) w oknie — do szybkiej
    projekcji kosztu bez pełnej symulacji cykl-po-cyklu."""
    slots = schedule(from_dt, hours_ahead, is_workday_fn)
    peak = sum(1 for s in slots if s.is_peak)
    return peak, hours_ahead - peak
