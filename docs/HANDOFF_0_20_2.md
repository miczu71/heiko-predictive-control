# Handoff — heiko_predictive_control 0.20.2 (2026-11-03)

Status: **wydanie zakończone i zweryfikowane na żywo.** Nic nie jest w toku.

## Co wydano
Pulpit bez symulowanej „oszczędności”, z kafelką „Zapisy do pompy” i jednym kosztem pompy.
Plan i uzasadnienie: `docs/PLAN_pulpit_0_20_2.md`; zmiany: `CHANGELOG.md` (wpis 0.20.2).

Commity (repo `/config/addons/heiko_predictive_control`, gałąź `main`):
- `0527449` plan
- `46490ae` feat (zmiana UI + usunięcie endpointu `/api/today_summary`)
- `3e77bb1` simplify (pole `pump` w `/api/day_summary`)
- `62c3785` fix po review (odporność na nieczytelny plan testu, „—” zamiast „0,00 zł” przy `error`)
- `4c5eecd` release 0.20.2

Tag `v0.20.2` opublikowany (nie draft). Zainstalowano przez `ha_manage_updates` (`update.heiko_predictive_control_update`, `backup=true`); wywołanie install dało timeout połączenia, ale instalacja doszła do końca (`installed_version=0.20.2`).

## Weryfikacja na żywo (playwright-ha)
- Stopka v0.20.2; konsola bez błędów add-onu.
- 3 kafelki w jednym rzędzie (1440) i 1 kolumna (390×844), bez poziomego scrolla.
- „Pompa dziś” = podłoga + CWU = kafel Doba (2,16 + 1,04 = 3,20 zł); podpis „wczoraj: 6,12 zł”.
- „Zapisy do pompy” = „brak / pompa na własnej krzywej”, bo test #8 ma status `przerwany` (nie błąd kafelki).
- Karta pompy bez „Optymalizator (poglądowo)” i Komfort/Ekonomia; jeden fold „Model i plan (poglądowo)” (+ osobny fold KPI, zostaje).
- Zrzuty: `/home/claude/ha-config/playwright/heiko-0.20.2-desktop.png`, `…-mobile.png`.
- Testy: pełny pytest 703 passed (przed ostatnią poprawką); po niej `tests/test_web.py` 62 passed, `tests/test_day_summary.py` zielone.

## Otwarte (nieblokujące)
1. Kolumny `baseline_cost_today_pln` / `sim_cost_today_*` są nadal zapisywane co cykl bez odbiorcy — kandydat do usunięcia (`cycle.py`, `db.py`, `publisher.py`; osobny etap, schemat DB).
2. Licznik „propozycje Doradcy: N” liczy surowe `STATUS_PENDING` bez `expires`; lepiej `advisor.count_pending(conn)` spójne z `list_proposals`.
3. Luki testów `_running_test_view` (web.py): krok P0 `value/stop`, „ostatni krok wykonany” (`step == len`), nieznany `kind`. Luźna asercja `"brak" in html`.
4. `loadYesterdayPumpCost` (static/day.js) nie pokazuje błędu w UI przy nieudanym fetchu; `setPumpCost` nie odświeża się, gdy kafel Doba stoi na „wczoraj”.
5. Decyzja usera: wyłącznik `heiko_enabled` zostaje bez zmian (blokuje tylko start testów, nie przerywa trwającego).

## Wskazówki
- Testy: tylko w venv ze scratchpada sesji (`…/scratchpad/venv-w1/bin/python -m pytest -q -p no:cacheprovider`, w katalogu `heiko_predictive_control/heiko_predictive_control`); zwykły `python -m pytest` w kontenerze nie działa. Pełny przebieg ~2–4 min. Jeśli venv zniknął, odtwórz go z `requirements.txt` add-onu.
- Repo PUBLIC: dane neutralne w docs/changelogu, hook pre-commit nieomijalny; bez commitów w oknie 02:45–03:15.
- Pakiet Pythona: `heiko_predictive_control/heiko_predictive_control/heiko_predictive_control/`.

## Sugerowane skille
- `tdd` + `addon-conventions` — przy domykaniu luk testów (pkt 3) i zmianach kodu add-onu.
- `simplify` → `code-review` → `release` — przy kolejnym wydaniu (np. sprzątanie kolumn symulacji, pkt 1; to zmiana schematu, więc najpierw plan i `grill-me`).
- `verification-before-completion` — przed ogłoszeniem sukcesu.
