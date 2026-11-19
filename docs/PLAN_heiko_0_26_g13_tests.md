# Heiko 0.26.0: testy przy włączonym Szczycie G13 (blokada tylko przy realnym konflikcie)

## Context
Przy włączonym `g13_peak_enabled` Start każdego testu z `writes` jest blokowany (`experiments.py:150`),
a schemat odrzuca `coexist:["g13_peak"]` przy dowolnych `writes` (`testkit/schema.py:307`, „dwóch pisarzy krzywej”).
G13 zapisuje wyłącznie przesunięcie krzywej, w oknie `[preheat_from, end)` z `g13_peak.window_from_feed`.
Analiza wykazała:
1. Testy P0 / slot52 / CWU nie piszą krzywej, więc konfliktu zapisów nie ma. Blokada jest zbędna.
2. Test `zapis` (krzywa, 10 min) koliduje z G13 tylko wtedy, gdy zachodzi na okno G13 albo G13 trzyma przesunięcie
   różne od bazowego (wtedy test wziąłby złą bazę).

Decyzja usera (punkt 1): **opt-in przez `coexist`**. Start nie jest blokowany. Domyślnie G13 pauzuje na czas testu,
więc `p0_ab` zachowuje czysty pomiar. Definicja z `coexist:["g13_peak"]` (dozwolone dla celów innych niż krzywa)
pozwala G13 działać równolegle.

Zakres: tylko G13. `tail_cut` i tryb urlopowy zostają bez zmian (dalej używają `writing_test`).

## Wynik — kto/co/kiedy
- **User:** test P0/slot52/CWU startuje bez wyłączania G13. `zapis` startuje rano albo wieczorem, gdy G13 nic nie robi.
  `bezwladnosc` dalej blokuje (okno `zone:peak` = szczyt), ale z nowym komunikatem o oknie.
- **Automatycznie:** G13 pauzuje podczas testu z zapisami, chyba że cel ≠ krzywa i definicja ma `coexist:["g13_peak"]`.
- **Sukces:** testy z sekcji Weryfikacja zielone; na żywo karta `p0_ab` przy włączonym G13 nie ma blokady,
  a `zapis` o 14:00 w dzień roboczy ma blokadę z godzinami okna.

## Zmiany (repo `/config/addons/heiko_predictive_control`, moduły w `heiko_predictive_control/heiko_predictive_control/heiko_predictive_control/`)

### A. `experiments.py`
1. Z łańcucha `elif` w `_plan_def` usunąć gałąź G13 (l. 150–151). W jej miejscu zostawić tylko przypadek testu
   **bez** `writes`: `g13_peak_enabled and not writes and "g13_peak" not in coexist` → dotychczasowy komunikat
   „wyłącz tryb Szczyt G13 (Opcje), żeby uruchomić test” (nakładki bez opt-in, jak dziś).
2. Cele `p0`, `slot52`, `dhw`: brak blokady G13.
3. Cel `curve`, po wyliczeniu `steps` (gdy `reason is None`): nowa funkcja `_g13_curve_conflict(steps, settings, shift, feed, g13_state)`:
   - nie działa, gdy `not g13_exec.active(settings)` (wymaga `g13_peak_enabled` i `heiko_enabled`; późny import);
   - **trzymanie:** `g13_state.get("base") is not None` i `shift` różne od bazy o ≥0,05 → blokada
     „Szczyt G13 trzyma teraz przesunięcie {shift:g} (bazowe {base:g}) — poczekaj na powrót albo wyłącz tryb (Opcje)”;
   - **okno:** przedział `[pierwszy at, ostatni at + CONFIRM_MIN]`; dla każdej doby, której dotyczy:
     `w = g13_peak.window_from_feed(feed, doba)` (`feed is None` albo `w is None` → blokada
     „nie znam okna Szczytu G13 na {dd.mm} (brak harmonogramu taryfy) — wyłącz tryb (Opcje)”), `w.empty` → OK,
     przecięcie z `[preheat_from:00, end:00)` → blokada
     „test zachodzi na okno Szczytu G13 ({p:02d}:00–{e:02d}:00) — uruchom go poza oknem albo wyłącz tryb (Opcje)”.
4. `_plan_def` dostaje opcjonalny argument `g13_state: dict | None = None`. Wołający z `conn` (`overview` l. 334,
   `start` l. 364, `testkit/defs.py:143` preview) podają `dbm.get_setting(conn, g13_exec.STATE_KEY) or {}`.
   `_plan` (l. 194, legacy) zostaje bez stanu.
5. Nowa funkcja `g13_yield_test(conn)`: zwraca `writing_test(conn)`, z wyjątkiem testu, którego zamrożona definicja
   (`engine.frozen_def`) ma cel ≠ `curve` (`engine.writes_target`) i `"g13_peak" in coexist`. Wtedy zwraca None.

### B. `g13_exec.py`
- l. 123 i 143: `experiments.writing_test(conn)` → `experiments.g13_yield_test(conn)`. Docstring (l. 4): dopisać opt-in.
- Sprawdzić (bez zmian, jeśli się zgadza), że wykrywanie „ręcznej zmiany” G13 filtruje `pump_writes` po encji
  przesunięcia (`_SHIFT_LIKE`), więc zapisy P0/slot52/CWU testu go nie mylą. Sprawdzić też, że `experiments.tick`
  testu P0/slot52/CWU nie traktuje zapisu przesunięcia przez G13 jako ręcznej zmiany własnej nastawy.

### C. `testkit/schema.py` (l. 307)
- Błąd tylko, gdy `writes` zawiera `curve_shift`: „coexist: g13_peak nie łączy się z zapisem przesunięcia krzywej
  (dwóch pisarzy krzywej)”. `writes` P0/slot52/dhw + `coexist` = poprawne.

### D. Teksty
- `grep -rn coexist` w `DOCS.md`, `README.md`, `templates/`, `static/` (edytor definicji). Tam, gdzie opisano `coexist`,
  dopisać jedno zdanie: „przy zapisie P0/slot52/CWU `coexist:["g13_peak"]` pozwala G13 działać w trakcie testu”.
- `CHANGELOG.md` → 0.26.0 (w kroku `release`).

## Testy (TDD, najpierw RED) — `heiko_predictive_control/tests/`
- `test_testkit_schema.py`: `writes` p0 + `coexist` → `[]`; `writes` curve + `coexist` → błąd `coexist`
  (istniejący test l. 204 sprawdzić/zostawić dla krzywej).
- `test_testkit_engine.py` (G13 włączony, feed ze szczytem 16–21 → okno 13–21):
  - `p0_ab` startuje (aktualizacja asercji l. 568, jeśli dotyczy P0);
  - `zapis` o 10:00 startuje; o 12:55 (10 min zachodzi na 13:00) zablokowany; o 14:00 zablokowany;
  - `zapis` o 21:30, stan G13 `base=0`, shift 0 → startuje; shift −3 → blokada „trzyma”;
  - `zapis` bez feedu → blokada „nie znam okna”; `bezwladnosc` → blokada okna;
  - nakładka bez `coexist` → blokada jak dziś (l. 337 bez zmian); karta w `overview` (l. 659) wg nowego komunikatu.
- `test_testkit_defs.py`: preview `zapis` w oknie → `blocked` z godzinami (l. 196 bez zmian).
- `test_g13_exec.py`: w trakcie testu P0 z `coexist` G13 zapisuje szczyt; P0 bez `coexist` → pauza; test krzywej → pauza.

Komenda: `cd <WORKTREE>/heiko_predictive_control && PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pyyaml pytest -q -p no:cacheprovider tests/test_testkit_schema.py tests/test_testkit_engine.py tests/test_testkit_defs.py tests/test_g13_exec.py tests/test_experiments.py`
(pełny zestaw robię sam po `wt land`).

## Wykonawca (paczka „ryzyko” — zapisy do pompy → `sonnet-worker`, jedna paczka W1)
- Pliki: `experiments.py`, `g13_exec.py`, `testkit/schema.py`, `testkit/defs.py`, testy jak wyżej, ewentualnie DOCS/hint edytora.
- Skille: `addon-conventions`, `tdd`.
- DONE: testy z listy zielone; `grep -n "g13_peak_enabled\") and (writes" experiments.py` pusty; `g13_exec` nie woła
  `writing_test`; schemat przyjmuje p0+coexist i odrzuca curve+coexist.
- NIE RÓB: `config.yaml` (wersja), `CHANGELOG.md`, `tail_cut.py`, `vacation*.py`, `g13_peak.py` (tylko import
  `window_from_feed`), nowych encji/zapisów, zmian `_write`/allowlisty, commitów.
- Po workerze: `claimcheck` → `verifier` (BASELINE = `git status --short` sprzed delegacji).

## Po etapie (jedyny = ostatni)
Kopia planu do `docs/PLAN_heiko_0_26_g13_tests.md` (pierwszy krok) → worker → verifier → pełny pytest →
`simplify` → `code-review` + `silent-failure-hunter` + `pr-test-analyzer` + security review (`184cd1a..HEAD`) →
`release` 0.26.0 → weryfikacja na żywo (Playwright przez ingress: karta testów przy włączonym G13).

## Rollback
`git revert` commitu etapu + wydanie 0.26.1 (albo reinstall 0.25.2). Brak migracji bazy i nowych opcji.
