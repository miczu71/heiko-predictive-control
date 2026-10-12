"""Szeregi czasowe do wykresów strony Statystyki — funkcje czyste na tabeli `cycles`.

Zakres 24 h oddaje każdy cykl, dłuższe (7 i 30 dni) są uśredniane do godzin, żeby wykres miał najwyżej ~720 punktów.
Wynik jest kolumnowy (`t` + serie o tej samej długości), czyli dokładnie taki, jakiego chce uPlot. Brak pomiaru to
`None`, nie 0. Podsumowanie liczy się z surowych cykli, nie ze średnich godzinowych."""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

RANGES_H = (24, 168, 720)
HOUR_S = 3600
AC_ON_MIN_W = 50.0                  # poniżej tej mocy klimatyzacja „nie pracuje”
MODEL_OK_ERR_C = 0.5                # błąd modelu w granicach ±0,5 °C uznajemy za trafny

# seria -> (kolumna cycles, sposób łączenia w godzinę)
HEIKO = {
    "indoor_c": ("indoor_temp_c", "mean"), "min_room_c": ("min_room_c", "min"), "outdoor_c": ("outdoor_temp_c", "mean"),
    "model_err_c": ("model_err_c", "mean"), "base_curve_c": ("base_curve_c", "mean"), "water_c": ("water_temp_c", "mean"),
    "plan_komfort_c": ("setpoint_komfort", "mean"), "plan_ekonomia_c": ("setpoint_ekonomia", "mean"),
}
ATTIC = {
    "indoor_c": ("indoor_temp_c", "mean"), "setpoint_c": ("ac_cmd_setpoint", "mean"), "power_w": ("ac_power_w", "mean"),
    "window_open": ("window_open", "max"), "energy_kwh": ("attic_energy_kwh", "sum"), "cost_pln": ("attic_cost_pln", "sum"),
}
_REDUCE = {"mean": lambda v: sum(v) / len(v), "min": min, "max": max, "sum": sum}


def _reduce(values: list, how: str):
    vals = [v for v in values if v is not None]
    return _REDUCE[how](vals) if vals else None


def _derived(row) -> dict:
    """Pola liczone z pojedynczego cyklu: energia wg taryfy i koszt (energia × cena z tego cyklu)."""
    kwh, price = row["energy_kwh"], row["price_pln_kwh"]
    peak = bool(row["tariff_peak"])
    return {"energy_offpeak_kwh": None if kwh is None or peak else kwh,
            "energy_peak_kwh": kwh if kwh is not None and peak else None,
            "cost_pln": None if kwh is None or price is None else kwh * price}


def _heiko_summary(rows: list[dict], step_h: float, meta: dict) -> dict:
    kwh = sum(r["energy_kwh"] for r in rows if r["energy_kwh"] is not None)
    peak = sum(r["energy_kwh"] for r in rows if r["energy_kwh"] is not None and r["tariff_peak"])
    errs = [abs(r["model_err_c"]) for r in rows if r["model_err_c"] is not None]
    rooms = [r["min_room_c"] for r in rows if r["min_room_c"] is not None]
    indoor = [r["indoor_temp_c"] for r in rows if r["indoor_temp_c"] is not None]
    room_min = meta.get("room_min_c")
    return {
        "avg_indoor_c": round(sum(indoor) / len(indoor), 2) if indoor else None,
        "coldest_room_c": round(min(rooms), 2) if rooms else None,
        "hours_below_min": None if room_min is None or not rooms else round(sum(1 for v in rooms if v < room_min) * step_h, 2),
        "energy_kwh": round(kwh, 2),
        "cost_pln": round(sum(r["energy_kwh"] * r["price_pln_kwh"] for r in rows
                              if r["energy_kwh"] is not None and r["price_pln_kwh"] is not None), 2),
        "peak_share": round(peak / kwh, 3) if kwh > 0 else None,
        "model_mean_abs_err_c": round(sum(errs) / len(errs), 3) if errs else None,
        "model_within_half_deg_pct": round(100 * sum(1 for e in errs if e <= MODEL_OK_ERR_C) / len(errs)) if errs else None,
    }


def _attic_summary(rows: list[dict], step_h: float) -> dict:
    indoor = [r["indoor_temp_c"] for r in rows if r["indoor_temp_c"] is not None]
    return {
        "avg_indoor_c": round(sum(indoor) / len(indoor), 2) if indoor else None,
        "energy_kwh": round(sum(r["attic_energy_kwh"] or 0 for r in rows), 3),
        "cost_pln": round(sum(r["attic_cost_pln"] or 0 for r in rows), 2),
        "ac_on_h": round(sum(1 for r in rows if (r["ac_power_w"] or 0) > AC_ON_MIN_W) * step_h, 2),
        "window_open_h": round(sum(1 for r in rows if r["window_open"]) * step_h, 2),
        "writes": sum(1 for r in rows if r["wrote"]),
    }


def build(conn, loop: str, hours: int, now: datetime, meta: dict | None = None) -> dict:
    if loop not in ("heiko", "attic"):
        raise ValueError(f"nieznana pętla: {loop}")
    if hours not in RANGES_H:
        raise ValueError(f"zakres w godzinach: {RANGES_H}")
    meta = meta or {}
    since = (now - timedelta(hours=hours)).isoformat()
    rows = []
    for r in conn.execute("SELECT * FROM cycles WHERE loop = ? AND ts >= ? ORDER BY ts", (loop, since)):
        d = dict(r)
        d["t"] = datetime.fromisoformat(d["ts"]).timestamp()
        rows.append(d)
    spec = HEIKO if loop == "heiko" else ATTIC
    bucket_s = 0 if hours <= 24 else HOUR_S
    groups: dict[float, list[dict]] = {}
    for d in rows:
        groups.setdefault(int(d["t"] // bucket_s * bucket_s) if bucket_s else d["t"], []).append(d)
    names = list(spec) + (["energy_offpeak_kwh", "energy_peak_kwh", "cost_pln"] if loop == "heiko" else [])
    series: dict[str, list] = {n: [] for n in names}
    for members in groups.values():
        for name, (col, how) in spec.items():
            series[name].append(_reduce([m[col] for m in members], how))
        if loop == "heiko":
            parts = [_derived(m) for m in members]
            for name in ("energy_offpeak_kwh", "energy_peak_kwh", "cost_pln"):
                series[name].append(_reduce([p[name] for p in parts], "sum"))
    diffs = [b["t"] - a["t"] for a, b in zip(rows, rows[1:]) if 0 < b["t"] - a["t"] <= HOUR_S]
    step_min = round(median(diffs) / 60, 1) if diffs else None
    step_h = (step_min or 0) / 60
    summary = None
    if rows:
        summary = _heiko_summary(rows, step_h, meta) if loop == "heiko" else _attic_summary(rows, step_h)
    return {"loop": loop, "hours": hours, "bucket_s": bucket_s, "step_min": step_min, "meta": meta,
            "t": list(groups), "series": series, "summary": summary}
