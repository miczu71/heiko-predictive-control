from datetime import datetime

from heiko_predictive_control.tariff import (
    is_peak_hour, price_for, schedule, peak_offpeak_hour_counts,
    DEFAULT_PEAK_PRICE_PLN, DEFAULT_OFFPEAK_PRICE_PLN,
)


def test_peak_hours_workday():
    assert is_peak_hour(6, True) is True
    assert is_peak_hour(12, True) is True
    assert is_peak_hour(13, True) is False  # [start, stop) — 13 już nie w szczycie
    assert is_peak_hour(14, True) is False
    assert is_peak_hour(15, True) is True
    assert is_peak_hour(21, True) is True
    assert is_peak_hour(22, True) is False
    assert is_peak_hour(5, True) is False


def test_weekend_never_peak():
    for h in range(24):
        assert is_peak_hour(h, False) is False


def test_price_for():
    assert price_for(True) == DEFAULT_PEAK_PRICE_PLN
    assert price_for(False) == DEFAULT_OFFPEAK_PRICE_PLN


def test_schedule_and_counts_match():
    start = datetime(2026, 1, 12, 5, 30)  # poniedziałek

    def workday(_d):
        return True

    slots = schedule(start, 24, workday)
    assert len(slots) == 24
    assert slots[0].hour_start.hour == 5  # zaokrąglone w dół do pełnej godziny
    assert slots[0].is_peak is False
    assert slots[1].is_peak is True  # godzina 6

    peak, offpeak = peak_offpeak_hour_counts(start, 24, workday)
    assert peak + offpeak == 24
    assert peak == sum(1 for s in slots if s.is_peak)


def test_schedule_weekend_all_offpeak():
    start = datetime(2026, 1, 10, 0, 0)  # sobota

    def weekend(_d):
        return False

    peak, offpeak = peak_offpeak_hour_counts(start, 24, weekend)
    assert peak == 0
    assert offpeak == 24
