# Nowe testy i miara cyklu CWU — roadmapa

Decyzja 2026-11-28: nie dublować istniejących definicji z bazy add-onu (`preheat_ab`, `ah_delay_ab`,
`night_curve_ab`, `dhw_setpoint_ab`, `g13_peak_depth_ab`, `tail_margin_ab`); dodać miarę CWU i warianty.
`ogon_ab` przerwany za zgodą (2026-11-28 07:03), nastawy bez zmian.

- [x] E1 — warianty jako dane: `curve_day_m1_ab`, `preheat_2_ab` (`docs/testdefs/`, zapisane przez `/api/test-defs`).
  Rollback: `DELETE /api/test-defs/<key>`.
- [ ] E2 — miara `dhw_cycle`, wydanie 0.28.0, potem definicje. Rollback: 0.27.0 z Supervisora, `git revert`.

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
