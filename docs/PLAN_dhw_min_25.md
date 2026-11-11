# Heiko predictive: minimalna nastawa CWU 25 °C (zamiast 16)

## Kontekst
Add-on `heiko_predictive_control` (0.25.0) pozwala zapisać nastawę CWU (`dhw_setpoint`) od 16 °C: w trybie
urlopowym (domyślnie 20 °C) i w testach testkit (cel `dhw`). Pompa przyjmuje minimum 25 °C. Zmieniam dolną
granicę na 25 °C we wszystkich miejscach i domyślne urlopowe 20 → 25.

Repo: `/config/addons/heiko_predictive_control`, gałąź `main`, czyste drzewo (HEAD 2268d04).

## Zmiany (kod: `heiko_predictive_control/heiko_predictive_control/`)
1. `experiments.py:26` — `DHW_MIN, DHW_MAX = 25, 60` (używa tego reguła zapisu `_WRITE_RULES` l.389, testkit
   `params.py` i `vacation_exec.dhw_target`).
2. `vacation_exec.py:65` — domyślne `vacation_dhw_setpoint` 20 → 25.
3. `web.py:64` — zakres walidacji `"vacation_dhw_setpoint": (25, 60)`.
4. `templates/options.html:104` — `min="25"`, domyślne `value` 25.
5. `catalog.py:59` — pełny zakres `dhw_setpoint` `(25, 60)`.

## Testy (`tests/`, TDD: najpierw asercje, potem kod)
- `test_no_pump_writes.py:67` → `("dhw_setpoint", 25, 60)`; `test_catalog.py:147` → `(25, 60)`.
- `test_vacation_exec.py`: `dhw_target({}) == 25`, clamp `5 → 25`; oczekiwane zapisy `[20]` → `[25]`,
  komunikaty „CWU 20” → „CWU 25”; `Pump(dhw_min=16)` → `25` (l.279), `dhw_intent: 20` gdzie to cel urlopu.
- `test_web.py:1044/1056`: odrzucane `24`, akceptowane `25` (zamiast 15/16).
- `test_testkit_engine.py:549/580`: wartości dhw 17 → np. 27 (żeby nadal były poprawne), 14 → np. 24.
- Polecenie: `cd .../heiko_predictive_control && uv run pytest -q` (wg `addon-conventions`).

## Wydanie
`simplify` → `code-review` (+ `silent-failure-hunter`) → `release` 0.25.1 (bump, CHANGELOG po polsku,
opublikowany release) → aktualizacja przez `ha_manage_updates` z backupem add-onu → weryfikacja na żywo.

## Weryfikacja na żywo
- Opcje add-onu (ingress, Playwright): pole „Nastawa CWU na urlop” ma min 25; zapis 24 odrzucony.
- Jeśli zapisane w bazie `vacation_dhw_setpoint` < 25 — `dhw_target` i tak przytnie do 25; sprawdzam w UI.
- Testkit: definicja z dhw < 25 zostanie odrzucona przez walidację (oczekiwane).

## Rollback
`git revert` commita + wydanie 0.25.2 albo przywrócenie backupu add-onu z `ha_manage_updates`.
