# PLAN S3 — T2: klient pv_roi, historia stref, baza KPI v4 (Heiko 0.22.0)

Źródło: `/config/docs/ROADMAP_heiko_0_22.md` (S3), `docs/ROADMAP_tariff_single_source.md` (T2, P2, P6, P7).
Decyzje T0: baza KPI przeliczana wg bieżących stref pv_roi; stara baza zostaje do udanego przeliczenia; godzina bez wiersza = pomijana w KPI.

## Zmiana
1. **Klient** `pv_roi_client.py`: `GET {pv_roi_url}/api/tariff/schedule?start=YYYY-MM-DDTHH&hours=168` (strony po 168 h, `requests`, timeout 15 s). Wiersze parsowane tym samym `tariff_feed._parse_item`. Strona z HTTP 400 (sprzed katalogu) = brak danych, nie błąd. Błąd sieci/5xx/zły JSON = błąd (`logger.warning`, wynik `False`). Historia zapisywana z `fetched_at = ''`, żeby nie psuła świeżości feedu (`_latest_fetch` bierze `MAX(fetched_at)`; pusty napis nie parsuje się jako data).
2. **Opcja** `pv_roi_url` (domyślnie `http://29a4454d-pv-roi-tracker:8099`): `config.yaml` (options + schema `str?`), `run.sh`, `main.py` (`_options_from_env`).
3. **Baza KPI v4** (`kpi.py`): `BASELINE_VERSION = 4`; `baseline_from_stats(..., zones=...)` — gdy podano `zones` (dict `'YYYY-MM-DDTHH'` → `(zone, is_peak)`), godzina jest szczytem, gdy `zone == EVENING_ZONE`; godzina bez wpisu w `zones` jest pomijana (nie liczy się ani do sumy, ani do szczytu). Bez `zones` zachowanie dotychczasowe (woła tak `analysis.py` — S4 to usunie). Parametr `evening_only` i `EVENING_PEAK_HOURS` nadal istnieją tylko dla ścieżki bez `zones` (S4 je usuwa).
4. **Sygnatura**: `tariff_signature(conn, start_local, end_local)` = sha1 (12 znaków) z posortowanych `(hour, tariff, zone)` okna sezonu z `tariff_hours`; zapisywana w bazie jako `sig`.
5. **`ensure_peak_baseline`** (nowa kolejność): (a) jest baza v4 i `peak_baseline_checked` < 24 h temu → zwróć ją; (b) `pv_roi_client.backfill_window(...)` pobiera całe okno sezonu (`season_window`, w czasie lokalnym); porażka → zwróć istniejącą bazę (stara zostaje, **nie kasować**), nie zapisuj `peak_baseline_checked`; (c) sygnatura; jeśli istnieje baza v4 z tą samą `sig` → zapisz `peak_baseline_checked`, zwróć ją; (d) statystyki HA (`get_statistics`); `None` → zwróć istniejącą; (e) `baseline_from_stats(zones=…)`, zapis z `v=4`, `sig`, `at`; zapisz `peak_baseline_checked`. Stara baza v3 zostaje do udanego zapisu v4.
6. **P7**: job `peak_baseline` w `main.py`: `interval hours=1` z `next_run_time` +45 s (zamiast jednorazowego `date`).
7. **P2**: w `baseline_from_stats(zones=…)` i `_evening_rows` strefa spoza znanych (`tariff_feed.KNOWN_ZONES` = zbiór: `evening_peak`, `morning_peak`, `peak`, `offpeak`, `off_peak`, `day`, `night`, `shoulder`, `other`, `weekend`; sprawdź w `pv_roi_tracker/tariff_zones.py` rzeczywiste nazwy i użyj ich) → `logger.warning` raz na godzinę (wzorzec `_warn` z `tariff_feed`) + gdy `is_peak` z feedu = 1, a strefa ≠ `evening_peak` i nie jest znaną strefą szczytu → ostrzeżenie o niespójności. Brak zmiany liczenia.
8. **P6**: `web.py` `api_plan`: `peak_price` z bieżącej godziny — wiersz `tariff_hours` dla `hour_key(now)` ze strefą `evening_peak`; brak takiego wiersza = `None`. `now` lokalny: dziś `datetime.now()` (TZ kontenera = `Europe/Warsaw`) — zostaje, ale w jednym miejscu (`now = datetime.now()` raz na żądanie, użyte w `recent_kpi` i `hour_key`). Test `peak_price is None` (brak wiersza evening_peak o tej godzinie, stary wiersz sprzed godzin nie liczy się).

## Wykonawca (sonnet-worker; paczka „ryzyko” — pieniądze/taryfy; jedna paczka)
Repo: `/config/addons/heiko_predictive_control`, kod w `heiko_predictive_control/heiko_predictive_control/`, testy w `heiko_predictive_control/tests/`.
Skille: `addon-conventions`, `tdd` (testy czerwone przed kodem — zaraportuj).

| Plik | Zmiana |
|---|---|
| `heiko_predictive_control/pv_roi_client.py` (nowy) | `fetch_page(base_url, start: str, hours=168, get=requests.get) -> list[tuple] \| None` (None = błąd; `[]` = HTTP 400); `backfill_window(conn, base_url, start_local, end_local, get=requests.get) -> bool` — strony po 168 h od pełnej godziny `start_local` do `end_local`, `INSERT … ON CONFLICT(hour) DO UPDATE` z `fetched_at = ''` **tylko gdy istniejący wiersz ma `fetched_at = ''` albo go nie ma** (nie nadpisuj wierszy z feedu: warunek `WHERE tariff_hours.fetched_at = ''`), `commit` po każdej stronie; zwraca False przy pierwszym błędzie strony |
| `heiko_predictive_control/kpi.py` | pkt 3, 4, 5, 7 (`tariff_signature`, `zones`, nowa `ensure_peak_baseline` z parametrem `fetch_history=pv_roi_client.backfill_window`, `BASELINE_VERSION = 4`) |
| `heiko_predictive_control/tariff_feed.py` | `KNOWN_ZONES` + `warn_unknown_zone(zone, is_peak, dt)` (współdzielone z `kpi.py`, używa `_warn`-owego wzorca raz/h) |
| `heiko_predictive_control/main.py` | `pv_roi_url` w `_options_from_env` (`_env("PV_ROI_URL", "http://29a4454d-pv-roi-tracker:8099")`); job `peak_baseline` co godzinę (pkt 6) |
| `heiko_predictive_control/web.py` | `api_plan` — pkt 8 |
| `config.yaml`, `run.sh` | opcja `pv_roi_url` (options: domyślna wartość; schema: `pv_roi_url: str?`; run.sh: `export PV_ROI_URL=$(jq -r '.pv_roi_url // ""' "$CONFIG")`) — wersji NIE podbijaj |
| `heiko_predictive_control/tests/test_pv_roi_client.py` (nowy) | strona OK (parsowanie, `fetched_at == ''`), 400 → `[]`, wyjątek sieci/5xx → `None`; `backfill_window`: stronicowanie (np. 400 h = 3 strony, kolejne `start`), porażka drugiej strony → `False`, wiersz z feedu (`fetched_at` ISO) nie nadpisany, `_latest_fetch` ignoruje wiersze z historii |
| `heiko_predictive_control/tests/test_kpi.py` | przepisz/dodaj: `zones` — szczyt wg strefy, godzina bez wpisu pominięta, lato/zimno bez zmian; sygnatura zmienia się po zmianie strefy; `ensure_peak_baseline`: (1) pierwsza baza v4 z `sig`; (2) druga w <24 h nie woła ani `fetch_history`, ani `get_statistics`; (3) `fetch_history` False → zwraca starą bazę v3, `peak_baseline` nietknięty, brak `peak_baseline_checked`; (4) statystyki `None` → stara baza zostaje; (5) po 24 h ta sama `sig` → bez `get_statistics`; (6) zmiana strefy → przeliczenie; usuń/zamień testy `test_failed_recompute_drops_the_old_clock_baseline`, `test_old_clock_only_baseline_is_recomputed_once`, `test_v2_baseline_…` na wersje zgodne z P7 (stara baza zostaje); P2: `caplog` — nieznana strefa → jedno ostrzeżenie na godzinę |
| `heiko_predictive_control/tests/test_web*.py` (znajdź test `api_plan`; jeśli brak — nowy `test_web_plan_peak_price.py` na wzór istniejących testów Flask) | `peak_price`: jest wiersz `evening_peak` bieżącej godziny → wartość; tylko stary wiersz (godzinę wcześniej) → `None`; brak wierszy → `None` |

Komenda testu (z `/config/addons/heiko_predictive_control/heiko_predictive_control`, na pierwszym planie, ~4 min pełny; w trakcie pracy odpalaj `tests/test_kpi.py tests/test_pv_roi_client.py tests/test_tariff_feed.py`):
`/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider` (baseline: 808 passed; jeśli interpreter nie ma pytest, zgłoś — nie instaluj niczego globalnie).

**DONE:** pytest zielony (≥ 808 + nowe, 0 failed); nowe testy czerwone przed zmianą kodu (zaraportuj); `grep -n "BASELINE_VERSION = 4" kpi.py` ma trafienie; `grep -rn "pv_roi_url\|PV_ROI_URL" config.yaml run.sh heiko_predictive_control/main.py` ma 4 trafienia (options, schema, run.sh, main.py).
**NIE RÓB:** nie ruszaj `tariff.py`, `cycle.py`, `live.py`, `dhw_cycles.py`, `attic_runs.py`, `heating_runs.py`, `summaries.py`, `floor_plan.py`, `analysis.py`, `advisor.py`, `g13_*.py`, `experiments.py`; nie usuwaj `fallback_hour`/`default_is_workday` (to S4); nie usuwaj opcji `tariff_state_entity`/`tariff_price_entity`; wersji nie podbijaj; `CHANGELOG.md` nie edytuj; nie commituj; nie dodawaj zależności (`requests` już jest); nie zmieniaj zachowania poza opisanym.

## Łączność Heiko → pv_roi
Z tego kontenera nie da się jej sprawdzić (HA to osobny host, bez dostępu do sieci hassio), a nowy kod Heiko trafia na żywo dopiero w S9. Dlatego: klient loguje błąd `WARNING` z adresem i przyczyną (bez tokenów), a weryfikacja na żywo S9 sprawdza `peak_baseline` v4 w logach. Jeśli połączenie nie działa, wariant awaryjny (historia przez atrybut MQTT) wymaga osobnej decyzji usera — S9 staje i pyta.

## Rollback
`git revert` commitu S3. Baza v4 zostaje w `settings` jako `v=4`; po cofnięciu kodu `BASELINE_VERSION=3` przeliczy ją po restarcie (akceptowalne). Wiersze historii w `tariff_hours` są nieszkodliwe.
