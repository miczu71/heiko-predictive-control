"""Katalog miar testów (E3): doby wariantów, kontekst, miary `arm`/`test` i `evaluate`.

Czysty odczyt bazy (poza `inertia_refit` — ma skutek uboczny jak legacy `experiments._result`). Bez HA i sieci:
kWh doby wstrzykiwane przez `Ctx.day_kwh`. Stałe i legacy-funkcje importowane z `experiments` / `floor_learn`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
from typing import Callable
from zoneinfo import ZoneInfo

from .. import db as dbm
from .. import experiments as ex
from .. import floor_learn, heating_runs, summaries
from .verdict import decide

_WINDOW_RE = re.compile(r"(\d{2}):(\d{2})→(\d{2}):(\d{2})")


@dataclass(frozen=True)
class ArmDay:
    """Doba wariantu: lokalne, naiwne czasy strefy `settings["timezone"]`, przedział [start, end)."""
    arm: str
    start: datetime
    end: datetime


@dataclass
class Ctx:
    conn: object
    settings: dict
    now: datetime | None
    test: object = None
    day_kwh: Callable | None = None

    def __post_init__(self):
        self.default_day_kwh = self.day_kwh is None
        if self.day_kwh is None:
            self.day_kwh = lambda *a: ex._day_kwh(*a)        # późne wiązanie (podmiana w testach)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.get("timezone") or "Europe/Warsaw")


@dataclass(frozen=True)
class Measure:
    scope: str                                # "arm" | "test"
    fn: Callable


def _window(day: date, window: str) -> tuple[datetime, datetime]:
    """Okno `HH:MM→HH:MM` od dnia `day`; koniec nie późniejszy niż początek = następny dzień (13:00→13:00 = doba)."""
    m = _WINDOW_RE.fullmatch(window)
    if not m:
        raise ValueError(f"okno {window!r}")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    start = datetime.combine(day, time(h1, m1))
    end = datetime.combine(day, time(h2, m2))
    return start, end if end > start else end + timedelta(days=1)


def arm_days_from_steps(steps: list[dict], label: Callable[[dict], str]) -> list[ArmDay]:
    """Doba = od kroku do następnego (ostatni krok to powrót do bazy, nie doba)."""
    return [ArmDay(label(cur), datetime.fromisoformat(cur["at"]), datetime.fromisoformat(nxt["at"]))
            for cur, nxt in zip(steps, steps[1:])]


def arm_days_from_plan(arms_by_day: dict[str, str], window: str = "00:00→00:00") -> list[ArmDay]:
    """Doba wariantu = okno od `HH:MM` dnia do `HH:MM` następnego dnia (weekend nie skraca doby)."""
    return [ArmDay(arm, *_window(date.fromisoformat(iso), window)) for iso, arm in arms_by_day.items()]


def _avg_outdoor(conn, start: datetime, end: datetime) -> float | None:
    return conn.execute("SELECT AVG(outdoor_temp_c) FROM cycles WHERE loop = 'heiko' AND ts >= ? AND ts < ?",
                        (start.isoformat(), end.isoformat())).fetchone()[0]


def outdoor_c(ctx: Ctx, days: list[ArmDay]) -> float | None:
    """Kowariata: średnia z dobowych średnich temp. zewn. (nie miara)."""
    temps = [t for t in (_avg_outdoor(ctx.conn, d.start, d.end) for d in days) if t is not None]
    return round(mean(temps), 1) if temps else None


def _to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


# --- miary `arm` ---

def kwh_day(ctx: Ctx, days: list[ArmDay]) -> dict:
    if ctx.now is None and ctx.default_day_kwh:           # domyślny hook dotyka HA i wymaga `now` — doby pomijamy
        return {"value": None, "n": 0}
    values = [v for v in (ctx.day_kwh(ctx.conn, ctx.settings, d.start.date(), ctx.now) for d in days) if v is not None]
    return {"value": round(mean(values), 2) if values else None, "n": len(values)}


def zone_spread(ctx: Ctx, days: list[ArmDay]) -> dict:
    """Pula próbek wariantu (jak legacy `_p0_result`): rozrzut stref w standby."""
    mode: dict = {}
    zones: dict = {}
    n_zones = 0
    for d in days:
        samples = summaries.zone_samples(ctx.conn, ctx.settings, int(d.start.replace(tzinfo=ctx.tz).timestamp()),
                                         int(d.end.replace(tzinfo=ctx.tz).timestamp()))
        if samples:
            mode.update(samples[0])
            zones.update(samples[1])
            n_zones = samples[2]
    spread = summaries.standby_spread(mode, zones, n_zones) if n_zones else None
    return {"value": spread and spread["spread_c"], "spread_c": spread and spread["spread_c"],
            "zones_avg_c": spread and spread["zones_avg_c"], "samples": spread["samples"] if spread else 0,
            "n": len(days)}


def _has_energy_row(conn, day: date) -> bool:
    return conn.execute("SELECT 1 FROM daily_summary WHERE day = ? AND topic = 'energy'",
                        (day.isoformat(),)).fetchone() is not None


def floor_cost(ctx: Ctx, days: list[ArmDay], window: str, per: str | None = None) -> dict:
    per_day = []
    for d in days:
        start, end = _window(d.start.date(), window)
        runs = heating_runs.load_range(ctx.conn, _to_utc(start, ctx.tz), _to_utc(end, ctx.tz))
        if any(r.get("cost_pln") is None for r in runs):              # koszt nieznany to nie zero
            continue
        if not runs and not _has_energy_row(ctx.conn, d.start.date()):
            continue
        cost = sum(r["cost_pln"] for r in runs)
        if per == "hdd":
            out = _avg_outdoor(ctx.conn, start, end)
            hdd = None if out is None else max(0.0, floor_learn.HEATING_MAX_OUTDOOR_C - out)
            if hdd is None or hdd < 1.0:
                continue
            cost /= hdd
        per_day.append({"day": d.start.date().isoformat(), "value": round(cost, 3), "_raw": cost})
    value = round(mean(p["_raw"] for p in per_day), 3) if per_day else None
    return {"value": value, "n": len(per_day), "per_day": [{"day": p["day"], "value": p["value"]} for p in per_day]}


def min_room_at(ctx: Ctx, days: list[ArmDay], at: str) -> dict:
    per_day = []
    for d in days:
        t = datetime.combine(d.start.date(), time(*(int(x) for x in at.split(":"))))
        if t < d.start:
            t += timedelta(days=1)
        low = ctx.conn.execute("SELECT MIN(min_room_c) FROM cycles WHERE loop = 'heiko' AND ts >= ? AND ts <= ?",
                               ((t - timedelta(minutes=30)).isoformat(), (t + timedelta(minutes=30)).isoformat())
                               ).fetchone()[0]
        if low is not None:
            per_day.append({"day": d.start.date().isoformat(), "value": round(low, 1)})
    value = round(min(low["value"] for low in per_day), 1) if per_day else None   # najgorszy dzień
    return {"value": value, "n": len(per_day), "per_day": per_day}


def safety_trips(ctx: Ctx, days: list[ArmDay]) -> dict:
    trips = set((dbm.get_setting(ctx.conn, "g13_peak_state") or {}).get("trips") or [])
    count = sum(1 for d in days if d.start.date().isoformat() in trips)
    return {"value": count, "n": len(days)}


# --- miary `test` ---

def excitation(ctx: Ctx) -> dict:
    rows = ctx.conn.execute("SELECT reduced_active, water_setpoint_c, base_curve_c, curve_shift_c FROM cycles "
                            "WHERE loop = 'heiko' AND ts >= ? ORDER BY id", (ctx.test["started"],)).fetchall()
    result = floor_learn.excitation(rows)
    return {"value": result["sufficient"], "detail": {k: v for k, v in result.items() if k != "sufficient"}}


def inertia_refit(ctx: Ctx) -> dict:
    """UWAGA: skutek uboczny (zapis `floor_model_state`), jak legacy `_result`. Nie wołać w podglądzie."""
    refit = floor_learn.refit_from_cycles(ctx.conn, ctx.settings, ctx.now, excitation_since=ctx.test["started"])
    return {k: refit.get(k) for k in ("ok", "reason", "rmse_c", "tau_h", "identified")}


def writes_confirmed(ctx: Ctx) -> dict:
    writes = ctx.conn.execute("SELECT ts, confirmed_at FROM pump_writes WHERE test_id = ?", (ctx.test["id"],)).fetchall()
    delays = [(datetime.fromisoformat(w["confirmed_at"]) - datetime.fromisoformat(w["ts"])).total_seconds() / 60
              for w in writes if w["confirmed_at"] not in (None, ex.NO_CONFIRMATION, ex.SUPERSEDED)]
    return {"writes": len(writes), "confirmed": len(delays),
            "max_confirm_min": round(max(delays), 1) if delays else None, "value": len(delays)}


CATALOG: dict[str, Measure] = {
    "kwh_day": Measure("arm", kwh_day),
    "zone_spread": Measure("arm", zone_spread),
    "floor_cost": Measure("arm", floor_cost),
    "min_room_at": Measure("arm", min_room_at),
    "safety_trips": Measure("arm", safety_trips),
    "excitation": Measure("test", excitation),
    "inertia_refit": Measure("test", inertia_refit),
    "writes_confirmed": Measure("test", writes_confirmed),
}


def _spec(entry) -> tuple[str, dict]:
    if isinstance(entry, str):
        return entry, {}
    return entry["name"], {k: v for k, v in entry.items() if k != "name"}


def evaluate(defn: dict, ctx: Ctx, arm_days: list[ArmDay]) -> dict:
    """Wyniki miar per wariant (`arm`) i dla testu (`test`) + werdykt (None, gdy miara główna jest `test`)."""
    specs = [_spec(e) for e in defn.get("measures") or []]
    for name, _ in specs:
        if name not in CATALOG:
            raise ValueError(f"nieznana miara {name!r}")
    primary = (defn.get("verdict") or {}).get("primary")
    by_arm: dict[str, list[ArmDay]] = {}
    for d in arm_days:
        by_arm.setdefault(d.arm, []).append(d)
    arms = {}
    for arm, days in by_arm.items():
        results = {name: CATALOG[name].fn(ctx, days, **params) for name, params in specs
                   if CATALOG[name].scope == "arm"}
        main = results.get(primary) or {}
        arms[arm] = {"days": len(days), "outdoor_c": outdoor_c(ctx, days), "measures": results,
                     "value": main.get("value"), "n": main.get("n", 0)}
    test = {name: CATALOG[name].fn(ctx, **params) for name, params in specs if CATALOG[name].scope == "test"}
    primary_arm = primary in CATALOG and CATALOG[primary].scope == "arm"
    verdict = decide(defn["verdict"], arms) if defn.get("verdict") and primary_arm else None
    return {"arms": arms, "test": test, "verdict": verdict}
