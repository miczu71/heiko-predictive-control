# PLAN S7 — E2: opcje, kafel „Ogon grzania”, walidacja ustawień (Heiko 0.22.0)

Źródła: `/config/docs/ROADMAP_heiko_0_22.md` (S7), `ROADMAP_heiko_tail_cut.md` (E2). Watchdog HA (`set_mode auto`) robię sam przez ha-mcp — poza kontraktem workera.

## Decyzje / odstępstwa od ROADMAP
- Opcje `tail_cut_*` tylko w bazie (`settings`) + formularz Opcji + `POST /api/settings`, **tak jak G13** (`g13_peak_*` też nie ma w `config.yaml`; Supervisor nie zasiewa bazy). Bez zmian w `config.yaml`/`run.sh`/`main.py` → brak ryzyka dla startu add-onu.
- „Statystyki” = sekcja w kaflu na Pulpicie (odcięcia dziś / 7 dni / łącznie z `cuts`, ostatnie odcięcie, trwający ogon w minutach). **Szacunek kWh pomijam**: brak kontrfaktu (nie wiemy, jak długo ogon trwałby bez odcięcia) — liczba z sufitu byłaby gorsza niż jej brak.
- Walidacja serwerowa `POST /api/settings` (NIEBLOKUJĄCE z S1): wartości spoza zakresu dla kluczy `g13_peak_preheat_shift`/`g13_peak_peak_shift` (−3…3) i `tail_*` → HTTP 400 z listą błędów, **nic nie zapisane** (all-or-nothing); inne klucze bez zmian.

## Kontrakt
**`PKG/tail_cut_view.py`** (nowy): `build(conn, settings, now) -> dict`:
`status` ∈ `off` (opcja wyłączona) / `blocked` (`tail_cut_enabled` ale `heiko_enabled` fałsz) / `holding` (`st.forced`) / `pause` (`now < pause_until`) / `watching` (reszta); `status_label` po polsku; `margin_k, delay_min, hold_min, min_room_c, pause_min` (z opcji z domyślnymi z `tail_cut._num`); `tail_minutes` (minuty od `tail_since` albo `None`); `cuts_today`, `cuts_7d`, `cuts_total` (z `st["cuts"]`, iso naiwne lokalne; porównanie po dacie), `last_cut` (iso | None), `last` (`st["last"]`). Czysty odczyt `tail_cut.STATE_KEY`.
**`PKG/web.py`**: `GET /api/tail_cut` (wzór `/api/g13`, `_local_now`); walidacja w `api_set_settings` (funkcja `_validate_settings(payload) -> list[str]` w `web.py`, zakresy w stałej słownikowej): `g13_peak_*_shift` −3…3; `tail_margin_k` 0,2…5; `tail_delay_min` 10…240; `tail_min_pause_min` 5…240; `tail_min_room_c` 15…26; `tail_dhw_hold_min` 1…10; wartości nieliczbowe (poza `None`/pustym dla kluczy `tail_*`) → błąd. `tail_cut_enabled` bez walidacji.
**`templates/options.html`**: `<fieldset>` „Ogon grzania” po fieldsetcie G13: checkbox `tail_cut_enabled` (domyślnie odznaczony), 5 pól number (`tail_margin_k` step 0.1 domyślnie 1.0, `tail_delay_min` 45, `tail_min_pause_min` 30, `tail_min_room_c` step 0.1 20.5, `tail_dhw_hold_min` 2) z min/max jak walidacja; hint po polsku: co robi (przełącza Auto→CWU na `hold` min, gdy Tc stoi ≥ nastawa+margines przez `delay` min), że wymaga „Sterowanie aktywne (Heiko)”, wyłączone domyślnie, wyłączenie/test/zimny pokój natychmiast przywraca Auto.
**`templates/dashboard.html` + `static/tail_cut.js`** (nowy, wzór `g13.js`; `<script>` obok g13.js): kafel `tail-cut-tile` po kaflu G13: badge ze statusem, linia opisu stanu (off: „Wyłączone — pompa sama kończy cykl.”; holding: „Trzymam CWU od hh:mm, powrót do Auto po N min.”; pause: „Przerwa do hh:mm.”; watching: tail_minutes? „Ogon trwa N min (próg M min).” : „Czekam na ogon.”), tabela Odcięcia: Dziś / 7 dni / Łącznie, linia „Ostatnie odcięcie: …”, link do Opcji. Odświeżanie 60 s (`!document.hidden`), błąd → `notice-inline`. Styl z istniejących klas (`day-tile`, `day-table`, `badge`, `sub`); nowego CSS tylko gdy konieczne (`static/app.css`).
**Testy**: `tests/test_tail_cut_view.py` (statusy off/blocked/watching/holding/pause, liczniki cięć dziś/7 dni/łącznie, `tail_minutes`), `tests/test_web.py` (`/api/tail_cut` 200 + klucze; opcje w `/options` zawierają `name="tail_cut_enabled"` i 5 pól; POST `g13_peak_peak_shift=-4` → 400 i nic nie zapisane; POST poprawnych `tail_*` → 200; POST nieznanych kluczy bez zmian jak dotąd; Pulpit zawiera `id="tail-cut-tile"`).

## Wykonawca (jedna paczka W1, haiku-worker; potem `claimcheck` + `verifier`)
Repo: `/config/addons/heiko_predictive_control`, kod `heiko_predictive_control/heiko_predictive_control/` (`PKG`), testy `heiko_predictive_control/tests/`. Skille: `addon-conventions`, `tdd` (testy czerwone przed kodem — zaraportuj).
| Plik | Zmiana |
|---|---|
| `PKG/tail_cut_view.py` | nowy wg kontraktu |
| `PKG/web.py` | `GET /api/tail_cut`, `_validate_settings`, użycie w `api_set_settings` (400 `{"ok": false, "errors": [...]}`) |
| `PKG/templates/options.html` | fieldset „Ogon grzania” |
| `PKG/templates/dashboard.html` | kafel + `<script src=".../static/tail_cut.js?v={{ version }}">` |
| `PKG/static/tail_cut.js` | nowy wg kontraktu |
| `tests/test_tail_cut_view.py`, `tests/test_web.py` | wg kontraktu |
Test: z `/config/addons/heiko_predictive_control/heiko_predictive_control`: `/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider tests/test_tail_cut_view.py tests/test_web.py`, potem pełny (`timeout 580`, baseline 906 passed).
**DONE:** testy zielone, pełny pytest 0 failed (906 + nowe, podaj bilans); `GET /api/tail_cut` zwraca wszystkie klucze kontraktu; POST −4 → 400 bez zapisu.
**NIE RÓB:** `tail_cut.py`, `experiments.py`, `g13_*.py`, `db.py`, `main.py`, `config.yaml`, `run.sh`, `telemetry.py`; nie podbijaj wersji; `CHANGELOG.md` nie edytuj; nie commituj; nie zmieniaj istniejących opcji/kafli (G13) poza dodaniem sąsiednich; zero emoji i PNG (tylko tekst/SVG); nie „poprawiaj” sąsiedniego kodu.

## Poza workerem (ja)
Watchdog HA: `ha_search` → automatyzacja watchdoga heartbeatu (> 45 min) → dodać akcję `heiko_heatpump.set_mode` `auto` **tylko jeśli** `mode_setting` = DHW (warunek), przez `ha_config_set_automation`; `automation.pompa_fix` nietknięta. Weryfikacja UI: Playwright przez ingress dopiero po wydaniu (S9) — kafel jest nowym kodem nieobecnym na żywo.

## Rollback
`git revert` commitu S7. Watchdog: usunąć dodaną akcję (stara konfiguracja zapisana w raporcie etapu).
