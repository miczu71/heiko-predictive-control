# PLAN S4 — T3: koniec zgadywania taryfy z zegara (Heiko 0.22.0)

Źródło: `/config/docs/ROADMAP_heiko_0_22.md` (S4), `docs/ROADMAP_tariff_single_source.md` (T3, P3, P4, P5, P9).
Decyzje T0: (1) brak pv_roi → ostatni znany harmonogram + „nieaktualne od …”; godzina bez wiersza w `tariff_hours` →
jawne „brak danych taryfy” (koszt bez wyceny, KPI pomija godzinę). Nic nie jest zgadywane z zegara.

## Kontrakt (obowiązuje obie paczki)

**`tariff_feed.py`**
- `TariffFeed(ha_client, db, entity)` i `for_settings(db, settings, ha_client=None)` — **bez** parametru `is_workday`.
- `at(dt) -> TariffHour | None` (bez zmian). `is_peak(dt) -> bool | None`, `price(dt) -> float | None`,
  `offpeak_price(dt) -> float | None` — `None` gdy brak wiersza (dla `offpeak_price`: brak jakiegokolwiek wiersza w oknie 36 h).
  Brak wiersza → `_warn("hour", …, "brak danych taryfy dla godziny <hour>")` (WARNING raz na godzinę). Usunąć `_fallback`, `import tariff`.
- Stała `NO_TARIFF_ZONE = "no_tariff"` (klucz strefy dla godziny bez wiersza w rozbiciach kosztu).
- `status(now) -> {source: 'feed'|'stale'|'none', age_min, stale_since, confidence, tariff}`:
  `none` = brak jakiegokolwiek wiersza z feedu (`_latest_fetch()` is None), `stale` = ostatni zapis feedu starszy niż `STALE_MIN`
  (`stale_since` = ISO czasu tego zapisu, `tariff`/`confidence` z ostatniego wiersza ≤ teraz), `feed` = świeży (`stale_since` None).
  **Źródło informuje tylko UI — decyzje czytają `at()` bez względu na `source`** (nieaktualny harmonogram dalej obowiązuje, decyzja T0-1).
- Nowa funkcja `offpeak_hours_per_day(db) -> int | None`: liczba godzin `is_peak = 0` w ostatniej pełnej dobie (24 wiersze)
  mającej ≥1 godzinę szczytu; `None`, gdy takiej doby nie ma.
- `tariff.py` **kasujemy w całości** (`PEAK_WINDOWS`, `is_peak_hour`, `price_for`, `HourSlot`, `schedule`, `peak_offpeak_hour_counts`,
  `fallback_hour`, `fallback_prices`, `DEFAULT_*_PRICE_PLN` — poza `default_is_workday` nic nie ma użytkowników). `default_is_workday(d) -> d.weekday() < 5`
  przenieść do nowego `workday.py` (zostaje jako domyślnik werdyktu święta w `main.py` i `cycle.workday_resolver`, jeśli to ostatnie nadal jest używane — patrz niżej).

**Zasada dla `is_workday`:** po zmianie zostaje tylko tam, gdzie rozstrzyga o czymś poza taryfą: `experiments.build_schedule/_plan/overview/start`
(+ `web.create_app` przekazuje do nich), `cycle.run_attic_cycle` (attic.decide), closure `is_workday` w `main.py`. **Wszędzie indziej parametr usunąć** razem
z martwymi pochodnymi (`workday_resolver`, `check_workday` w `run_heiko_cycle`, `_DEFAULT_WORKDAY` w advisorze, `is_workday=None` w `g13_exec.run`). Przed
usunięciem parametru potwierdź `grep`em, że ciało funkcji go nie używa poza przekazaniem do feedu/zapasu.

**Koszt bez wyceny (godzina bez wiersza):** energia liczy się do kWh, część z sieci dostaje 0 zł i trafia do strefy `NO_TARIFF_ZONE`
(w `zones` i `sources` jako `grid:no_tariff`); gdy cena jest `None` przy braku podziału na źródła (`d_grid is None`) → 0 zł i `split_complete = False`.
`offpeak_price is None` (energia z baterii o nieznanym koszcie) → 0 zł. `dhw_model.ah_effect`/`summarize`: `price_fn(...) is None` → efekt AH
bez wyceny (`extra_pln = None`, w sumie pomijany; sprawdź ciało i dostosuj istniejące testy). Brak ceny nigdy nie jest zastępowany liczbą z zegara.

**Cykl Heiko (`cycle.run_heiko_cycle`):** `row = feed.at(now)`; `is_peak, price = (row.is_peak, row.gross) if row else (None, None)`. Istniejąca
bramka `is_peak is None` pomija cykl (zmień komunikat na „brak danych taryfy”). Przed budową bloków planu: jeśli którakolwiek godzina
horyzontu (`HORIZON_H`) nie ma wiersza → `logger.warning("Heiko: taryfa nie obejmuje horyzontu planu …")`, `insert_cycle`, return (jak przy braku punktów krzywej).
`fp.build_blocks(t0, n_steps, day_start, day_end, hour_fn, step_h=…, max_len_h=…)` — `hour_fn` obowiązkowe, parametry `is_workday`, `peak_price`, `offpeak_price` usunąć.
`attic` (`run_attic_cycle`): `price = row.gross if row else None`; `attic_cost_pln = None`, gdy `price is None`.
Strefa cyklu: `row.zone if row and row.zone else None` (bez „peak”/„offpeak” z zegara).

**KPI:** `kpi.EVENING_PEAK_HOURS`, `is_evening_peak_hour`, `evening_only`, ścieżka bez `zones` — usunięte. `baseline_from_stats(..., zones)` — `zones` obowiązkowe
(godzina bez wpisu pomijana). W `_evening_rows` godzina bez wiersza → `tariff_peak = None` (pomijana przez `hourly_rows`/`peak_share_kpi`).
Wspólny helper `zones_from_db(conn, start_local, end_local) -> dict` (to samo, co dziś buduje `ensure_peak_baseline`) — używa go też `analysis.py`.

**Opcje:** usunąć `tariff_state_entity`, `tariff_price_entity` (config.yaml options + schema, run.sh, `main.py` `_options_from_env`, `cycle.py`, `live.py`, `templates/options.html` jeśli występują).

**UI (P4):** `live.collect` nie czyta encji taryfy; zwraca `tariff = {"peak": None, "label": "brak danych taryfy", "price": "—", "note": ""}`.
`web.live_data` nadpisuje z `feed.at(now_local)` (strefa czasowa z `settings["timezone"]`) przez `live.tariff_from_hour(hour)` (+ `"note"`: `""` gdy `feed`,
`"nieaktualne od HH:MM DD.MM"` gdy `stale`); `data["tariff_status"] = status`. Brak wiersza → zostaje domyślne „brak danych taryfy”.
`base.html` plakietka: `tariff_source == 'stale'` → „taryfa: dane nieaktualne od …”, `'none'` → „taryfa: brak danych z pv_roi” (zamiast „zapas G12w”).
`dashboard.html` + `static/dashboard.js`: dodać `data-live="tariff.note"` obok ceny wg istniejącego wzorca `data-live`.
**P9:** `web._local_now(settings)` (`datetime.now(ZoneInfo(settings.get("timezone") or "Europe/Warsaw")).replace(tzinfo=None)`) zastępuje powielone `datetime.now(ZoneInfo(...))` w `live_data`, `page_dashboard`, `api_g13` i `datetime.now()` w `api_plan`.
**G13:** `g13_peak.workday_from_feed` — usunąć bramkę `status(...).source != "feed"` (czyta wiersz bez względu na świeżość; brak wiersza → `None`).

**Doradca / raport:** `advisor.replay_winter(settings, now, model, feed, …)` + `_replay_effects`: `hour_fn` z `feed`; brak wiersza w horyzoncie doby → `None` (doba pominięta); `web.py`
przekazuje `feed`. `analysis.py`: baza przez `kpi.zones_from_db`; `offpeak_h = tariff_feed.offpeak_hours_per_day(conn)` przekazane do funkcji generującej wnioski (to ona dziś liczy z `fallback_hour`);
`None` → pomiń wniosek „Sufit przesuwania”; teksty „G12w” → „wg taryfy”. `battery_replay.py`: krok bez wiersza `at(t)` (cena `None`) → pomiń krok (jak `ok = False`); strefa `row.zone` (bez zgadywania „peak/offpeak”).
`stats.py:41` (`bool(row["tariff_peak"])`) — tylko zgłoś w raporcie, jeśli `None` zmienia wynik; nie zmieniaj.

## Wykonawca (kolejno W1 → W2 w jednym worktree; obie paczki „ryzyko” → `sonnet-worker`; potem jeden `verifier`)
Repo: `/config/addons/heiko_predictive_control`, kod w `heiko_predictive_control/heiko_predictive_control/` (dalej `PKG`), testy w `heiko_predictive_control/tests/`.
Skille: `addon-conventions`, `tdd` (testy czerwone przed kodem — zaraportuj). Nie uruchamiaj pytest w trakcie edycji innych plików.

### W1 — rdzeń: feed, cykl, UI, opcje
| Plik | Zmiana |
|---|---|
| `PKG/tariff_feed.py` | kontrakt wyżej: ctor, `is_peak/price/offpeak_price → None`, `NO_TARIFF_ZONE`, `status` 3-stanowy, `offpeak_hours_per_day`, bez `_fallback`; docstring modułu bez „zapasu G12w” |
| `PKG/tariff.py` → `PKG/workday.py` | skasować `tariff.py`, utworzyć `workday.py` z `default_is_workday`; zaktualizować importy (`main.py`, `cycle.py`, `web.py`, pozostałe — `grep -rn "from .tariff import\|import tariff\|tariff\.\(fallback\|default\)"`) |
| `PKG/cycle.py` | cykl Heiko i attic wg kontraktu; usunąć `fallback_*`/`price_for`/`tariff_*_entity`; martwe `workday_resolver`/`check_workday` jeśli bez użytkowników |
| `PKG/floor_plan.py` | `build_blocks` wg kontraktu (usuń `fallback_hour`) |
| `PKG/live.py` | `collect` bez encji taryfy; `tariff_from_hour` + pole `note` |
| `PKG/web.py` | `_local_now`, `live_data` (hour, note, status), `inject_tariff_source` (`source`, `stale_since`), `replay_winter` dostaje `feed`, bez `is_workday` poza experiments/g13 view; `api_plan` na `_local_now` |
| `PKG/g13_peak.py`, `PKG/g13_view.py`, `PKG/g13_exec.py` | bez bramki `source`; `for_settings` bez `is_workday`; parametr `is_workday` usunięty z `now_view/build/run/_run` gdy orphan |
| `PKG/main.py` | `_options_from_env` bez opcji taryfy; `default_is_workday` z `workday`; wywołania bez usuniętych parametrów |
| `config.yaml`, `run.sh`, `PKG/templates/base.html`, `PKG/templates/dashboard.html`, `PKG/static/dashboard.js`, `PKG/templates/options.html` | opcje usunięte; plakietka i `tariff.note` wg UI powyżej (wersji NIE podbijaj) |
| `tests/test_tariff.py` | **skasować**; nowy `tests/test_workday.py` (pn–pt) |
| `tests/test_tariff_feed.py`, `tests/test_cycle.py`, `tests/test_attic_cycle.py`, `tests/test_web.py`, `tests/test_floor_plan.py`, `tests/test_g13_*.py` | feed: `None` bez wiersza, `status` feed/stale/none (+`stale_since`), `offpeak_hours_per_day`; cykl: brak wiersza → cykl pominięty, brak wiersza w horyzoncie → plan pominięty z ostrzeżeniem; `/api/live` (P5): feed, stale (note „nieaktualne od …”), brak wiersza („brak danych taryfy”), strefa czasowa (`Europe/Warsaw` vs UTC na granicy doby); `build_blocks` z `hour_fn`; usunąć/zamienić testy zapasu zegarowego |

Test W1 (częściowy, na pierwszym planie): `/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider tests/test_tariff_feed.py tests/test_cycle.py tests/test_attic_cycle.py tests/test_web.py tests/test_floor_plan.py tests/test_g13_peak.py tests/test_g13_view.py tests/test_g13_exec.py tests/test_workday.py` — testy plików z W2 mogą być czerwone do końca W2 (raportuj je, nie naprawiaj w W1).

### W2 — konsumenci: koszty, KPI, doradca
| Plik | Zmiana |
|---|---|
| `PKG/dhw_cycles.py` | `_zone_key` (strefa z wiersza, inaczej `NO_TARIFF_ZONE`), `_cost` i `extract_cycles`: `feed` obowiązkowy, bez `is_workday`/`peak_price`/`fallback_*`/`price_for`; koszt bez wyceny wg kontraktu; `store_cycles` bez `is_workday` |
| `PKG/attic_runs.py`, `PKG/heating_runs.py` | j.w. (`price_at` → `None` bez wiersza; koszt zegarowy z całki mocy bez ceny → 0 zł); `store_runs` bez `is_workday` |
| `PKG/dhw_model.py` | `ah_effect/summarize` z ceną `None` (kontrakt) |
| `PKG/summaries.py`, `PKG/day_summary.py` | `_energy`: godzina bez wiersza (`feed.is_peak → None`) nie liczy się do szczytu (całość do `kwh`); bez `is_workday`; `day_summary.build_today` przekazuje dalej bez `is_workday` |
| `PKG/kpi.py` | wg „KPI” w kontrakcie (`zones` obowiązkowe, `zones_from_db`, bez `EVENING_PEAK_HOURS`/`evening_only`/`default_is_workday`/`fallback_hour`) |
| `PKG/analysis.py` | wg „Doradca / raport” |
| `PKG/advisor.py` | `replay_winter/_replay_effects` z `feed`; usunąć `_DEFAULT_WORKDAY`, `fallback_prices` i martwe `is_workday` w łańcuchu `run/build_snapshot/dhw_peak_share/dhw_slot_inputs/…`; `for_settings` bez `is_workday` |
| `PKG/battery_replay.py`, `PKG/experiments.py` | `battery_replay`: wg kontraktu; `experiments`: tylko `for_settings` bez `is_workday` (parametr `is_workday` w `build_schedule/_plan/overview/start` ZOSTAJE) |
| `tests/test_dhw_cycles.py`, `test_attic_runs.py`, `test_heating_runs.py`, `test_summaries.py`, `test_day_summary.py`, `test_kpi.py`, `test_analysis.py`, `test_advisor*.py`, `test_battery_replay.py`, `test_experiments.py`, `test_dhw_model.py`, `test_costs.py`, `test_stats.py` | dopasować sygnatury; nowe: godzina bez wiersza → `grid:no_tariff`/0 zł/`split_complete False`; `offpeak_price None`; `_evening_rows` bez wiersza → `tariff_peak None`; `replay_winter` bez wiersza → doba pominięta; `analysis` bez doby 24 h → brak „Sufitu przesuwania”; usunąć testy zapasu zegarowego; test `api_plan peak_price` użyć wspólnego `_local_now` (zamrożony czas), nie `datetime.now()` osobno (flake) |

Komenda testu pełna (z `/config/addons/heiko_predictive_control/heiko_predictive_control`, pierwszy plan, ~4,5 min): `/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider` (baseline: 835 passed).

**DONE:** pełny pytest zielony (0 failed, liczba testów ≥ 835 − usunięte testy zegara + nowe — podaj bilans); nowe testy czerwone przed kodem (zaraportuj);
`grep -rnE "fallback_hour|fallback_prices|PEAK_WINDOWS|EVENING_PEAK_HOURS|evening_only|tariff_state_entity|tariff_price_entity|TARIFF_STATE_ENTITY|TARIFF_PRICE_ENTITY|g12w|G12w" PKG config.yaml run.sh` — 0 trafień poza komentarzami historycznymi w `kpi.py`/`CHANGELOG.md` (wypisz każde pozostałe);
`grep -rn "from .tariff import\|import tariff$" PKG` — 0; `grep -rn "is_workday" PKG` — wypisz każde trafienie i uzasadnij pozostawienie (dozwolone: experiments, `cycle.run_attic_cycle`, closure `main.py`, `web.create_app` → experiments).
**NIE RÓB:** nie ruszaj `pv_roi_client.py`, `db.py`, `telemetry.py`, `floor_model.py`, `floor_learn.py`, `rooms.py`, `catalog.py`, `energy_source.py`, `g13_*` poza wymienionym; nie zmieniaj schematu bazy ani `BASELINE_VERSION`; nie podbijaj wersji; `CHANGELOG.md` nie edytuj; nie commituj; nie dodawaj zależności;
nie zastępuj braku danych taryfy żadną stałą ani zegarem (to cel etapu); nie „poprawiaj” sąsiedniego kodu; nie uruchamiaj pytest w trakcie edycji; nie kasuj `default_is_workday`.

## Rollback
`git revert` commitu S4. Opcje `tariff_state_entity`/`tariff_price_entity` wracają z `config.yaml` (stare wartości w `options.json` Supervisora i tak zostają nieużyte).
