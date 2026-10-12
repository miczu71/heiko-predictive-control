"""Model grzania CWU i koszt grzałki AH (M2, plan `docs/ROADMAP_heiko_dhw_model.md`) — funkcje czyste,
**tylko odczyt**. Z odcinków pracy samej sprężarki (`dhw_cycles`: `hp_*`) uczy tempo grzania (°C/min)
i moc (kW) jako funkcję temperatury zewnętrznej i wody. Dla cyklu z AH liczy kontrfaktykę „to samo
ogrzanie samą sprężarką”: ile kWh i zł AH dołożyła i ile minut zaoszczędziła. W zł/kWh ciepła AH
(COP 1) z pompą nie wygra — model wycenia skrócenie cyklu. Za mało danych albo warunki poza zakresem
uczenia → brak wyniku z powodem, bez zgadywania."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

MIN_FIT_SEGMENTS = 8        # 3 współczynniki na model; mniej odcinków = przeuczenie
RANGE_MARGIN_C = 3.0        # tolerancja ekstrapolacji poza zakres temperatur z uczenia
MIN_RATE_C_MIN = 0.05       # poniżej tego model nie przewiduje realnego grzania sprężarką
MIN_COMPRESSOR_KW = 0.3     # niższa średnia moc na odcinku = sprężarka nie pracowała
MIN_SAVED_MIN = 0.5         # krótsza oszczędność czasu — cena za minutę byłaby szumem

_HP_KEYS = ("hp_rate_c_min", "hp_kw", "hp_water_mid_c", "outdoor_c")
_AH_KEYS = ("ah_rate_c_min", "ah_total_kw", "ah_seg_min", "ah_water_mid_c", "outdoor_c")


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Układ liniowy metodą Gaussa z wyborem elementu głównego; osobliwy → None."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    scale = max(abs(x) for row in a for x in row) or 1.0
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-9 * scale:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


def _ols(xs: list[tuple[float, float]], ys: list[float]) -> dict | None:
    """y = c0 + c1·x1 + c2·x2 (najmniejsze kwadraty) + R²."""
    rows = [[1.0, a, b] for a, b in xs]
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    xty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(3)]
    coef = _solve(xtx, xty)
    if coef is None:
        return None
    mean = sum(ys) / len(ys)
    sst = sum((y - mean) ** 2 for y in ys)
    ssr = sum((y - sum(c * v for c, v in zip(coef, r))) ** 2 for r, y in zip(rows, ys))
    return {"coef": [round(c, 6) for c in coef], "r2": round(1 - ssr / sst, 3) if sst > 0 else 0.0}


def fit_model(cycles: list[dict]) -> dict:
    """Model z odcinków sprężarki wszystkich cykli (nie tylko z AH). `ok: False` + `reason`, gdy brak
    wystarczających danych albo temperatury nie różnicują się na tyle, by rozróżnić współczynniki."""
    pts = [c for c in cycles
           if all(c.get(k) is not None for k in _HP_KEYS)
           and c["hp_rate_c_min"] > MIN_RATE_C_MIN and c["hp_kw"] >= MIN_COMPRESSOR_KW]
    if len(pts) < MIN_FIT_SEGMENTS:
        return {"ok": False, "n": len(pts), "reason": f"za mało odcinków sprężarki ({len(pts)} < {MIN_FIT_SEGMENTS})"}
    xs = [(c["outdoor_c"], c["hp_water_mid_c"]) for c in pts]
    rate = _ols(xs, [c["hp_rate_c_min"] for c in pts])
    kw = _ols(xs, [c["hp_kw"] for c in pts])
    if rate is None or kw is None:
        return {"ok": False, "n": len(pts), "reason": "temperatury w odcinkach prawie się nie różnią"}
    return {"ok": True, "n": len(pts), "rate": rate, "kw": kw,
            "ranges": {"outdoor": [min(x[0] for x in xs), max(x[0] for x in xs)],
                       "water": [min(x[1] for x in xs), max(x[1] for x in xs)]}}


def predict(model: dict, outdoor_c: float, water_c: float) -> tuple[float, float]:
    """(tempo °C/min, moc kW) samej sprężarki w danych warunkach."""
    def f(fit: dict) -> float:
        c = fit["coef"]
        return c[0] + c[1] * outdoor_c + c[2] * water_c
    return f(model["rate"]), f(model["kw"])


def _in_range(model: dict, outdoor_c: float, water_c: float) -> bool:
    lo_o, hi_o = model["ranges"]["outdoor"]
    lo_w, hi_w = model["ranges"]["water"]
    return (lo_o - RANGE_MARGIN_C <= outdoor_c <= hi_o + RANGE_MARGIN_C
            and lo_w - RANGE_MARGIN_C <= water_c <= hi_w + RANGE_MARGIN_C)


def ah_effect(cycle: dict, model: dict, price_fn: Callable[[datetime], float], tz: ZoneInfo) -> dict:
    """Kontrfaktyka odcinka z AH: to samo ogrzanie (°C) samą sprężarką. `extra_kwh`/`extra_pln` = ile
    AH dołożyła, `saved_min` = o ile krócej trwało grzanie. Cena z godziny wejścia AH (`price_fn(lokalny czas)`)."""
    if not model.get("ok"):
        return {"ok": False, "reason": model.get("reason", "brak modelu")}
    if cycle.get("ah_tick_min") is None:
        return {"ok": False, "reason": "brak odcinka z AH"}
    if any(cycle.get(k) is None for k in _AH_KEYS):
        return {"ok": False, "reason": "odcinek z AH niezmierzony (zapis sprzed 0.13.0 albo krótszy niż 3 min)"}
    delta_c = cycle["ah_rate_c_min"] * cycle["ah_seg_min"]
    if delta_c <= 0:
        return {"ok": False, "reason": "brak przyrostu temperatury na odcinku z AH"}
    if not _in_range(model, cycle["outdoor_c"], cycle["ah_water_mid_c"]):
        return {"ok": False, "reason": "warunki poza zakresem uczenia modelu"}
    rate, kw = predict(model, cycle["outdoor_c"], cycle["ah_water_mid_c"])
    if rate < MIN_RATE_C_MIN or kw <= 0:
        return {"ok": False, "reason": "model nie przewiduje grzania samą sprężarką"}
    minutes_without = delta_c / rate
    extra_kwh = cycle["ah_total_kw"] * cycle["ah_seg_min"] / 60.0 - kw * minutes_without / 60.0
    ah_start = datetime.fromisoformat(cycle["start"]) + timedelta(minutes=cycle["ah_tick_min"])
    price = price_fn(ah_start.astimezone(tz))
    return {"ok": True, "extra_kwh": round(extra_kwh, 3), "extra_pln": round(extra_kwh * price, 2),
            "saved_min": round(minutes_without - cycle["ah_seg_min"], 1), "price_pln_kwh": price}


def summarize(cycles: list[dict], model: dict, price_fn: Callable[[datetime], float], tz: ZoneInfo) -> dict:
    """Suma po cyklach z AH: dodatkowe kWh/zł, zaoszczędzone minuty i cena za minutę. Cykle bez wyniku
    zostają na liście z powodem (`skipped`), żeby suma nie udawała pełnej próby."""
    rows, skipped = [], []
    for c in cycles:
        if c.get("ah_tick_min") is None:
            continue
        eff = ah_effect(c, model, price_fn, tz)
        (rows if eff["ok"] else skipped).append({"start": c["start"], **eff})
    pln = sum(r["extra_pln"] for r in rows)
    saved = sum(r["saved_min"] for r in rows)
    return {"cycles_with_ah": len(rows) + len(skipped), "evaluated": len(rows), "rows": rows,
            "skipped": [{"start": s["start"], "reason": s["reason"]} for s in skipped],
            "extra_kwh": round(sum(r["extra_kwh"] for r in rows), 3), "extra_pln": round(pln, 2),
            "saved_min": round(saved, 1),
            "pln_per_saved_min": round(pln / saved, 3) if saved >= MIN_SAVED_MIN else None}
