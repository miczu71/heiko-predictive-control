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
| T0 | Wywiad (`grill-me`): co robi Heiko przy braku pv_roi (UI „brak danych taryfy” vs ostatni znany harmonogram), jak liczyć święta, czy baza z zimy ma być przeliczana przy zmianie taryfy | DO ZROBIENIA |
| T1 | pv_roi: parametr `start` (do 168 h wstecz lub dowolny zakres) w `/api/tariff/schedule` albo nowy endpoint historii; testy; wydanie pv_roi | DO ZROBIENIA |
| T2 | Heiko: klient pv_roi (HTTP po wewnętrznym hostname add-onu) + tabela `tariff_hours` zasilana także historią; baza KPI z zimy wg stref z pv_roi (`BASELINE_VERSION` 4), bez `EVENING_PEAK_HOURS` | DO ZROBIENIA |
| T3 | Heiko: usunięcie `fallback_hour`/`PEAK_WINDOWS`, `tariff_state_entity`, `tariff_price_entity`; tam, gdzie brak danych — jawny stan „brak danych taryfy” w UI, nie zgadnięte godziny; testy `test_tariff.py` przepisane | DO ZROBIENIA |
| T4 | Wydanie łączone (T2+T3), weryfikacja na żywo, test letni (przesunięty szczyt G13) na danych z pv_roi | DO ZROBIENIA |

Kroki dokładne, rollback i sekcja Wykonawca powstaną w `docs/PLAN_tariff_single_source_T*.md` po T0.
