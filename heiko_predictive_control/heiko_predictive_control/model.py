"""Model termiczny obiegu głównego (podłogówka) — 1 para współczynników
(strata/zysk), uczona online (EWMA), zamiast RC-fit offline albo ML.

Uzasadnienie w docs/BLUEPRINT_heiko_predictive_addon.md (repo konfiguracji HA,
sekcja "Jak działają porównywalne systemy" + "Czy stosujemy ML? Nie"): fizyka
budynku jest prosta i dobrze poznana (RC network), literatura MPC dla pomp
ciepła konsekwentnie używa właśnie takich uproszczonych modeli fizycznych, nie
sieci neuronowych — a tu dodatkowo nie ma jeszcze żadnych danych treningowych
z aktywnego sterowania (Etap 1 = tylko odczyt), więc model musi startować od
wartości fizycznie sensownej, nie czekać na dane.

Uproszczony model pierwszego rzędu:
    d(indoor)/dt ≈ k_gain * (setpoint - indoor)          gdy pompa grzeje
    d(indoor)/dt ≈ -k_loss * (indoor - outdoor)           gdy pompa nie grzeje

Oba współczynniki mają jednostkę °C na godzinę na °C różnicy — łatwe do
zweryfikowania na oko ("przy ΔT=10°C i k=0.1, spodziewamy się ~1°C/h zmiany").
"""
from __future__ import annotations

from dataclasses import dataclass

# Granice fizycznej sensowności — chronią przed niestabilnością EWMA przy
# szumie pomiarowym (np. czujnik chwilowo martwy, skok przez otwarte okno).
# Dom dobrze ocieplony z podłogówką: strata wolna, zysk z wylewki też wolny
# (duża bezwładność cieplna betonu) w porównaniu np. z grzejnikami/klimą.
_K_LOSS_MIN, _K_LOSS_MAX = 0.01, 1.0
_K_GAIN_MIN, _K_GAIN_MAX = 0.02, 1.5

# Seed startowy — rząd wielkości dla dobrze ocieplonego domu z wolną
# bezwładnością wylewki; patrz docstring modułu. Samokorygujący się od
# pierwszego cyklu EWMA, nie wymaga bycia dokładnym.
DEFAULT_K_LOSS = 0.08
DEFAULT_K_GAIN = 0.15

_DEFAULT_EWMA_ALPHA = 0.15


@dataclass
class ThermalModel:
    k_loss: float = DEFAULT_K_LOSS
    k_gain: float = DEFAULT_K_GAIN
    alpha: float = _DEFAULT_EWMA_ALPHA
    samples_seen: int = 0

    def predict_delta_c(self, indoor_c: float, outdoor_c: float,
                         setpoint_c: float, dt_hours: float,
                         heating_active: bool) -> float:
        """Przewidywana zmiana temperatury wewnętrznej w oknie `dt_hours`."""
        if dt_hours <= 0:
            return 0.0
        if heating_active:
            rate = self.k_gain * (setpoint_c - indoor_c)
        else:
            rate = -self.k_loss * (indoor_c - outdoor_c)
        return rate * dt_hours

    def update(self, indoor_before_c: float, indoor_after_c: float,
               outdoor_c: float, setpoint_c: float, dt_hours: float,
               heating_active: bool) -> None:
        """Aktualizacja EWMA na podstawie zaobserwowanej realnej zmiany.
        Próbki z dt<=0, zerowym ΔT napędowym (dzielenie przez ~0) albo
        wynikiem poza granicami fizycznej sensowności są odrzucane — lepiej
        pominąć cykl niż zepsuć współczynnik jednym szumem pomiarowym."""
        if dt_hours <= 0:
            return
        observed_rate = (indoor_after_c - indoor_before_c) / dt_hours

        if heating_active:
            drive = setpoint_c - indoor_before_c
            if abs(drive) < 0.5:
                return
            sample = observed_rate / drive
            if not (_K_GAIN_MIN <= sample <= _K_GAIN_MAX):
                return
            self.k_gain = (1 - self.alpha) * self.k_gain + self.alpha * sample
        else:
            drive = indoor_before_c - outdoor_c
            if drive < 0.5:
                return
            sample = -observed_rate / drive
            if not (_K_LOSS_MIN <= sample <= _K_LOSS_MAX):
                return
            self.k_loss = (1 - self.alpha) * self.k_loss + self.alpha * sample

        self.samples_seen += 1

    def as_dict(self) -> dict[str, float | int]:
        return {"k_loss": round(self.k_loss, 5), "k_gain": round(self.k_gain, 5),
                "samples_seen": self.samples_seen}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ThermalModel":
        if not data:
            return cls()
        return cls(
            k_loss=float(data.get("k_loss", DEFAULT_K_LOSS)),
            k_gain=float(data.get("k_gain", DEFAULT_K_GAIN)),
            samples_seen=int(data.get("samples_seen", 0)),
        )
