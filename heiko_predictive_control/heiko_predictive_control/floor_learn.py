"""Uczenie modelu podłogówki (orkiestracja I/O nad czystym `floor_model`):

* `bootstrap` — jednorazowe dopasowanie na statystykach długoterminowych (LTS)
  zeszłej zimy, żeby plan miał sensowne parametry od pierwszego dnia sezonu;
* `refit_from_cycles` — dobowe dopasowanie na własnych cyklach add-onu
  (faza cienia: pompa pracuje na krzywej natywnej, my tylko obserwujemy).

Nowy model zastępuje poprzedni tylko gdy `floor_model.accept_fit` go przepuści
(prognoza 6 h z błędem ≤ 0,5°C i lepsza od „nic się nie zmieni”)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db as dbm
from . import floor_model as fm
from . import floor_plan as fp
from . import ha_client

logger = logging.getLogger(__name__)

HEATING_MONTHS = (10, 11, 12, 1, 2, 3, 4)
HEATING_MAX_OUTDOOR_C = 14.0
MIN_RUN_HOURS = 12
_HOUR_MS = 3_600_000
_CURVE_AMBIENT = [f"number.heiko_heat_pump_curve_ambient_temp_{i}" for i in range(1, 6)]
_CURVE_WATER = [f"number.heiko_heat_pump_curve_water_temp_{i}" for i in range(1, 6)]


def season_window(now: datetime) -> tuple[datetime, datetime]:
    """Ostatni PEŁNY sezon grzewczy: 15.10 – 15.04 (UTC)."""
    end_year = now.year if (now.month, now.day) >= (4, 15) else now.year - 1
    return (datetime(end_year - 1, 10, 15, tzinfo=timezone.utc),
            datetime(end_year, 4, 15, tzinfo=timezone.utc))


def _entities(raw, default: list[str]) -> list[str]:
    items = [e.strip() for e in str(raw or "").split(",") if e.strip()]
    return items or default


def hourly_rows(stats: dict[str, list[dict]], zones: list[str], outdoor: str, energy: str
                ) -> list[tuple[int, float, float, float]]:
    """(start_ms, średnia stref, temp. zewn., energia kWh) dla godzin, w których
    są WSZYSTKIE serie — zmienny skład średniej robiłby sztuczne skoki."""
    def by_start(entity, field):
        return {r["start"]: r[field] for r in stats.get(entity, []) if r.get(field) is not None}
    z = [by_start(e, "mean") for e in zones]
    o, en = by_start(outdoor, "mean"), by_start(energy, "change")
    rows = []
    for start in sorted(o):
        if start in en and en[start] >= 0 and all(start in s for s in z):
            rows.append((start, sum(s[start] for s in z) / len(z), o[start], en[start]))
    return rows


def heating_runs(rows: list[tuple[int, float, float, float]]) -> list[list[tuple[int, float, float, float]]]:
    """Ciągłe (godzina po godzinie) odcinki sezonu grzewczego."""
    runs, cur = [], []
    for row in rows:
        month = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).month
        ok = month in HEATING_MONTHS and row[2] < HEATING_MAX_OUTDOOR_C
        if ok and cur and row[0] - cur[-1][0] != _HOUR_MS:
            runs.append(cur)
            cur = []
        if ok:
            cur.append(row)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= MIN_RUN_HOURS]


def bootstrap(conn, settings: dict, now: datetime,
              get_statistics=ha_client.get_statistics,
              get_numeric=ha_client.get_numeric_state) -> dict:
    """Dopasowanie na LTS zeszłej zimy. Zapisuje wynik (`floor_bootstrap`) i, gdy
    dopasowanie przejdzie bramkę jakości, także model (`floor_model_state`)."""
    info: dict = {"at": now.isoformat(timespec="minutes"), "ok": False}
    zones = _entities(settings.get("day_zone_temp_entities"), [])
    outdoor = settings.get("heiko_bootstrap_outdoor_entity") or settings.get("outdoor_temp_entity", "")
    energy = settings.get("pump_energy_entity", "")
    start, end = season_window(now)
    stats = get_statistics(zones + [outdoor, energy], start.isoformat(), end.isoformat())
    if stats is None:
        return _finish_bootstrap(conn, info, "brak dostępu do statystyk HA (spróbuję po restarcie)",
                                 retry=True)
    amb = [get_numeric(e) for e in _entities(settings.get("heiko_curve_ambient_entities"), _CURVE_AMBIENT)]
    wat = [get_numeric(e) for e in _entities(settings.get("heiko_curve_water_entities"), _CURVE_WATER)]
    runs = heating_runs(hourly_rows(stats, zones, outdoor, energy))
    info["hours"] = sum(len(r) for r in runs)
    info["runs"] = len(runs)
    if not runs:
        return _finish_bootstrap(conn, info, "brak ciągłych godzin sezonu grzewczego w statystykach")

    segments, q_all, tr_all, tw_all = [], [], [], []
    for run in runs:
        tr = [r[1] for r in run]
        to = [r[2] for r in run]
        water = [fp.curve_setpoint(amb, wat, t) for t in to]
        if any(w is None for w in water):
            return _finish_bootstrap(conn, info, "brak punktów krzywej grzewczej")
        q = [fm.measured_heat_kw(r[3], 1.0, r[2], w) for r, w in zip(run, water)]
        segments.append(fm.Segment(tr=tr, t_out=to, q_kw=q))
        q_all += q
        tr_all += tr
        tw_all += water
    fit = fm.fit_best(segments, dt_h=1.0)
    if fit is not None:
        info.update(rmse_c=round(fit.rmse_c, 3) if fit.rmse_c is not None else None,
                    persist_c=round(fit.persist_c, 3) if fit.persist_c is not None else None,
                    tau_h=fit.tau_h, c=fit.c, balance_r=fit.balance_r)
    if not fm.accept_fit(fit):
        return _finish_bootstrap(conn, info, "dopasowanie nie przeszło bramki jakości — zostają wartości domyślne")
    current = dbm.get_setting(conn, "floor_model_state")
    gain = fm.fit_energy_gain(q_all, tr_all, tw_all)
    fit.e, fit.e_samples = (gain if gain else (fm.DEFAULT_E, 0))
    fm.stamp(fit, fm.SOURCE_BOOTSTRAP, now)
    if current and current.get("source") == fm.SOURCE_SHADOW:
        return _finish_bootstrap(conn, info, "model z fazy cienia już istnieje — bootstrap pominięty")
    dbm.set_setting(conn, "floor_model_state", fit.as_dict())
    info["ok"] = True
    return _finish_bootstrap(conn, info, "ok")


def _finish_bootstrap(conn, info: dict, reason: str, retry: bool = False) -> dict:
    info["reason"] = reason
    if not retry:                       # brak WebSocketu = spróbuj przy następnym starcie
        dbm.set_setting(conn, "floor_bootstrap", info)
    logger.info("Bootstrap modelu podłogówki: %s", info)
    return info


def cycle_segments(rows, gap_min: tuple[float, float] = (10.0, 20.0)) -> list[fm.Segment]:
    """Cykle add-onu (co 15 min) -> segmenty. Moc `heat_kw` wiersza j dotyczy
    przedziału (t[j−1], t[j]], więc w segmencie przesuwa się o jeden krok."""
    segments: list[fm.Segment] = []
    cur: list = []

    def flush():
        if len(cur) >= 3:
            q = [r["heat_kw"] for r in cur[1:]]
            segments.append(fm.Segment(tr=[r["indoor_temp_c"] for r in cur],
                                       t_out=[r["outdoor_temp_c"] for r in cur],
                                       q_kw=q + [q[-1]]))
        cur.clear()

    prev_ts = None
    for r in rows:
        complete = (r["indoor_temp_c"] is not None and r["outdoor_temp_c"] is not None
                    and (r["heat_kw"] is not None or not cur))
        ts = datetime.fromisoformat(r["ts"])
        if not complete:
            flush()
            prev_ts = None
            continue
        if prev_ts is not None and not (gap_min[0] <= (ts - prev_ts).total_seconds() / 60 <= gap_min[1]):
            flush()
        cur.append(r)
        prev_ts = ts
    flush()
    return segments


def refit_from_cycles(conn, settings: dict, now: datetime, days: int = 14,
                      min_points: int = 150) -> dict:
    """Dobowe dopasowanie modelu na własnych cyklach (faza cienia)."""
    info: dict = {"at": now.isoformat(timespec="minutes"), "ok": False}
    cutoff = (now - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT ts, indoor_temp_c, outdoor_temp_c, heat_kw, water_temp_c, base_curve_c "
        "FROM cycles WHERE loop = 'heiko' AND ts >= ? ORDER BY id", (cutoff,)).fetchall()
    segments = cycle_segments(rows)
    info["rows"] = len(rows)
    fit = fm.fit_best(segments, dt_h=0.25, min_points=min_points)
    if fit is None:
        info["reason"] = "za mało danych z grzaniem do dopasowania"
        return _finish_refit(conn, info)
    info.update(rmse_c=_round(fit.rmse_c), persist_c=_round(fit.persist_c), tau_h=fit.tau_h,
                c=fit.c, balance_r=fit.balance_r)
    if not fm.accept_fit(fit):
        info["reason"] = "dopasowanie nie przeszło bramki jakości — model bez zmian"
        return _finish_refit(conn, info)
    current = load_model(conn)
    q, tr, tw = [], [], []
    for r in rows:
        if r["heat_kw"] and r["heat_kw"] > 0.05 and r["indoor_temp_c"] is not None:
            water = r["water_temp_c"] if r["water_temp_c"] is not None else r["base_curve_c"]
            if water is not None:
                q.append(r["heat_kw"])
                tr.append(r["indoor_temp_c"])
                tw.append(water)
    gain = fm.fit_energy_gain(q, tr, tw)
    fit.e, fit.e_samples = gain if gain else (current.e, current.e_samples)
    fm.stamp(fit, fm.SOURCE_SHADOW, now)
    dbm.set_setting(conn, "floor_model_state", fit.as_dict())
    info["ok"], info["reason"] = True, "ok"
    return _finish_refit(conn, info)


def load_model(conn) -> fm.FloorModel:
    return fm.FloorModel.from_dict(dbm.get_setting(conn, "floor_model_state"))


def _round(value):
    return None if value is None else round(value, 3)


def _finish_refit(conn, info: dict) -> dict:
    dbm.set_setting(conn, "floor_refit", info)
    logger.info("Dobowe dopasowanie modelu podłogówki: %s", info)
    return info
