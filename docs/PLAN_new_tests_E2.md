# E2 — miara `dhw_cycle`, wydanie 0.28.0

## Uzupełnienie względem roadmapy

Cykle CWU zapisuje do bazy zadanie o 00:25 (doba poprzednia), a test z zapisami kończył się zaraz po ostatnim kroku
(00:00). Bez czekania `dhw_cycle` gubiłaby ostatnią dobę. Test z tą miarą zamyka się od 00:45 po ostatniej dobie
(oba tory: z zapisami i same nakładki). Restart add-onu w tym oknie nadal przerywa test z zapisami.

## Wykonawca (haiku-worker)

Katalog kodu: `heiko_predictive_control/heiko_predictive_control/`, testy: `heiko_predictive_control/tests/`.

1. `testkit/measures.py`: import `dhw_cycles` (do istniejącego `from .. import floor_learn, heating_runs, summaries`).
   Po `tail_cuts` dodać:

```python
# Pole cyklu CWU → liczba miejsc po przecinku; `kwh_total` jest wartością miary.
_DHW_FIELDS = {"kwh_total": 3, "duration_min": 1, "ah_kwh": 3, "ah_min": 1, "cost_pln": 2}


def _mean_known(values: list) -> float | None:
    known = [v for v in values if v is not None]
    return mean(known) if known else None


def dhw_cycle(ctx: Ctx, days: list[ArmDay]) -> dict:
    """Cykl CWU z zegara panelu (bez kąpieli, PV i zapotrzebowania): per doba średnie z kompletnych cykli, wariant =
    średnia dób. `dhw_cycles.start` to UTC, doby wariantu są lokalne. Doba bez cyklu z energią pomijana."""
    per_day = []
    for d in days:
        cycles = [c for c in dhw_cycles.load_range(ctx.conn, _to_utc(d.start, ctx.tz), _to_utc(d.end, ctx.tz))
                  if c.get("complete") and c.get("type") == "zegar"]
        means = {k: _mean_known([c.get(k) for c in cycles]) for k in _DHW_FIELDS}
        if means["kwh_total"] is not None:
            per_day.append({"day": d.start.date().isoformat(), **means})

    def rounded(source: dict) -> dict:
        return {k: None if source[k] is None else round(source[k], digits) for k, digits in _DHW_FIELDS.items()}

    total = rounded({k: _mean_known([p[k] for p in per_day]) for k in _DHW_FIELDS})
    return {"value": total["kwh_total"], **total, "n": len(per_day),
            "per_day": [{"day": p["day"], "value": rounded(p)["kwh_total"], **rounded(p)} for p in per_day]}
```

   W `CATALOG`, po `tail_cuts`: `"dhw_cycle": Measure("arm", dhw_cycle),`.

2. `testkit/params.py`: dodać `"dhw_cycle"` do `MEASURES`.

3. `testkit/engine.py`:
   - `MEASURE_TEXT`: `"dhw_cycle": "średnia energia cyklu CWU z zegara panelu (kWh, niższy lepszy); bez kąpieli i grzania z PV",`
   - po `end_at` dodać:

```python
def _measure_names(defn: dict) -> set[str]:
    return {m["name"] if isinstance(m, dict) else m for m in defn.get("measures") or []}


def ready_at(defn: dict, arms_by_day: dict[str, str]) -> datetime | None:
    """Od kiedy można liczyć wynik: `end_at`, ale miary z nocnych dzienników czekają na ich zapis — cykle CWU (00:25)
    do 00:45 po ostatniej dobie, przebiegi podłogówki (00:30) 45 min po końcu o północy. None = brak dób."""
    end = end_at(defn, arms_by_day)
    if end is None:
        return None
    names = _measure_names(defn)
    if "dhw_cycle" in names:
        last = (end - timedelta(microseconds=1)).date()
        return datetime.combine(last + timedelta(days=1), time(0, 45))
    if "floor_cost" in names and end.time() == time():
        return end + timedelta(minutes=45)
    return end


def waits_for_dhw_log(defn: dict, arms_by_day: dict[str, str], now: datetime) -> bool:
    """Test z zapisami po ostatnim kroku: czy czekać z wynikiem na dziennik cykli CWU."""
    if "dhw_cycle" not in _measure_names(defn):
        return False
    ready = ready_at(defn, arms_by_day)
    return ready is not None and now < ready
```

4. `experiments.py`:
   - `_tick_overlay`: usunąć komentarz „Okno kończące się o północy…”, linie `names = …` i `settle = …`; warunek:
     `ready = engine.ready_at(defn, arms)` i `if ready is None or now >= ready:`. `end` i `"until": _iso(end)` zostają.
     Jeśli `time` przestaje być używane w `experiments.py`, usunąć z importu.
   - `tick`: przed linią `if current is None:` (po sprawdzeniu `confirmed_at is None`) przenieść tu
     `schedule = json.loads(test["schedule"])` (usunąć z dalszego miejsca) i dodać:

```python
        # Po ostatnim kroku nastawa jest już bazowa: czekając na dziennik cykli CWU nie pilnujemy zmian ręcznych.
        if test["step"] >= len(schedule) and engine.waits_for_dhw_log(defn, json.loads(test["arms_json"] or "{}"), now):
            return {"status": RUNNING, "waiting": "zapis cykli CWU"}
```

5. Testy:
   - `tests/test_testkit_schema.py`: zbiór `MEASURES` w asercji + `"dhw_cycle"`.
   - `tests/test_testkit_measures.py` (seam: `ms.dhw_cycle(ctx(conn), [day(...)])`, wiersze wstawiane do `dhw_cycles`
     jak helper `run`, `start` w UTC przez `local_to_utc`): (a) dwa cykle zegarowe w dobie uśredniane, potem średnia
     dób; cykl typu „nadwyżka PV” i cykl `complete: False` pominięte; (b) granica doby w czasie lokalnym: cykl
     o 00:30 lokalnie należy do tej doby (w UTC to dzień wcześniej); (c) brak cykli → `value` None, `n` 0;
     (d) `ah_kwh` None w cyklu nie psuje średniej pozostałych pól.
   - `tests/test_testkit_engine.py` (seam: `_start`/`_tick`/`_run`, fixture `dhw_setpoint_ab` z
     `measures=["dhw_cycle"]`, `verdict.primary="dhw_cycle"`, `_ent_pump`, `_add_def`): test z zapisami po ostatnim
     kroku ma `RUNNING` o 00:10, także gdy nastawę CWU zmieniono ręcznie w tym oknie, a `DONE` o 00:45;
     test nakładek z `dhw_cycle` (wzorzec `test_overlay_floor_cost_waits_for_nightly_heating_runs`) analogicznie.

6. Wersja `0.28.0`: `heiko_predictive_control/config.yaml` i `__init__.py`. `CHANGELOG.md` (po polsku, styl pliku):
   nowa miara `dhw_cycle` (co liczy, pominięte cykle); test z tą miarą zamyka się od 00:45 po ostatniej dobie;
   bez migracji bazy; przed powrotem do 0.27.0 przerwać test używający tej miary.

**Test:** `cd heiko_predictive_control && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with pyyaml --with-requirements requirements.txt python -m pytest -q -p no:cacheprovider tests/test_testkit_*.py`

**DONE:** testy testkit przechodzą, nowe testy z pkt 5 istnieją, wersja 0.28.0 w obu plikach.

**NIE RÓB:** commit/push; zmiany w `dhw_cycles.py`, `static/`, `docs/`, istniejących plikach `tests/*.json`; zmiany
zachowania `floor_cost`.

## Po wydaniu (rodzic)

Definicje przez API (roadmapa E2 pkt 5). Rollback: 0.27.0 z Supervisora, `git revert`.

## Zmiany po planie
- Review: restart w oknie czekania nie przerywa już testu, który czeka tylko na nocny zapis; `floor_cost` z oknem przez
  północ czeka na przebiegi (poprawka zachowania, wbrew „NIE RÓB”). `waits_for_dhw_log` zastąpione przez `ready_at`.
- 0.28.1 (decyzja usera): `dhw_cycle` bierze poranne starty o 4/6/8 (−1…+5 min) zamiast typu „zegar”.
