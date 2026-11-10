"""Harmonogram testu z definicji (E2). Czysty moduł: bez I/O, bez zegara (`now` z argumentu), bez `web`.

`build(defn, baseline, now, is_workday, feed=None) -> Plan`:
- `baseline`: `{param: wartość}`, np. `{"curve_shift": 0.0}` albo `{"p0_run": 1.0, "p0_stop": 6.0}`;
- `is_workday(date) -> bool` i `feed` (z `day_peak_windows(date)`) są wstrzykiwane, jak w `experiments.build_schedule`;
- definicja jest już zwalidowana (`schema.validate`); przy czymkolwiek nierozwiązywalnym (np. `other` przy bazie
  spoza `experiments.P0_PAIRS`) zwraca `Plan([], {})`.
Semantyka: docs/PLAN_heiko_testkit_E2.md, „Semantyka schedule.build”.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .. import experiments as ex

# Parsowanie wartości już zwalidowanej przez schema (bez importu schema: ciągnie `params` → `web`).
_BASE_OFFSET_RE = re.compile(r"base([+-])(\d+(?:\.\d+)?)")
_BASE_TILDE_RE = re.compile(r"base~(\d+(?:\.\d+)?)")
_REL_RE = re.compile(r"\+\d+m")
_REL_TO_RE = re.compile(r"\+([1-9]\d{0,3})m")
_SPAN_RE = re.compile(r"(\d{2}):(\d{2})\s*(?:→|-)\s*(\d{2}):(\d{2})")

# Allowlista zapisu: nastawa → sufiks encji (ta sama reguła co `experiments._WRITE_RULES`).
_SUFFIX = {
    "curve_shift": ex.ALLOWED_ENTITY_SUFFIX,
    "p0_run": ex.P0_RUN_SUFFIX,
    "p0_stop": ex.P0_STOP_SUFFIX,
    "slot52": ex.SLOT52_SUFFIX,
    "dhw": ex.DHW_SUFFIX,
}


class _Unresolvable(Exception):
    """Definicja nie daje się rozwiązać dla danej bazy/feedu → cały plan pusty."""


@dataclass(frozen=True)
class Plan:
    steps: list[dict] = field(default_factory=list)        # {"at": "YYYY-MM-DDTHH:MM", "param": str, "value": float}
    arms_by_day: dict[str, str] = field(default_factory=dict)   # {"2026-10-09": "A"}


@dataclass(frozen=True)
class _Ctx:
    baseline: dict
    now: datetime
    is_workday: object
    feed: object
    windows: list                 # top-level `windows` (pusta lista = brak)
    fallback: list                # top-level `windows_fallback`
    min_h: int

    @property
    def today(self) -> date:
        return self.now.date()

    @property
    def earliest(self) -> datetime:
        return self.now + timedelta(minutes=5)


def build(defn: dict, baseline: dict, now: datetime, is_workday, feed=None) -> Plan:
    try:
        return _build(defn, baseline, now, is_workday, feed)
    except _Unresolvable:
        return Plan([], {})


def _build(defn: dict, baseline: dict, now: datetime, is_workday, feed) -> Plan:
    arms = defn["arms"]
    arm_keys = list(arms)
    design = defn["design"]
    order = defn.get("order", arm_keys)
    flt, count = defn["days"]["filter"], defn["days"]["count"]
    ctx = _Ctx(baseline, now, is_workday, feed, defn.get("windows") or [], defn.get("windows_fallback") or [],
               defn.get("min_window_h", 1))

    collected: list[tuple[datetime, str, float]] = []
    arms_by_day: dict[str, str] = {}
    last = None                                   # (dzień, wariant) ostatniej liczącej się doby
    day = ctx.today
    while len(arms_by_day) < count and day <= ctx.today + timedelta(days=max(14, 2 * count + 7)):
        n = len(arms_by_day)
        if design == "sequence" and n >= len(order):
            break
        if _qualifies(flt, day, ctx):
            arm = arm_keys[0] if design == "single" else order[n % len(order)]
            writes = arms[arm].get("writes", [])
            day_steps = [step for w in writes for step in _write_steps(w, day, ctx)]
            if day_steps or not writes:
                arms_by_day[day.isoformat()] = arm
                collected += day_steps
                last = (day, arm)
        day += timedelta(days=1)

    if defn.get("restore", True) and last is not None:
        last_day, arm = last
        for w in arms[arm].get("writes", []):
            if _is_whole_day(w, ctx):
                param = w["param"]
                collected.append((datetime.combine(last_day + timedelta(days=1), time()), param,
                                  _base(param, ctx.baseline)))

    collected.sort(key=lambda item: item[0])      # stabilne: przy równym czasie kolejność jak w `writes`
    steps = [{"at": when.isoformat(timespec="minutes"), "param": param, "value": float(value)}
             for when, param, value in collected]
    return Plan(steps, arms_by_day)


def _qualifies(flt: str, day: date, ctx: _Ctx) -> bool:
    if flt == "all":
        return True
    runs = ctx.feed.day_peak_windows(day) if ctx.feed is not None else None
    if runs is None:                              # feed nie zna doby → dzień roboczy z kalendarza
        return bool(ctx.is_workday(day))
    if flt == "workday":
        return any(b - a >= ctx.min_h for a, b in runs)
    return bool(runs)                             # peak_day: ≥1 odcinek szczytu


def _is_whole_day(w: dict, ctx: _Ctx) -> bool:
    return "from" not in w and "to" not in w and not ctx.windows


def _write_steps(w: dict, day: date, ctx: _Ctx) -> list[tuple[datetime, str, float]]:
    param, spec = w["param"], w["value"]
    frm, to = w.get("from"), w.get("to")
    if _is_whole_day(w, ctx):
        at = datetime.combine(day, time())
        return [(at, param, _resolve(spec, param, ctx))] if at >= ctx.earliest else []
    rel = _REL_TO_RE.fullmatch(to) if isinstance(to, str) else None
    if frm == "+0m" and rel:                      # względne: tylko dziś, bez reguły „≥ now+5 min”
        if day != ctx.today:
            return []
        end = ctx.now + timedelta(minutes=int(rel.group(1)))
        return [(ctx.now, param, _resolve(spec, param, ctx)), (end, param, _base(param, ctx.baseline))]
    if frm is None and to is None:
        windows = [span for entry in ctx.windows for span in _entry_spans(entry, day, ctx)]
    elif isinstance(frm, str) and isinstance(to, str) and frm == to == "zone:peak":
        windows = _peak_spans(day, ctx)
    elif isinstance(frm, str) and isinstance(to, str) and _HHMM(frm) and _HHMM(to):
        windows = [(_at(day, frm), _at(day, to))]
    else:
        raise _Unresolvable(f"okno {frm!r}→{to!r}")
    out = []
    for start, end in windows:
        if start < ctx.earliest:
            continue
        out.append((start, param, _resolve(spec, param, ctx)))
        out.append((end, param, _base(param, ctx.baseline)))
    return out


def _HHMM(text: str) -> bool:
    return bool(re.fullmatch(r"\d{2}:\d{2}", text))


def _at(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(day, time(int(h), int(m)))


def _span(day: date, text: str) -> tuple[datetime, datetime]:
    m = _SPAN_RE.fullmatch(text)
    if not m:
        raise _Unresolvable(f"okno {text!r}")
    return _at(day, f"{m.group(1)}:{m.group(2)}"), _at(day, f"{m.group(3)}:{m.group(4)}")


def _entry_spans(entry: str, day: date, ctx: _Ctx) -> list[tuple[datetime, datetime]]:
    if entry == "zone:peak":
        return _peak_spans(day, ctx)
    return [_span(day, entry)]


def _peak_spans(day: date, ctx: _Ctx) -> list[tuple[datetime, datetime]]:
    """Drogie strefy z feedu: odcinek [a, b) → okno (a, b−1), gdy trwa ≥ min_window_h; feed bez doby → fallback."""
    runs = ctx.feed.day_peak_windows(day) if ctx.feed is not None else None
    if runs is None:
        return [_span(day, text) for text in ctx.fallback]
    return [(datetime.combine(day, time(a)), datetime.combine(day, time(b - 1)))
            for a, b in runs if b - a >= ctx.min_h]


def _base(param: str, baseline: dict):
    if param not in baseline:
        raise _Unresolvable(f"brak bazy dla {param}")
    return baseline[param]


def _other(param: str, baseline: dict) -> float:
    """Element drugiej pary P0 niż bieżąca; baza spoza `P0_PAIRS` → nierozwiązywalne."""
    if param not in ("p0_run", "p0_stop"):
        raise _Unresolvable(f"other dla {param}")
    pair = (_base("p0_run", baseline), _base("p0_stop", baseline))
    if pair not in ex.P0_PAIRS:
        raise _Unresolvable("baza P0 spoza P0_PAIRS")
    other = next(p for p in ex.P0_PAIRS if p != pair)
    return other[0] if param == "p0_run" else other[1]


def _allowed(param: str, old: float, new: float) -> bool:
    if param not in _SUFFIX:
        raise _Unresolvable(f"nastawa {param}")
    return ex._WRITE_RULES[_SUFFIX[param]](old, new)


def _resolve(spec, param: str, ctx: _Ctx) -> float:
    """Wartość zapisu względem bazy: liczba, `base`, `base±N`, `base~N` (+N, a gdy poza regułą −N), `other`."""
    if isinstance(spec, bool):
        raise _Unresolvable("wartość logiczna")
    if isinstance(spec, (int, float)):
        return float(spec)
    if not isinstance(spec, str):
        raise _Unresolvable(f"wartość {spec!r}")
    if spec == "base":
        return float(_base(param, ctx.baseline))
    if spec == "other":
        return _other(param, ctx.baseline)
    m = _BASE_OFFSET_RE.fullmatch(spec)
    if m:
        step = float(m.group(2))
        return _base(param, ctx.baseline) + (step if m.group(1) == "+" else -step)
    m = _BASE_TILDE_RE.fullmatch(spec)
    if m:
        base, step = _base(param, ctx.baseline), float(m.group(1))
        return base + step if _allowed(param, base, base + step) else base - step
    raise _Unresolvable(f"wartość {spec!r}")
