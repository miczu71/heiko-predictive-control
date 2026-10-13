"""Skąd naprawdę przyszła energia pompy w kroku cyklu: sieć, bateria czy PV — i ile kosztowała.

Przypisanie krańcowe (pompa jest „ostatnim odbiornikiem”): gdyby pompa nie pracowała, pobór z sieci
spadłby o tyle, ile go było, potem mniej rozładowałaby się bateria, a resztę oddalibyśmy do sieci.
Z przyrostów liczników w kroku: sieć = min(E, ΔI), bateria = min(reszta, ΔD), PV = reszta.

Wycena: sieć po cenie taryfy z chwili, PV po RCE (utracony przychód z net-billingu),
bateria po średnim ważonym koszcie energii, która do niej weszła (ładowanie z sieci po taryfie,
z nadwyżki PV po RCE). Funkcje czyste, bez I/O."""
from __future__ import annotations

import bisect
from dataclasses import dataclass

ENTITIES = {                     # domyślne encje HA (opcja nadpisująca: <nazwa>_entity)
    "grid_import": "sensor.power_meter_consumption",          # licznik kWh poboru z sieci
    "battery_charge": "sensor.battery_total_charge",          # licznik kWh ładowania baterii
    "battery_discharge": "sensor.battery_total_discharge",    # licznik kWh rozładowania baterii
    "battery_soc": "sensor.battery_state_of_capacity",        # %
    "battery_capacity": "sensor.batteries_rated_capacity",    # Wh
    "rce_price": "sensor.current_rce_price_kwh",              # zł/kWh
}
COUNTERS = ("grid_import", "battery_charge", "battery_discharge")
MAX_STEP_KWH = 10.0              # większy przyrost licznika w jednym kroku = błędny odczyt, krok pomijamy


def entities(settings: dict) -> dict[str, str]:
    return {k: settings.get(f"{k}_entity") or default for k, default in ENTITIES.items()}


def price(grid_kwh: float, battery_kwh: float, pv_kwh: float,
          tariff: float, battery_cost: float, rce: float) -> float:
    """Koszt podzielonej energii. Ujemna RCE = 0: oddana wtedy energia nic nie jest warta."""
    return grid_kwh * tariff + battery_kwh * battery_cost + pv_kwh * max(rce, 0.0)


@dataclass(frozen=True)
class Step:
    load_kwh: float          # energia pompy w kroku
    import_kwh: float        # przyrost licznika poboru z sieci
    charge_kwh: float        # przyrost licznika ładowania baterii
    discharge_kwh: float     # przyrost licznika rozładowania baterii
    soc_kwh: float           # energia w baterii na końcu kroku
    grid_price: float        # cena taryfy, zł/kWh
    rce_price: float         # RCE, zł/kWh


@dataclass(frozen=True)
class Attribution:
    grid_kwh: float
    battery_kwh: float
    pv_kwh: float
    cost_pln: float
    battery_cost: float      # średni koszt energii w baterii po kroku, zł/kWh


def split(load_kwh: float, import_kwh: float, discharge_kwh: float = 0.0) -> tuple[float, float, float]:
    """(sieć, bateria, PV) — przypisanie krańcowe energii odbiornika w kroku."""
    grid = min(load_kwh, max(import_kwh, 0.0))
    battery = min(load_kwh - grid, max(discharge_kwh, 0.0))
    return grid, battery, load_kwh - grid - battery


class Counter:
    """Historia stanu z HA jako schodki (wartość „nosi się” do przodu): odczyt i przyrost licznika."""

    def __init__(self, points):
        pts = sorted((t, v) for t, v in points if v is not None)
        self.times, self.values = [t for t, _ in pts], [v for _, v in pts]

    def at(self, t) -> float | None:
        i = bisect.bisect_right(self.times, t) - 1
        return self.values[i] if i >= 0 else None

    def delta(self, a, b) -> float | None:
        """Przyrost w [a, b]; brak danych albo reset licznika → None."""
        va, vb = self.at(a), self.at(b)
        return None if va is None or vb is None or vb < va else vb - va


def attribute(step: Step, battery_cost: float) -> Attribution:
    grid, battery, pv = split(step.load_kwh, step.import_kwh, step.discharge_kwh)
    cost = price(grid, battery, pv, step.grid_price, battery_cost, step.rce_price)
    return Attribution(grid, battery, pv, cost, _battery_cost_after(step, grid, battery_cost))


def _battery_cost_after(step: Step, load_grid_kwh: float, battery_cost: float) -> float:
    """Średnia ważona: zawartość sprzed ładowania po starej cenie + naładowane w kroku wg źródła
    (pobór z sieci, który został po pompie, idzie najpierw na ładowanie; reszta to nadwyżka PV)."""
    if step.charge_kwh <= 0:
        return battery_cost
    from_grid = min(step.charge_kwh, max(step.import_kwh - load_grid_kwh, 0.0))
    charged_cost = price(from_grid, 0.0, step.charge_kwh - from_grid, step.grid_price, 0.0, step.rce_price)
    kept = max(step.soc_kwh - step.charge_kwh, 0.0)
    return (kept * battery_cost + charged_cost) / (kept + step.charge_kwh)
