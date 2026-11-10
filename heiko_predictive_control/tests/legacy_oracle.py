"""Wyrocznia złotych testów E2/E3/E4: kopia 1:1 starego kodu `experiments` sprzed przełączenia silnika na definicje.

Wyłącznie do testów (nie importować z kodu add-onu). Stałe i `_fmt`/`_day_kwh` z `experiments` (późne wiązanie —
testy podmieniają `ex._day_kwh`)."""
import json
from datetime import datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from heiko_predictive_control import experiments as ex
from heiko_predictive_control import floor_learn, summaries


def _p0_schedule(baseline: tuple, now: datetime) -> list[dict]:
    """A B A B A B w dobach od najbliższej północy; krok 7 (północ po ostatniej dobie) wraca do wartości wyjściowej."""
    if baseline not in ex.P0_PAIRS:
        return []
    other = next(p for p in ex.P0_PAIRS if p != baseline)
    first = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return [{"at": (first + timedelta(days=i)).isoformat(timespec="minutes"), "value": pair[0], "stop": pair[1]}
            for i, pair in enumerate([baseline, other] * (ex.P0_DAYS // 2) + [baseline])]


def _inertia_windows(day, feed) -> tuple | None:
    """Okna testu na dobę: drogie strefy (`is_peak`) z feedu taryfy; None = feed nie zna doby (okna domyślne)."""
    runs = feed.day_peak_windows(day) if feed is not None else None
    if runs is None:
        return None
    return tuple((a, b - 1) for a, b in runs if b - a >= ex.INERTIA_MIN_WINDOW_H)


def build_schedule(kind: str, baseline, now: datetime, is_workday, feed=None) -> list[dict]:
    """Kroki [{at, value}] od `now` (P0: też `stop`); ostatni krok zawsze wraca do wartości wyjściowej."""
    steps: list[tuple[datetime, float]] = []
    if kind == "p0_ab":
        return _p0_schedule(baseline, now)
    if kind == "zapis":
        delta = ex.MAX_STEP if baseline < ex.SHIFT_MAX else -ex.MAX_STEP
        steps = [(now, baseline + delta), (now + timedelta(minutes=ex.CONFIRM_MIN), baseline)]
    elif kind == "bezwladnosc":
        day, days = now.date(), 0
        while days < ex.INERTIA_DAYS and day <= now.date() + timedelta(days=14):
            windows = _inertia_windows(day, feed)
            if windows is not None or is_workday(day):
                for start_h, end_h in (ex.INERTIA_WINDOWS if windows is None else windows):
                    start = datetime.combine(day, datetime.min.time()).replace(hour=start_h)
                    if start >= now + timedelta(minutes=5):
                        steps += [(start, baseline - 1), (start.replace(hour=end_h), baseline)]
                days += any(at.date() == day for at, _ in steps)
            day += timedelta(days=1)
    return [{"at": at.isoformat(timespec="minutes"), "value": value} for at, value in steps]


def _validate_p0(schedule: list[dict], baseline: tuple) -> str | None:
    if not schedule or (schedule[-1]["value"], schedule[-1].get("stop")) != baseline:
        return "harmonogram nie kończy się powrotem do wartości wyjściowej"
    for step in schedule:
        if (step["value"], step.get("stop")) not in ex.P0_PAIRS:
            return f"para {step['value']:g}/{step.get('stop')} spoza dozwolonych {', '.join(ex._fmt(p) for p in ex.P0_PAIRS)}"
    return None


def validate(schedule: list[dict], baseline) -> str | None:
    """Twarde granice w kodzie, niezależnie od definicji testu. Zwraca przyczynę odrzucenia albo None."""
    if isinstance(baseline, tuple):
        return _validate_p0(schedule, baseline)
    if not schedule or schedule[-1]["value"] != baseline:
        return "harmonogram nie kończy się powrotem do wartości wyjściowej"
    prev = baseline
    for step in schedule:
        if not ex.SHIFT_MIN <= step["value"] <= ex.SHIFT_MAX:
            return f"wartość {step['value']:g} poza zakresem {ex.SHIFT_MIN}…{ex.SHIFT_MAX}"
        if abs(step["value"] - prev) > ex.MAX_STEP:
            return f"krok {prev:g} → {step['value']:g} większy niż {ex.MAX_STEP}"
        prev = step["value"]
    return None


def _passed(conn, kind: str) -> dict | None:
    """Ostatni zakończony test danego typu, jeśli spełnił swoją miarę wyniku (karta w UI: odznaka, na koniec listy)."""
    for row in conn.execute("SELECT ended, result FROM tests WHERE kind = ? AND status = ? ORDER BY id DESC",
                            (kind, ex.DONE)):
        r = json.loads(row["result"] or "{}")
        if kind == "bezwladnosc":
            ok = (r.get("excitation") or {}).get("sufficient")        # miara testu = wymuszenie, nie identyfikacja
        elif kind == "p0_ab":
            ok = r.get("verdict")
        else:
            ok = 0 < r.get("confirmed", 0) == r.get("writes")
        if ok:
            return {"ended": row["ended"], "confirmed": r["confirmed"], "writes": r["writes"],
                    **({"verdict": r["verdict"]} if kind == "p0_ab" else {}),
                    **({"identified": bool((r.get("refit") or {}).get("identified"))} if kind == "bezwladnosc" else {})}
    return None


def _p0_result(conn, settings: dict, test, now: datetime) -> dict:
    """Wynik testu A/B: per wariant (para) średni rozrzut stref w standby, średnia stref, kWh/dobę i temp. zewn.;
    werdykt: 5/5 wygrywa, gdy rozrzut niższy o ≥ ex.P0_MIN_GAIN_C i średnia stref nie spada (z tolerancją szumu)."""
    steps = json.loads(test["schedule"])
    tz = ZoneInfo(settings.get("timezone") or "Europe/Warsaw")
    pooled: dict[str, dict] = {}
    for cur, nxt in zip(steps, steps[1:]):                 # doba = od kroku do następnego (ostatni krok to powrót)
        start, end = datetime.fromisoformat(cur["at"]), datetime.fromisoformat(nxt["at"])
        v = pooled.setdefault(ex._fmt((cur["value"], cur["stop"])), {"days": 0, "mode": {}, "zones": {}, "n": 0,
                                                                  "kwh": [], "out": []})
        v["days"] += 1
        samples = summaries.zone_samples(conn, settings, int(start.replace(tzinfo=tz).timestamp()),
                                         int(end.replace(tzinfo=tz).timestamp()))
        if samples:
            v["mode"].update(samples[0])
            v["zones"].update(samples[1])
            v["n"] = samples[2]
        kwh = ex._day_kwh(conn, settings, start.date(), now)
        if kwh is not None:
            v["kwh"].append(kwh)
        out = conn.execute("SELECT AVG(outdoor_temp_c) FROM cycles WHERE loop = 'heiko' AND ts >= ? AND ts < ?",
                           (start.isoformat(), end.isoformat())).fetchone()[0]
        if out is not None:
            v["out"].append(out)
    variants = {}
    for label, v in pooled.items():
        spread = summaries.standby_spread(v["mode"], v["zones"], v["n"]) if v["n"] else None
        variants[label] = {"days": v["days"], "spread_c": spread and spread["spread_c"],
                           "zones_avg_c": spread and spread["zones_avg_c"], "samples": spread["samples"] if spread else 0,
                           "kwh_day": round(mean(v["kwh"]), 2) if v["kwh"] else None,
                           "outdoor_c": round(mean(v["out"]), 1) if v["out"] else None}
    a, b = variants.get("1/6"), variants.get("5/5")
    result: dict = {"variants": variants, "verdict": None, "confidence": None}
    if a and b and a["spread_c"] is not None and b["spread_c"] is not None:
        gain = round(a["spread_c"] - b["spread_c"], 2)
        result["gain_c"] = gain
        result["verdict"] = "5/5" if gain >= ex.P0_MIN_GAIN_C and b["zones_avg_c"] >= a["zones_avg_c"] - ex.P0_AVG_TOLERANCE_C \
            else "1/6"
        if a["kwh_day"] is not None and b["kwh_day"] is not None:
            result["extra_kwh_day"] = round(b["kwh_day"] - a["kwh_day"], 2)
        weather = a["outdoor_c"] is not None and b["outdoor_c"] is not None
        result["confidence"] = "niska" if not weather or abs(a["outdoor_c"] - b["outdoor_c"]) > ex.P0_WEATHER_BAND_C else "wysoka"
        result["outdoor_c"] = round((a["outdoor_c"] + b["outdoor_c"]) / 2, 1) if weather else None
    return result


def _done_message(test, result: dict) -> str:
    if test["kind"] != "p0_ab":
        return f"Test „{ex.KINDS[test['kind']].title}” zakończony. Wynik w zakładce Testy."
    if result["verdict"] is None:
        return "Test P0 zakończony bez werdyktu (za mało próbek w standby). Szczegóły w zakładce Testy."
    kwh = f", koszt {result['extra_kwh_day']:+g} kWh/dobę" if "extra_kwh_day" in result else ""
    return (f"Test P0 zakończony: werdykt {result['verdict']} (rozrzut pokoi przy 5/5 {'niższy' if result['gain_c'] >= 0 else 'wyższy'} o {abs(result['gain_c']):g}°C{kwh}, "
            f"pewność {result['confidence']}). Wynik w zakładce Testy.")


def _result(conn, test, now: datetime, settings: dict) -> dict:
    writes = conn.execute("SELECT ts, confirmed_at FROM pump_writes WHERE test_id = ?", (test["id"],)).fetchall()
    delays = [(datetime.fromisoformat(w["confirmed_at"]) - datetime.fromisoformat(w["ts"])).total_seconds() / 60
              for w in writes if w["confirmed_at"] not in (None, ex.NO_CONFIRMATION, ex.SUPERSEDED)]
    result = {"writes": len(writes), "confirmed": len(delays),
              "max_confirm_min": round(max(delays), 1) if delays else None}
    if test["kind"] == "p0_ab":
        result.update(_p0_result(conn, settings, test, now))
    if test["kind"] == "bezwladnosc":
        rows = conn.execute("SELECT reduced_active, water_setpoint_c, base_curve_c, curve_shift_c FROM cycles "
                            "WHERE loop = 'heiko' AND ts >= ? ORDER BY id", (test["started"],)).fetchall()
        result["excitation"] = floor_learn.excitation(rows)
        refit = floor_learn.refit_from_cycles(conn, settings, now, excitation_since=test["started"])
        result["refit"] = {k: refit.get(k) for k in ("ok", "reason", "rmse_c", "tau_h", "identified")}
    return result
