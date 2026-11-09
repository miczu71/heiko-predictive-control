"""Atrapa `TariffFeed` do testów kosztów: stały harmonogram (szczyt 6-13 i 15-22 w dni robocze) z podanymi cenami.
Nie jest zapasem w kodzie produkcyjnym — tylko źródłem cen w testach; `missing=True` = brak wierszy taryfy."""
from types import SimpleNamespace


class StubFeed:
    def __init__(self, peak_price=2.0, offpeak_price=1.0, missing=False):
        self.peak_price, self.off, self.missing = peak_price, offpeak_price, missing

    def at(self, dt):
        if self.missing:
            return None
        peak = dt.weekday() < 5 and (6 <= dt.hour < 13 or 15 <= dt.hour < 22)
        return SimpleNamespace(is_peak=peak, zone="peak" if peak else "offpeak",
                               gross=self.peak_price if peak else self.off)

    def is_peak(self, dt):
        row = self.at(dt)
        return None if row is None else row.is_peak

    def price(self, dt):
        row = self.at(dt)
        return None if row is None else row.gross

    def offpeak_price(self, dt):
        return None if self.missing else self.off
