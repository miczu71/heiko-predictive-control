# ROADMAP — jedno źródło prawdy dla taryf (pv_roi_tracker)

Zasada (user, 2026-11-06): wszystkie taryfy — strefy, ceny, szczyty, w tym letnie przesunięcie szczytów G13 —
pochodzą wyłącznie z `pv_roi_tracker`. Heiko niczego nie zgaduje z zegara.

## Stan wyjściowy (0.21.1)
- Pulpit (kafel taryfy) i KPI „udział w szczycie popołudniowym” biorą strefę z `tariff_hours` (feed pv_roi).
- **Odstępstwa od zasady, do usunięcia:**
  1. `kpi.py`: baza z zimy i godziny bez wiersza w `tariff_hours` używają okna 16–21 w dni robocze (`EVENING_PEAK_HOURS`) — zimowe przybliżenie, latem błędne.
  2. `tariff.py` (`fallback_hour`, `PEAK_WINDOWS` G12w) używany w `cycle`, `summaries`, `dhw_cycles`, `attic_runs`, `floor_plan`, `tariff_feed`, `analysis`.
  3. Stare encje G12w: `tariff_state_entity`, `tariff_price_entity` (`cycle.py:179,477`, `live.py` jako zapas, `config.yaml`, `run.sh`, `main.py`).
  4. `default_is_workday` (pn–pt) jako zapas dnia roboczego, gdy brak `binary_sensor.workday`.
- pv_roi: `GET /api/tariff/schedule?hours=1..168&tariff=` liczy tylko od bieżącej godziny (`web.py:1351`); `tariff_module.schedule(start, hours, tariff)` umie dowolny zakres.

## Etapy
| Etap | Zakres | Status |
|---|---|---|
| T0 | Wywiad (`grill-me`): co robi Heiko przy braku pv_roi (UI „brak danych taryfy” vs ostatni znany harmonogram), jak liczyć święta, czy baza z zimy ma być przeliczana przy zmianie taryfy | ZROBIONE 09.11: (1) brak pv_roi = ostatni znany harmonogram + znacznik „nieaktualne od…”, godzina bez wiersza = „brak danych taryfy” (koszt bez wyceny, KPI pomija); (2) G13 ze stref pv_roi (`evening_peak`), święta i lato same; (3) baza KPI przeliczana wg bieżących stref (`BASELINE_VERSION` 4), stara zostaje do udanego przeliczenia. Realizacja: `/config/docs/ROADMAP_heiko_0_22.md` (S2–S5) |
| T1 | pv_roi: parametr `start` (do 168 h wstecz lub dowolny zakres) w `/api/tariff/schedule` albo nowy endpoint historii; testy; wydanie pv_roi | DO ZROBIENIA |
| T2 | Heiko: klient pv_roi (HTTP po wewnętrznym hostname add-onu) + tabela `tariff_hours` zasilana także historią; baza KPI z zimy wg stref z pv_roi (`BASELINE_VERSION` 4), bez `EVENING_PEAK_HOURS` | DO ZROBIENIA |
| T3 | Heiko: usunięcie `fallback_hour`/`PEAK_WINDOWS`, `tariff_state_entity`, `tariff_price_entity`; tam, gdzie brak danych — jawny stan „brak danych taryfy” w UI, nie zgadnięte godziny; testy `test_tariff.py` przepisane | DO ZROBIENIA |
| T4 | Wydanie łączone (T2+T3), weryfikacja na żywo, test letni (przesunięty szczyt G13) na danych z pv_roi | DO ZROBIENIA |

Kroki dokładne, rollback i sekcja Wykonawca powstaną w `docs/PLAN_tariff_single_source_T*.md` po T0.

## Poza głównym wątkiem — otwarte punkty z wydania 0.21.1 (2026-11-06)

Nieblokujące uwagi z `code-review`, `silent-failure-hunter` i `pr-test-analyzer`, plus rzeczy zgłoszone przez usera. Każdy punkt ma przypisany etap albo jest osobnym małym wydaniem.

| # | Punkt | Gdzie | Przypisanie |
|---|---|---|---|
| P1 | **Kolory paska „Doba: koszt ogrzewania”**: strefa „średnia” (`--chart-cold` #c2571a) i „droga” (`--peak` #9a4a2c) mają niemal ten sam odcień (w trybie ciemnym #f29a63 vs #e39a78); pasek ma 6 px, a legenda 10 px kwadraciki. Przyczyna: kolory wzięte z palety wykresów temperatur (`--chart-*`), nie dobrane do kategorii taryfy. Poprawka: osobna paleta stref (skill `impeccable`, weryfikacja Playwrightem w obu motywach), paleta do akceptacji przez usera | `static/app.css:490,507–514`, `static/app.js` (`sourceBar`) | osobne małe wydanie, niezależne od T0–T4 |
| P2 | Nieznana strefa w `tariff_hours` (np. `peak`, zmiana nazwy w pv_roi) jest po cichu liczona jako „poza szczytem” — KPI spada do ~0% i wygląda jak idealne przesunięcie. Dodać `logger.warning` (raz na godzinę) i/lub użyć `is_peak` z feedu jako kontroli spójności | `kpi.py` `_evening_rows` | T2 |
| P3 | Okno 16–21 w dni robocze (baza z zimy i godziny bez wiersza w feedzie) jest błędne latem i w święta; recent KPI i baza mogą mierzyć różne okna | `kpi.py` `EVENING_PEAK_HOURS` | T2 (baza ze stref pv_roi), T3 (usunięcie okna) |
| P4 | Kafel taryfy na Pulpicie wraca do starego sensora G12w, gdy feed jest nieaktualny albo nie ma wiersza dla bieżącej godziny; `tariff_source` mówi wtedy `feed`, a kafel pochodzi z innego źródła. Docelowo: „brak danych taryfy” | `web.py` `live_data` | T3 |
| P5 | Test integracyjny `/api/live`: kafel pokazuje „Szczyt popołudniowy” z ceną z harmonogramu przy `source=feed` oraz zostaje stary odczyt przy `fallback`/braku wiersza; błąd strefy czasowej w `feed.at(...)` | `tests/test_web.py` | T3 |
| P6 | `peak_price` w tytule KPI to ostatnia stawka `evening_peak` z godziny ≤ teraz; przy zmianie taryfy albo braku takich wierszy tytuł może pokazać starą stawkę albo jej nie pokazać. Brak testu `peak_price is None` | `web.py` `api_plan` | T2 |
| P7 | Nieudane przeliczenie bazy KPI (statystyki HA niedostępne przy starcie) kasuje poprzednią bazę, a ponowna próba jest dopiero po restarcie — KPI zostaje „w trakcie liczenia”. Rozważyć zachowanie starej bazy do czasu udanego przeliczenia | `kpi.py` `ensure_peak_baseline` | T2 |
| P8 | Wynik KPI po wydaniu: 7 dni = 0,04% przy bazie z zimy 7,2% (−7,1 p.p.). Sprawdzić na wykresie dziennych cykli, czy pompa naprawdę prawie nie pobiera z sieci w 16–21 (PV/bateria), czy to artefakt mapowania stref | dane produkcyjne | do sprawdzenia przed T2 |
| P9 | Okno wysłane w 0.21.1: dwa miejsca liczą „teraz” w strefie lokalnej tym samym wyrażeniem (`api_g13`, `live_data`) — wyciągnąć helper `_local_now(settings)` | `web.py` | przy okazji T3 |
