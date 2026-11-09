# PLAN S7 — urlop E4: panel i powiadomienia (Heiko 0.23.0, ryzyko)

Źródło: `/config/docs/ROADMAP_heiko_0_23.md` (S7). Wersji NIE podbijać (S8). Wzorzec: `g13_view.py` + `static/g13.js` + kafel G13 w `templates/dashboard.html`.

## Kontrakt

**`vacation_view.py`** (nowy, tylko odczyt): `build(conn, settings, now) -> dict`
- `status` (kolejność): `off` (`vacation_enabled` fałsz) · `scheduled` (włączony, `today < vacation_from`) · `expired` (`today > vacation_to`) · `blocked` (w oknie dat, ale `heiko_enabled` fałsz) · `tripped` (stan `vacation_state.tripped`) · `suspended` (`suspended_day == today`) · `active`.
- Etykiety `status_label`: „wyłączony”, „zaplanowany od DD.MM”, „zakończony”, „wstrzymany: sterowanie Heiko wyłączone”, „bezpiecznik — bazowe do ocieplenia”, „wstrzymany do jutra”, „włączony”.
- Pola: `enabled`, `from`, `to` (ISO lub None), `drop` (1–3), `min_room_c`, `dhw` (cel CWU), `with_away`, `peak_pressing` (bool: `g13_peak_enabled`), `summary` (jedna linia kompaktowa, np. „Urlop do 15.11 · krzywa −3 · CWU 20”; bez daty „do” → „Urlop · krzywa −2 · CWU 20”; dla `off` pusty), `dhw_warn` (z `vacation_state.dhw_warn`: „brak”/„min”/None), `trip_room` (gdy `tripped`), `trips_30d` (liczba wpisów `trips` z 30 dni).
- Wartości liczbowe jak w `vacation_exec` (`_config`, `dhw_target`) — nie duplikuj domyślnych.

**`web.py`**: `GET /api/vacation` → `vacation_view.build` (wzorzec `api_g13`, `conn.close()` w `finally`). `SETTING_RANGES`: `vacation_curve_drop` (1, 3), `vacation_min_room_c` (10, 25), `vacation_dhw_setpoint` (16, 60). `_validate_settings`: `vacation_from`/`vacation_to` muszą być `""`/None albo ISO `YYYY-MM-DD` (`date.fromisoformat`); gdy oba podane, `from <= to`; błędy po polsku w stylu istniejących. Wartości całkowite dla drop i dhw (nieułamkowe).

**Powiadomienia (`vacation_exec.py`)**: nowy stan `announced` (ISO daty startu).
- Start: w gałęzi aktywnej, po sprawdzeniu `running_test` i `dhw`, gdy `not st.get("announced")` → `notify.family`, tytuł `TITLE`, treść „Tryb urlopowy włączony{: do DD.MM}: krzywa −N, CWU M.” (bez wyciekania danych prywatnych), `st["announced"] = today`, zapis stanu od razu.
- Koniec: gdy `not vacation.is_active(settings, today)` (NIE `active()` — wyłączenie `heiko_enabled` nie jest końcem urlopu) i `st.get("announced")` → `notify.family` „Tryb urlopowy zakończony.”, usuń `announced`. Bez `announced` brak powiadomienia.
- Bezpiecznik: dotychczasowe powiadomienie `bezpiecznik` idzie na `notify.kacper`. Pozostałe (odrzucony zapis, wstrzymanie) bez zmian (`advisor_notify_service`).
- `notify` wstrzykiwane `(service, title, message)` jak dziś; pomocnik `_notify_to(service, message, notify)` w `vacation_exec.py` (`(notify or ha_client.notify)(service, TITLE, message)`).

**UI**
- `templates/dashboard.html`: trzeci `mode-tile` „Urlop” (id `vacation-tile`, badge `vacation-badge`, `vacation-summary`, `details.fold` z `vacation-body` i linkiem do Opcji), skrypt `static/vacation.js?v={{ version }}` po `tail_cut.js`.
- `static/vacation.js`: wzorzec `g13.js` (`HPC.getJSON("/api/vacation")`, odświeżanie 5 min, błąd → badge „błąd”). Summary = `d.summary` albo tekst statusu; w szczegółach: okno dat, obniżka, CWU (z ostrzeżeniem, gdy `dhw_warn`), min. pokój, Away tak/nie, docisk szczytu (tak/nie wg `peak_pressing`), bezpiecznik 30 dni. Badge `ok` dla `active`, `warn` dla `blocked`/`suspended`/`tripped`, bez klasy dla reszty.
- `static/app.css`: `.mode-strip` 3 kolumny (`repeat(3, minmax(0, 1fr))`), poniżej 900 px jedna kolumna (obecny próg 640 → 900 dla trzech kafli). Bez nowych kolorów (tokeny istniejące).
- `templates/options.html`: fieldset „Tryb urlopowy” po „Szczyt G13”: `vacation_enabled` (checkbox), `vacation_from`/`vacation_to` (`type=date`), `vacation_curve_drop` (1–3, domyślnie 2), `vacation_min_room_c` (domyślnie 16), `vacation_dhw_setpoint` (16–60, domyślnie 20), `vacation_with_away` (checkbox). Hint po polsku: całodobowa obniżka krzywej, szczyt −3 (przy włączonym trybie Szczyt G13), CWU na czas urlopu i 48 po nim, bezpiecznik, brak anty-legionelli. `static/options.js` bez zmian (daty idą jako string, puste = `""`).

## Wykonawca (paczka W1, ryzyko → `sonnet-worker`, `wt new /config/addons/heiko_predictive_control heiko-0_23-S7`)

Repo: `/config/addons/heiko_predictive_control`; moduły `heiko_predictive_control/heiko_predictive_control/heiko_predictive_control/`, testy `.../tests/`. Skille `addon-conventions`, `tdd` (testy najpierw, RED). Przeczytaj najpierw: `g13_view.py`, `vacation_exec.py`, `tests/test_g13_view.py`, `tests/test_vacation_exec.py`, `tests/test_web.py`, `static/g13.js`, fragmenty `web.py` (`SETTING_RANGES`, `_validate_settings`, `api_g13`).

| Plik | Zmiana |
|---|---|
| `tests/test_vacation_view.py` (nowy) | każdy status (off/scheduled/expired/blocked/tripped/suspended/active), `summary` z datą i bez, `dhw_warn`, `trips_30d` |
| `tests/test_web.py` | `GET /api/vacation`; walidacja: drop 0/4, dhw 15/61, min_room 9/26, data nie-ISO, `from > to`, poprawne wartości i puste daty przechodzą |
| `tests/test_vacation_exec.py` | start → jedno powiadomienie `notify.family` (drugi tick bez); koniec po `vacation_to` → jedno `notify.family`; wyłączenie `heiko_enabled` w trakcie urlopu NIE daje „zakończony”; bezpiecznik → `notify.kacper`; test w toku nie ogłasza startu |
| `vacation_view.py`, `web.py`, `vacation_exec.py` | wg kontraktu |
| `templates/dashboard.html`, `templates/options.html`, `static/vacation.js`, `static/app.css` | wg kontraktu (HTML/CSS/JS bez zależności zewnętrznych) |

Test: `cd <WORKTREE>/heiko_predictive_control && PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pyyaml pytest -q -p no:cacheprovider tests/test_vacation.py tests/test_vacation_exec.py tests/test_vacation_view.py tests/test_web.py tests/test_g13_view.py` (pełny zestaw robię sam po `wt land`, w tle).

DONE: nowe testy zielone (RED przed implementacją), istniejące asercje nieosłabione, `git status` w worktree = tylko pliki z tabeli, zero nowych zależności.

NIE RÓB: `config.yaml`, `attic.py`, `cycle.py`, `experiments.py`, `g13_*.py`, `catalog.py`, zmian logiki `vacation.py`, nowych zapisów do pompy, podbijania wersji, CHANGELOG, commitów, ikon/emoji (tylko tekst/SVG istniejące).

## Weryfikacja UI (ja, po `wt land`)
Lokalny `create_app` na tymczasowej bazie ze stanem urlopu, Playwright 390 i 1280 px, motyw jasny i ciemny, `browser_console_messages` czyste; zrzuty do `/config/playwright/`. Skill `impeccable` do przeglądu kafla i fieldsetu.

## Rollback
`wt drop` przed landem; po landzie `git revert`. Urlop domyślnie wyłączony — nowy kafel pokazuje „wyłączony”, brak zmiany zachowania.
