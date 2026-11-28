# Nowe testy i miara cyklu CWU — roadmapa

Decyzja 2026-11-28: nie dublować istniejących definicji z bazy add-onu (`preheat_ab`, `ah_delay_ab`,
`night_curve_ab`, `dhw_setpoint_ab`, `g13_peak_depth_ab`, `tail_margin_ab`); dodać miarę CWU i warianty.
`ogon_ab` przerwany za zgodą (2026-11-28 07:03), nastawy bez zmian.

- [x] E1 — warianty jako dane: `curve_day_m1_ab`, `preheat_2_ab` (`docs/testdefs/`, zapisane przez `/api/test-defs`).
  Rollback: `DELETE /api/test-defs/<key>`.
- [x] E2 — miara `dhw_cycle`, wydania 0.28.0 i 0.28.1, definicje zapisane (2026-11-28).

## E2 — kroki
1. `testkit/measures.py`: `dhw_cycle(ctx, days)` z tabeli `dhw_cycles` (JSON `data`): cykle `complete`, tylko
   typ zegarowy z `dhw_cycles._classify` (bez kąpieli i PV), `start` w dobie wariantu. Per doba średnie
   `kwh_total` (= `value`), `duration_min`, `ah_kwh`, `ah_min`, `cost_pln`. Doba bez cyklu pomijana; brak danych → None.
   Sprawdzić strefę czasową `start` w tabeli względem granic doby wariantu. Wpis w `CATALOG` jako `Measure("arm", …)`.
2. `testkit/params.py`: `dhw_cycle` w `MEASURES`.
3. Testy w `tests/test_testkit_measures.py`; wersja 0.28.0 w `config.yaml`, CHANGELOG.
4. Wydanie: simplify → code-review (+ silent-failure-hunter, pr-test-analyzer, security) → release.
   Przed wydaniem sprawdzić, że żaden test nie trwa (restart przerywa test).
5. Po wydaniu przez API: nowa definicja `ah_delay_m5_ab` (slot52 base vs base−5, miary `dhw_cycle`, `kwh_day`,
   `min_room_at` 06:00); `PUT` dla `ah_delay_ab` i `dhw_setpoint_ab` — dołożyć `dhw_cycle` jako miarę główną.
   JSON wszystkich trzech do `docs/testdefs/` (poprzednie wersje zapisać przed `PUT`).

## Stan E2 (2026-11-28, zakończony)

- Na żywo 0.28.1. `dhw_cycle` liczy poranne starty o 4/6/8 (−1…+5 min); typ „zegar” (04/08 ±60 s) nie miał cykli od 24.10.
- Zapisane przez API: `ah_delay_m5_ab` (nowa), `ah_delay_ab` i `dhw_setpoint_ab` (`dhw_cycle` główna, `min_gain` 0,15 kWh).
  JSON w `docs/testdefs/` (`*.prev.json` = wersje sprzed PUT; cofnięcie: PUT tych plików, `DELETE /api/test-defs/ah_delay_m5_ab`).
- Moc testów: doba z porannym cyklem 26/32, średnio 1,44 kWh, odchylenie 0,33. Przy 8 dobach (4 na wariant, `min_n` 4)
  jedna doba bez cyklu daje „za mało danych”, a różnica 0,15 kWh ginie w szumie. Do rozważenia przed Startem: więcej dób.

Znane, niepoprawione:
- Podgląd w edytorze definicji (`testkit/defs.preview`) nie przekazuje bieżącej nastawy do `_plan_def`, więc dla `slot52`
  i `dhw` zawsze pokazuje „nie mogę odczytać…”. Starsze niż E2.
- Wynik liczy się bez doby, gdy nocny zapis się nie uda (także wyścig z backfillem po restarcie).
- Test z zapisami i `floor_cost` bez `dhw_cycle` nie czeka na przebiegi 00:30.
- Przy `floor_cost` 13:00 w teście z zapisami czekanie trwa do ~25 h i blokuje G13.
