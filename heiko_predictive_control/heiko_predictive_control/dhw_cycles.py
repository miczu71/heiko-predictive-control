"""Dziennik cykli CWU i grzałki AH (A2, plan `docs/PLAN_heiko_backup_sources_ah.md`) — funkcje czyste
+ jedna funkcja I/O (`store_cycles`). Automatyzuje tabele budowane dotąd ręcznie z recordera
(`docs/HEIKO_AH_cost_report.md` §9.1/§10/§11): dla każdego cyklu CWU (`working_mode` w DHW_CODES)
minuta i minuty pracy AH, kWh/koszt z fazy C, i konfiguracja slotów 49/50/52 w chwili startu.
**Tylko odczyt** — żadnej ścieżki zapisu do pompy. Zero propozycji: to surowe dane pod A4 (decyzja
usera o polityce AH), nie analizator doradcy."""
from __future__ import annotations

import bisect
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import catalog
from . import ha_client
from .summaries import DHW_CODES, day_bounds
from .tariff import DEFAULT_OFFPEAK_PRICE_PLN, DEFAULT_PEAK_PRICE_PLN, is_peak_hour

logger = logging.getLogger(__name__)

# Minuta cyklu, w której mierzymy deficyt wody (zgodnie z ręczną analizą A0/A3 — slot 52
# domyślnie 20 min, opóźnienie AH obserwowane ≈ 19 min).
DEFICIT_AT_MIN = 19.0
CLOCK_HOURS = (4, 8)               # godziny startu „zegarowego” pompy (panel), z tolerancją
CLOCK_TOLERANCE_S = 60
TARGET_LOOKBACK_MIN = 5            # zmiana celu tuż przed startem = „zmiana celu” (np. kąpiel 58°C)
BACKFILL_DAYS = 7                  # zasięg recordera HA

# Klucze katalogu (catalog.py) dla slotów decydujących o polityce AH; dziennik zmian (param_changes)
# działa dopiero od A1 (26.09) — dla wcześniejszych chwil config_at zwraca None (uczciwe „?”, bez zgadywania).
CONFIG_SLOTS: tuple[tuple[str, str], ...] = (("49", "backup_dhw_enabled"), ("50", "backup_heater"),
                                              ("52", "backup_start_delay"))

Series = list[tuple[datetime, str]]


def _points(series: Series) -> tuple[list[datetime], list[float | None]]:
    pts = sorted((t, catalog.to_number(s)) for t, s in series)
    return [t for t, _ in pts], [v for _, v in pts]


def _value_at(times: list[datetime], values: list[float | None], t: datetime) -> float | None:
    """Ostatnia znana wartość w chwili `t` (krok schodkowy — wartość „nosi się” do przodu)."""
    i = bisect.bisect_right(times, t) - 1
    return values[i] if i >= 0 else None


def _delta(times: list[datetime], values: list[float | None], start: datetime, end: datetime) -> float | None:
    """Przyrost licznika w [start, end]. Spadek (reset licznika) → None, nie ujemna wartość."""
    a, b = _value_at(times, values, start), _value_at(times, values, end)
    if a is None or b is None or b < a:
        return None
    return b - a


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def _first_tick(times: list[datetime], values: list[float | None], start: datetime, end: datetime
                ) -> datetime | None:
    """Pierwszy moment, w którym licznik (np. `ah_working_time`) rośnie ponad wartość ze startu cyklu."""
    base = _value_at(times, values, start)
    if base is None:
        return None
    i = bisect.bisect_right(times, start)
    for t, v in zip(times[i:], values[i:]):
        if t > end:
            break
        if v is not None and v > base:
            return t
    return None


def _classify(start: datetime, target_times: list[datetime], target_values: list[float | None],
              tz: ZoneInfo) -> str:
    """„zegar” (start ±60 s od 04:00/08:00 lokalnie), „zmiana celu” (dhw_setpoint zmieniony
    tuż przed startem — kąpiel/PV), reszta „zapotrzebowanie”. Heurystyka pomocnicza — zegar panelu
    jest niepełny (`docs/HEIKO_AH_cost_report.md` §9.2), surowe dane cyklu zostają zawsze widoczne."""
    local = start.astimezone(tz)
    for h in CLOCK_HOURS:
        clock = local.replace(hour=h, minute=0, second=0, microsecond=0)
        if abs((local - clock).total_seconds()) <= CLOCK_TOLERANCE_S:
            return "zegar"
    target = _value_at(target_times, target_values, start)
    before = _value_at(target_times, target_values, start - timedelta(minutes=TARGET_LOOKBACK_MIN))
    if target is not None and before is not None and target != before:
        return "zmiana celu"
    return "zapotrzebowanie"


def _cost(times: list[datetime], values: list[float | None], start: datetime, end: datetime,
          tz: ZoneInfo, is_workday: Callable[[date], bool],
          peak_price: float, offpeak_price: float) -> dict | None:
    """kWh i koszt PLN z licznika energii (faza C = AH) na [start, end], przyrost po przyroście
    przypisany do godziny lokalnej próbki końcowej (jak `summaries._energy`)."""
    if _value_at(times, values, start) is None:
        return None
    i = bisect.bisect_right(times, start)
    total = peak = 0.0
    prev = _value_at(times, values, start)
    for t, v in zip(times[i:], values[i:]):
        if t > end:
            break
        if prev is not None and v is not None and v >= prev:
            inc = v - prev
            local = t.astimezone(tz)
            total += inc
            if is_peak_hour(local.hour, is_workday(local.date())):
                peak += inc
        prev = v
    if total <= 0:
        return {"kwh": 0.0, "pln": 0.0}
    price = peak_price * peak + offpeak_price * (total - peak)
    return {"kwh": round(total, 3), "pln": round(price, 2)}


def extract_cycles(series: dict[str, Series], window_start: datetime, window_end: datetime,
                    tz: ZoneInfo, is_workday: Callable[[date], bool] = lambda d: d.weekday() < 5,
                    peak_price: float = DEFAULT_PEAK_PRICE_PLN,
                    offpeak_price: float = DEFAULT_OFFPEAK_PRICE_PLN) -> list[dict]:
    """Cykle CWU (`working_mode` w DHW_CODES) w [window_start, window_end). `series`: mode, water,
    target (dhw_setpoint), ah (ah_working_time), energy_c, outdoor. Cykl wciąż trwający na prawej
    granicy okna dostaje `complete: False` i nie powinien być zapisany — wołający go pomija, okno
    przesunięte o dzień naprzód złapie go w całości."""
    mode_times, mode_values = _points(series.get("mode", []))
    water_times, water_values = _points(series.get("water", []))
    target_times, target_values = _points(series.get("target", []))
    ah_times, ah_values = _points(series.get("ah", []))
    energy_times, energy_values = _points(series.get("energy_c", []))
    outdoor_times, outdoor_values = _points(series.get("outdoor", []))
    if not mode_times:
        return []

    runs: list[tuple[datetime, datetime, bool]] = []
    cur_start = None
    carry = _value_at(mode_times, mode_values, window_start)
    if carry is not None and int(round(carry)) in DHW_CODES:
        cur_start = window_start
    for t, v in zip(mode_times, mode_values):
        if t < window_start or t >= window_end:
            continue
        in_dhw = v is not None and int(round(v)) in DHW_CODES
        if in_dhw and cur_start is None:
            cur_start = t
        elif not in_dhw and cur_start is not None:
            runs.append((cur_start, t, True))
            cur_start = None
    if cur_start is not None:
        runs.append((cur_start, window_end, False))

    cycles = []
    for start, end, complete in runs:
        duration_min = round(_minutes(start, end), 1)
        target = _value_at(target_times, target_values, start)
        water_start = _value_at(water_times, water_values, start)
        deficit_at = start + timedelta(minutes=DEFICIT_AT_MIN)
        water_min19 = _value_at(water_times, water_values, deficit_at) if deficit_at <= end else None
        deficit = None if target is None or water_min19 is None else round(target - water_min19, 1)
        first_tick = _first_tick(ah_times, ah_values, start, end)
        ah_min = _delta(ah_times, ah_values, start, end)
        cost = _cost(energy_times, energy_values, start, end + timedelta(minutes=2),
                     tz, is_workday, peak_price, offpeak_price)
        cycles.append({
            "start": start.isoformat(), "end": end.isoformat(), "complete": complete,
            "duration_min": duration_min, "type": _classify(start, target_times, target_values, tz),
            "target_c": target, "water_start_c": water_start, "water_min19_c": water_min19,
            "deficit_c": deficit,
            "outdoor_c": _value_at(outdoor_times, outdoor_values, start),
            "ah_tick_min": None if first_tick is None else round(_minutes(start, first_tick), 1),
            "ah_min": None if ah_min is None else round(ah_min, 1),
            "ah_kwh": None if cost is None else cost["kwh"],
            "ah_pln": None if cost is None else cost["pln"],
        })
    return cycles


def config_at(conn, when: datetime) -> dict[str, str | None]:
    """Wartość slotów 49/50/52 w chwili `when`, z dziennika zmian (`param_changes`, działa od 26.09).
    Brak wpisu sprzed `when` → None (uczciwe „nieznane”, bez rekonstrukcji)."""
    out: dict[str, str | None] = {}
    for slot, key in CONFIG_SLOTS:
        best_t, best_v = None, None
        for row in conn.execute("SELECT ts, new FROM param_changes WHERE key = ?", (key,)):
            t = _parse_ts(row["ts"])
            if t is None or t > when:
                continue
            if best_t is None or t > best_t:
                best_t, best_v = t, row["new"]
        out[slot] = best_v
    return out


def _parse_ts(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def group_by_config(cycles: list[dict]) -> list[dict]:
    """Statystyki (n cykli ≥19 min, n z AH, mediana minuty ticka, suma kWh/PLN) per konfiguracja
    49/50/52 w chwili startu. Konfiguracja z jakimkolwiek None trafia do własnej grupy „?”."""
    groups: dict[tuple, list[dict]] = {}
    for c in cycles:
        key = (c.get("cfg_49"), c.get("cfg_50"), c.get("cfg_52"))
        groups.setdefault(key, []).append(c)
    out = []
    for (c49, c50, c52), rows in groups.items():
        long_enough = [r for r in rows if (r["duration_min"] or 0) >= DEFICIT_AT_MIN]
        with_ah = [r for r in long_enough if r["ah_tick_min"] is not None]
        ticks = sorted(r["ah_tick_min"] for r in with_ah)
        median_tick = ticks[len(ticks) // 2] if ticks else None
        kwh = sum(r["ah_kwh"] or 0 for r in rows)
        pln = sum(r["ah_pln"] or 0 for r in rows)
        out.append({
            "cfg_49": c49, "cfg_50": c50, "cfg_52": c52,
            "cycles": len(long_enough), "cycles_with_ah": len(with_ah),
            "median_ah_tick_min": median_tick, "kwh": round(kwh, 3), "pln": round(pln, 2),
            "small_sample": len(long_enough) < 5,
        })
    out.sort(key=lambda g: min((c["start"] for c in cycles if (c.get("cfg_49"), c.get("cfg_50"), c.get("cfg_52"))
                                == (g["cfg_49"], g["cfg_50"], g["cfg_52"])), default=""))
    return out


def store_cycles(conn, settings: dict, day: date, states: list[dict], now: datetime,
                 get_history=ha_client.get_history,
                 is_workday: Callable[[date], bool] | None = None) -> int:
    """Pobiera historię doby (z zapasem `DEFICIT_AT_MIN` + kilku godzin wstecz, żeby złapać cykle
    zaczęte tuż przed północą) i zapisuje kompletne cykle (INSERT OR REPLACE po `start`). Cykle wciąż
    trwające na granicy okna nie są zapisywane — okno przesunięte o dzień je złapie w całości."""
    tel = catalog.resolve_telemetry(states)
    params = catalog.resolve_params(states)
    roles = {"mode": tel.get("working_mode"), "water": tel.get("water_temperature"),
             "target": params.get("dhw_setpoint"), "ah": tel.get("ah_working_time"),
             "energy_c": settings.get("ah_energy_entity") or None,
             "outdoor": tel.get("ambient_air_temperature")}
    entities = [e for e in roles.values() if e]
    if not entities:
        return 0
    tz_name = settings.get("timezone") or "Europe/Warsaw"
    tz = ZoneInfo(tz_name)
    day_start, day_end = day_bounds(day, tz_name)
    fetch_start = day_start - timedelta(hours=3)
    hist = get_history(entities, fetch_start.isoformat(), day_end.isoformat())
    if not hist:
        return 0
    series = {role: hist.get(eid, []) for role, eid in roles.items() if eid}
    cycles = extract_cycles(series, fetch_start, day_end, tz, is_workday or (lambda d: d.weekday() < 5))
    stored = 0
    for c in cycles:
        if not c["complete"]:
            continue
        cfg = config_at(conn, datetime.fromisoformat(c["start"]))
        row = {**c, "cfg_49": cfg["49"], "cfg_50": cfg["50"], "cfg_52": cfg["52"]}
        conn.execute("INSERT OR REPLACE INTO dhw_cycles (start, end, data) VALUES (?, ?, ?)",
                     (c["start"], c["end"], json.dumps(row)))
        stored += 1
    if stored:
        conn.commit()
    return stored


def missing_days(conn, now: datetime, back_days: int = BACKFILL_DAYS) -> list[date]:
    """Ostatnie pełne doby (bez dzisiejszej) bez zapisanych cykli — od najstarszej. Osobna od
    `summaries.missing_days` (tabela własna), więc backfill nie zależy od streszczeń dobowych."""
    have = {r["d"] for r in conn.execute(
        "SELECT DISTINCT substr(start, 1, 10) AS d FROM dhw_cycles")}
    today = now.date()
    days = [today - timedelta(days=i) for i in range(1, back_days + 1)]
    return [d for d in sorted(days) if d.isoformat() not in have]


def load_cycles(conn, days: int = 60) -> list[dict]:
    """Cykle zapisanych ostatnich `days` dni, najnowsze pierwsze."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT data FROM dhw_cycles WHERE start >= ? ORDER BY start DESC", (since,)).fetchall()
    return [json.loads(r["data"]) for r in rows]
