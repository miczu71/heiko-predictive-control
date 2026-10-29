"""Analizator slotu 52 (opóźnienie grzałki AH w cyklu CWU, `backup_start_delay`) — jedyny parametr, który doradca
po akceptacji naprawdę zapisuje do pompy (H4). Krok zawsze ±5 min, w zakresie MIN_DELAY…MAX_DELAY.

Dwie reguły (z dziennika cykli CWU i modelu grzania AH):
- wydłuż: grzałka AH jest droga za zaoszczędzoną minutę (zł/min ≥ próg) — niech sprężarka grzeje dłużej sama;
- skróć: zimą (zewn. < próg) większość cykli trwa dłużej niż opóźnienie, a woda w min. 19 jest wyraźnie za zimna,
  przy tym AH się opłaca albo brak pomiaru. Gdy zadziała „wydłuż”, „skróć” się nie sprawdza."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from . import CONF_LOW, KIND_CHANGE, LENS_ECONOMY, Draft, Snapshot

KEY = "backup_start_delay"
STEP = 5
MIN_DELAY, MAX_DELAY = 20, 60
WINDOW_DAYS = 30                        # okno reguły AH (cykle ładowane do migawki)
WINTER_DAYS = 14
MIN_AH_CYCLES = 5                       # `summary.evaluated`
AH_VALUE_MAX_PLN_PER_MIN = 0.25         # powyżej = AH droga za zaoszczędzoną minutę
WINTER_OUTDOOR_C = 5.0
WINTER_MIN_CYCLES = 6
WINTER_OVER_SHARE = 0.5
WINTER_DEFICIT_C = 3.0
TTL_H = 72


def _aware(t: datetime) -> datetime:
    return t.astimezone() if t.tzinfo is None else t


def _start(cycle: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(cycle["start"])
    except (KeyError, TypeError, ValueError):
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def _winter_cycles(slot: dict, now: datetime, cur: float) -> list[dict]:
    since = _aware(now) - timedelta(days=WINTER_DAYS)
    out = []
    for c in slot.get("cycles") or []:
        t, dur, out_c = _start(c), c.get("duration_min"), c.get("outdoor_c")
        if t is not None and t >= since and dur is not None and out_c is not None and out_c < WINTER_OUTDOOR_C:
            out.append(c)
    return out


def analyze(snap: Snapshot) -> list[Draft]:
    slot = snap.dhw_slot
    cur = snap.params.get(KEY)
    if not slot or cur is None or cur % STEP != 0 or not (MIN_DELAY <= cur <= MAX_DELAY):
        return []
    ah = slot.get("ah") or {}
    price = ah.get("pln_per_saved_min")
    evaluated = ah.get("evaluated") or 0
    common = dict(analyzer="dhw_slot", dedupe_key=f"dhw_slot:{KEY}", lens=LENS_ECONOMY, kind=KIND_CHANGE, param_key=KEY,
                  from_value=cur, ttl_h=TTL_H, confidence=CONF_LOW)

    to = cur + STEP
    if to <= MAX_DELAY and evaluated >= MIN_AH_CYCLES and price is not None and price >= AH_VALUE_MAX_PLN_PER_MIN:
        return [Draft(
            **common, to_value=to,
            reason=(f"Grzałka AH kosztuje średnio {price:.2f} zł za każdą zaoszczędzoną minutę grzania "
                    f"(z {evaluated} cykli z ostatnich {WINDOW_DAYS} dni; próg {AH_VALUE_MAX_PLN_PER_MIN:.2f} zł/min). "
                    f"Opóźnienie jej wejścia (slot 52) z {cur:g} na {to:g} min zostawia więcej pracy samej sprężarce."),
            evidence={"pln_per_saved_min": price, "ah_cycles": evaluated, "window_days": WINDOW_DAYS,
                      "max_pln_per_min": AH_VALUE_MAX_PLN_PER_MIN})]

    to = cur - STEP
    if to < MIN_DELAY or not (price is None or price < AH_VALUE_MAX_PLN_PER_MIN):
        return []
    winter = _winter_cycles(slot, snap.now, cur)
    if len(winter) < WINTER_MIN_CYCLES:
        return []
    over = sum(1 for c in winter if c["duration_min"] > cur) / len(winter)
    deficits = [c["deficit_c"] for c in winter if c.get("deficit_c") is not None]
    if over < WINTER_OVER_SHARE or not deficits:
        return []
    med = median(deficits)
    if med < WINTER_DEFICIT_C:
        return []
    cost = f"AH kosztuje {price:.2f} zł/min zaoszczędzonego czasu" if price is not None else "brak pomiaru kosztu AH"
    return [Draft(
        **common, to_value=to,
        reason=(f"Zimą (poniżej {WINTER_OUTDOOR_C:g}°C na zewnątrz) {over * 100:.0f}% z {len(winter)} cykli CWU z ostatnich "
                f"{WINTER_DAYS} dni trwa dłużej niż {cur:g} min, a w 19. minucie woda jest średnio (mediana) o {med:.1f}°C "
                f"poniżej celu; {cost}. Wcześniejsze wejście grzałki AH (slot 52 z {cur:g} na {to:g} min) skróci cykle."),
        evidence={"winter_cycles": len(winter), "over_share": round(over, 3), "median_deficit_c": round(med, 2),
                  "pln_per_saved_min": price, "window_days": WINTER_DAYS, "outdoor_below_c": WINTER_OUTDOOR_C})]
