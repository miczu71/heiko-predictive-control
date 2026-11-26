# Test A/B skuteczności „ogona grzania” (heiko_predictive_control)

## Kontekst
W add-onie są tylko testy jednostkowe mechaniki `tail_cut` (`tests/test_tail_cut*.py`). Nie ma testu, który
sprawdza, czy odcinanie ogona oszczędza energię i nie obniża komfortu. Testkit ma już prawie wszystko:
nakładki (`overrides`) działają z `tail_cut` (`main.py:376`, test bez `writes` nie blokuje ogona), jest projekt
`alternate`, miary `kwh_day`/`floor_cost` i kowariata temperatury zewnętrznej. Brakuje: wyłączania ogona
nakładką, korekty kWh na pogodę, miary komfortu z całej doby, licznika odcięć i gotowej definicji.

## Ustalenia z wywiadu
- **Kto korzysta:** użytkownik uruchamia test „Ogon grzania A/B” w zakładce Testy (2 kliknięcia) i czyta tabelę wariantów.
- **Automatycznie:** 14 dób (00:00→00:00), na przemian A=ON (bieżące opcje `tail_*`), B=OFF. G13 działa (`coexist: ["g13_peak"]`).
- **Werdykt:** miara główna `kwh_hdd` (kWh/stopniodzień, baza 14 °C, doby z HDD < 1 pomijane), niższa lepsza,
  `min_gain` 0,08, `weather_band_c` 3,0, `min_n` 5. Obok, tylko do wglądu: `kwh_day` (surowe), `floor_cost` 00:00→00:00 (zł/dobę),
  `room_day` (średnia i minimum pokoju z doby), `tail_cuts` (odcięcia/dobę), `safety_trips`. **Bez strażnika komfortu.**
- **Za zgodą:** start testu tylko przez usera; wydanie add-onu po ostatnim etapie wg CLAUDE.md (bez pytania).
- **Sukces:** definicja przechodzi `schema.validate`, w dobach B ogon nie tnie (`tail_cuts` = 0), w dobach A tnie, tabela pokazuje wszystkie kolumny.

## Etapy (roadmapa `docs/ROADMAP_tail_ab.md`, plan etapu `docs/PLAN_tail_ab_E1.md`)
Krok 0: skopiować ten plan do `/config/addons/heiko_predictive_control/docs/` (ROADMAP + PLAN E1).

### E1 — kod: nakładka bool, miary, definicja (jedna paczka, `haiku-worker`, `wt new`)
PKG = `heiko_predictive_control/heiko_predictive_control/`

**Wykonawca**
1. `PKG/testkit/params.py`: `tail_cut_enabled` w `OVERRIDABLE` z wartością `None` (= bool, bez zakresu).
   `MEASURES` += `kwh_hdd`, `room_day`, `tail_cuts`.
2. `PKG/testkit/schema.py` (walidacja `overrides`, ~:142-152): gdy `OVERRIDABLE[name] is None` → wymagaj `bool`,
   w przeciwnym razie dotychczasowa ścieżka liczbowa (bool tam dalej odrzucany).
3. `PKG/testkit/overlay.py`: sprawdzić, że `effective_settings` przenosi bool bez rzutowania (zmiana tylko, jeśli rzutuje).
4. `PKG/testkit/measures.py`:
   - `kwh_hdd(ctx, days)`: dla każdej doby `experiments._day_kwh` / HDD z `_avg_outdoor` (`floor_learn.HEATING_MAX_OUTDOOR_C - out`,
     pomijaj HDD None/< 1, jak `floor_cost per="hdd"`); zwrot `{value, n, per_day}` jak `floor_cost`.
   - `room_day(ctx, days)`: `cycles.min_room_c` (`loop='heiko'`) w [start, end) doby; per_day `{day, mean, min}`;
     `value` = średnia dobowych średnich, `min` = najniższe minimum.
   - `tail_cuts(ctx, days)`: wiersze `pump_writes` z `entity_id = experiments.MODE_WRITE_ID`, `new = 3`, `ok = 1`,
     `confirmed_at` ≠ `NO_CONFIRMATION` w dobie; `value` = średnia odcięć/dobę, per_day liczby.
   - wpisy w `CATALOG` jako `Measure("arm", …)`.
5. `PKG/testkit/engine.py` `MEASURE_TEXT`: opisy trzech miar po polsku.
6. `PKG/testkit/builtin.py`: definicja `ogon_ab` (tytuł/opis wpisane w miejscu, bez `ex.KINDS`):
   `requires {heiko_enabled: true}`, `coexist ["g13_peak"]`, `days {filter all, count 14}`, `design alternate`,
   `order [A,B]`, `A.overrides {tail_cut_enabled: true}`, `B.overrides {tail_cut_enabled: false}`,
   `measures [kwh_hdd, kwh_day, {name: floor_cost, window: "00:00→00:00"}, room_day, tail_cuts, safety_trips]`,
   `verdict {primary kwh_hdd, better lower, min_gain 0.08, weather_band_c 3.0, min_n 5}`.
7. Testy (TDD, najpierw czerwone): `tests/test_testkit_schema.py` (bool OK, liczba dla bool → błąd, bool dla liczbowej → błąd),
   `tests/test_testkit_overlay.py` (B wyłącza `tail_cut.active`), `tests/test_testkit_measures.py` (3 miary na bazie w pamięci,
   HDD < 1 pomijane, retry bez potwierdzenia nie liczy się), `tests/test_testkit_builtin.py` (`ogon_ab` waliduje się,
   harmonogram A,B,A,B… 14 dób).
- **Komenda testu:** z `/config/addons/heiko_predictive_control/heiko_predictive_control`:
  `/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider tests/test_testkit_*.py tests/test_tail_cut.py`,
  potem pełny `timeout 580` (podać bilans względem baseline).
- **DONE:** nowe testy zielone, pełny pytest 0 failed; `ogon_ab` widoczny w `/api/test-defs`; zero nowych `call_service`
  (`tests/test_no_pump_writes.py` zielony).
- **NIE RÓB:** `config.yaml` (wersja), `CHANGELOG.md`, `tail_cut.py`, `g13_*.py`, `experiments.py`, `verdict.py`, `static/*`, commit.

Po workerze: `claimcheck` → `verifier` (BASELINE `git status --short`). Rollback: `wt drop` / `git revert` commita E1.

E1 można zrobić od razu: tylko worktree i commit w repo, bez wydania. Add-on na żywo się nie zmienia,
więc trwający test (do końca soboty 2026-11-28) biegnie dalej.

### E2 — wydanie 0.27.0 (ostatni etap) — **dopiero w niedzielę 2026-11-29**
Bramka przed startem: na żywo `GET /api/tests` (lub zakładka Testy) — żaden test nie ma statusu „trwa”.
Jeśli trwa → stop i pytam (aktualizacja add-onu restartuje go, a test z `writes` mógłby zostać przerwany).
`simplify` → `code-review` + równolegle `silent-failure-hunter`, `pr-test-analyzer`, security review diffu →
CHANGELOG + bump `config.yaml` → `release` → na żywo: `/api/test-defs` zawiera `ogon_ab`, podgląd testu
pokazuje 14 dób A/B. Start testu robi user. Rollback: poprzednia wersja add-onu (0.26.0) z Supervisora.

## Ryzyka
- Przełączenie B o północy przy `forced` → `tail_cut` od razu wraca do Auto (zamierzone, jak wyłączenie opcji).
- Ogon trwa kilkadziesiąt minut na dobę: wynik może wyjść „brak zwycięzcy” — to też odpowiedź.
- `daily_summary` energii obejmuje też CWU; obie doby w tym samym stopniu.
