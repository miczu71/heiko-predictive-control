"""Jednorazowe odtworzenie składu baterii (E2) z historii recordera HA za ostatnie dni.

Kroki co 15 min z przyrostów liczników (sieć, ładowanie, rozładowanie) i SoC; wynik trafia do tabeli
`battery_mix` aż do `now`: wiersze pętli A z tego okna są zastępowane (pętla A rusza przy starcie przed replayem i
zaczęłaby łańcuch od jednej kategorii „nieznane”), a skład na żywo (`battery_mix` w settings) startuje z ostatniego kroku.

Uproszczenie: replay nie zna licznika pompy, więc krok liczy z `load_kwh=0` i `from_grid = min(charge, import)`.
To lekko zawyża „sieć” w ładowaniu (pobór pompy wlicza się w import) — świadomie przyjęte, pompa ≪ ładowanie nocne.
Zero zapisów do HA i pompy."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import catalog, db as dbm
from . import energy_source as es
from .dhw_cycles import BACKFILL_DAYS

logger = logging.getLogger(__name__)

STEP_MIN = 15
DONE_KEY = "battery_mix_replayed"
_KEYS = ("grid_import", "battery_charge", "battery_discharge", "battery_soc", "battery_capacity", "rce_price")


def _utc(t: datetime) -> datetime:
    return t.astimezone(timezone.utc)      # czas lokalny bez strefy -> UTC


def replay(conn, settings: dict, now: datetime, get_history, feed, back_days: int = BACKFILL_DAYS) -> int:
    """Zapisz skład baterii z `back_days` dni wstecz; zwraca liczbę zapisanych wierszy (0 = nic / już zrobione)."""
    if dbm.get_setting(conn, DONE_KEY):
        return 0
    ent = es.entities(settings)
    fetch_start = now - timedelta(days=back_days, hours=1)
    history = get_history(list(ent.values()), _utc(fetch_start).isoformat(), _utc(now).isoformat())
    if history is None:
        logger.warning("Replay składu baterii: brak historii z HA, spróbuję przy następnym starcie")
        return 0
    missing = [ent[k] for k in _KEYS if not history.get(ent[k])]
    if missing:
        logger.warning("Replay składu baterii: brak serii %s", ", ".join(missing))
        return 0
    c = {k: es.Counter((t, catalog.to_number(s)) for t, s in history[ent[k]]) for k in _KEYS}

    cutoff = now
    start = (now - timedelta(days=back_days)).replace(minute=(now.minute // STEP_MIN) * STEP_MIN,
                                                      second=0, microsecond=0)
    cost0 = feed.offpeak_price(start)

    def soc_kwh_at(t: datetime) -> float | None:
        soc, cap = c["battery_soc"].at(_utc(t)), c["battery_capacity"].at(_utc(t))
        return None if soc is None or cap is None else soc / 100 * cap / 1000

    s0 = soc_kwh_at(start)
    mix = {"unknown": {"kwh": s0, "pln": s0 * cost0}} if s0 is not None else None
    rows: list[tuple[str, dict]] = []
    t = start + timedelta(minutes=STEP_MIN)
    while t < cutoff:
        prev = t - timedelta(minutes=STEP_MIN)
        t_utc, prev_utc = _utc(t), _utc(prev)
        d = [c[k].delta(prev_utc, t_utc) for k in ("grid_import", "battery_charge", "battery_discharge")]
        soc_kwh, rce = soc_kwh_at(t), c["rce_price"].at(t_utc)
        ok = soc_kwh is not None and rce is not None and all(
            v is not None and 0 <= v <= es.MAX_STEP_KWH for v in d)
        if ok:
            if mix is None:                      # brak SoC w starcie: pierwszy pełny krok jest startem
                mix = {"unknown": {"kwh": soc_kwh, "pln": soc_kwh * cost0}}
            step = es.Step(0.0, d[0], d[1], d[2], soc_kwh, feed.price(t), rce)
            row = feed.at(t)
            zone = row.zone if row is not None and row.zone else ("peak" if feed.is_peak(t) else "offpeak")
            mix = es.mix_after(mix, step, cost0 if (avg := es.mix_cost(mix)) is None else avg, zone)
            rows.append((t.isoformat(), mix))
        t += timedelta(minutes=STEP_MIN)

    if not rows:
        return 0
    try:
        conn.execute("DELETE FROM battery_mix WHERE ts >= ?", (rows[0][0],))
        for ts, m in rows:                       # jedna transakcja: częściowy zapis przesunąłby cutoff i zablokował replay
            conn.execute("INSERT OR REPLACE INTO battery_mix (ts, mix) VALUES (?, ?)", (ts, json.dumps(m)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    dbm.set_setting(conn, "battery_mix", rows[-1][1])
    dbm.set_setting(conn, DONE_KEY, now.isoformat())
    return len(rows)
