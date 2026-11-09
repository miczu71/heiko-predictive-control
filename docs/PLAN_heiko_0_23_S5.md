# PLAN S5 — urlop E2: CWU (Heiko 0.23.0, ryzyko)

Źródło: `/config/docs/ROADMAP_heiko_0_23.md` (S5), `/config/docs/ROADMAP_heiko_vacation.md`. Wersji NIE podbijać.
Zakres: nastawa CWU w urlopie (start → `vacation_dhw_setpoint`, koniec → jednorazowo 48). Daty/Away/G13/AC = S6, UI i walidacja POST = S7.

## Kontrakt

**`experiments.py`**
- Stałe `DHW_SUFFIX = "dhw_setpoint"`, `DHW_MIN, DHW_MAX = 16, 60`.
- `_WRITE_RULES[DHW_SUFFIX] = lambda old, new: DHW_MIN <= new <= DHW_MAX and new == int(new)` (krok całkowity 1, bez limitu skoku — 48 ↔ 20). Sufiks `dhw_setpoint` NIE łapie `anti_legionella_setpoint` (`endswith`).
- `_confirm_writes`: gałąź dla `entity_id.endswith(DHW_SUFFIX)` przed ogólnym `what` (komunikat „Pompa nie potwierdziła zapisu nastawy CWU = X — sprawdź ręcznie.”, tytuł `"Heiko: tryb urlopowy"`). Bez niej zapis CWU trafiłby do komunikatu P0.
- Reszta `experiments.py` bez zmian.

**`catalog.py`**: `dhw_setpoint` — zakres pełny `(16, 60)`, zakres „aktywny” (Doradca) bez zmian `(45, 55)`, krok 2.

**`vacation_exec.py`**
- `DHW_AFTER = 48`; `dhw_entity(settings)` = `curve_shift_entity(settings)` z sufiksem `heating_curve_parallel_shift` zamienionym na `dhw_setpoint` (bez nowej opcji encji).
- `_config` bez zmian; `dhw_target(settings)` = `int(_num(settings, "vacation_dhw_setpoint", 20))` obcięte do 16–60.
- `run(..., get_state=ha_client.get_state)` (nowy parametr, domyślny; `get_numeric` jak dotąd).
- Stan (w `vacation_state`): `dhw_active` (bool: CWU ustawione przez tryb), `dhw_before` (wartość sprzed wejścia, informacyjnie), `dhw_intent`, `dhw_fail_day`, `dhw_warn` (`None` | `"min"` | `"brak"`), `dhw_written_id`.
- **Wejście** (ścieżka aktywna, po sprawdzeniu `running_test`, PRZED logiką krzywej, niezależnie od `suspended_day` — CWU jest od krzywej niezależne), gdy `not dhw_active`:
  - `current = get_numeric(dhw_entity)`; `None` → `dhw_warn="brak"`, nic więcej (kolejny tick ponowi).
  - `min` encji = `get_state(dhw_entity)["attributes"]["min"]`; jeśli `min > target` → `dhw_warn="min"`, `logger.warning` (raz na zmianę `dhw_warn`), BEZ zapisu i bez powiadomienia; kolejny tick sprawdza ponownie (po aktualizacji integracji `min`=16 zapis ruszy sam).
  - `current == target` (±0,05) → `dhw_active=True`, `dhw_before=current`, bez zapisu.
  - inaczej: `dhw_intent=target` + zapis stanu PRZED zapisem; `experiments._write(conn, settings, None, current, target, now, writer, entity=dhw, supersede=False)` (ValueError → traktuj jak odrzucenie). Przyjęty → `dhw_active=True`, `dhw_before=current`, `dhw_warn=None`. Odrzucony → `dhw_fail_day=dziś`, powiadomienie raz („Tryb urlopowy: HA odrzuciło zapis nastawy CWU X — pozostaje Y.”), ponowienie dopiero następnego dnia (`dhw_fail_day != dziś`). Zapis odrzucony NIE wstrzymuje trybu krzywej.
  - Stan zapisuj do bazy od razu po próbie (`dbm.set_setting`), bo dalsze wczesne `return None` w `_run` nie wołają `_save`.
- **Wyjście** (tryb wyłączony albo `heiko_enabled=False`), gdy `dhw_active`: obsłuż NIEZALEŻNIE od `base` (obecna gałąź `not active` kończy się `return None`, gdy `base is None` — nie może to zablokować CWU). `experiments.running_test` ≠ None → nic. `current = get_numeric(dhw)`; `None` → nic (ponów); `current != 48` → zapis 48 przez `_write` (`supersede=False`); przyjęty → `dhw_active=False`, czyszczenie pól `dhw_*`; odrzucony → stan zostaje, ponawianie co tick, powiadomienie raz dziennie (`dhw_fail_day`). `current == 48` → tylko czyszczenie. Nie sprawdzaj „ręcznej zmiany” (user mógł sam przywrócić 48 albo inną; po urlopie i tak ma być 48 — decyzja roadmapy).
- Wyjście i wejście nie zmieniają dotychczasowych wyników `run` dla krzywej (testy S4 zielone bez edycji).
- Bezpiecznik krzywej nie dotyka CWU.

## Wykonawca (paczka W1, ryzyko → `sonnet-worker`, worktree `wt new /config/addons/heiko_predictive_control heiko-0_23-S5`)

Repo: `/config/addons/heiko_predictive_control`; pakiet `heiko_predictive_control/heiko_predictive_control/` (moduły), testy `heiko_predictive_control/heiko_predictive_control/tests/` (ścieżki względem repo: `heiko_predictive_control/heiko_predictive_control/...`). Skille `addon-conventions`, `tdd` (testy najpierw, RED potwierdzone).

| Plik | Zmiana |
|---|---|
| `tests/test_experiments.py` | allowlista: `dhw_setpoint` 48→20 ok, 20→48 ok, 15 odrzucone, 61 odrzucone, 20.5 odrzucone; `anti_legionella_setpoint` odrzucony; zapis z `supersede=False` nie zmienia `confirmed_at` cudzego zapisu; niepotwierdzony zapis `dhw_setpoint` → powiadomienie CWU (nie P0) |
| `tests/test_catalog.py` | pełny zakres `dhw_setpoint` = 16–60, zakres Doradcy 45–55 bez zmian (istniejące wiersze 44/57 nadal False) |
| `tests/test_vacation_exec.py` | rozszerz `Pump` o encję CWU (`DHW`, `dhw`, `dhw_min`, `get_state`); testy: włączenie zapisuje 20 i ustawia `dhw_active`; `min`=40 > 20 → brak zapisu, `dhw_warn="min"`, krzywa działa; nastawa już = 20 → bez zapisu; odrzucony zapis CWU → powiadomienie raz, ponowienie następnego dnia, krzywa nie wstrzymana; wyłączenie zapisuje 48 raz i czyści stan; wyłączenie z odrzuceniem ponawia; wyłączenie gdy `base is None` (krzywa wstrzymana od startu) nadal przywraca 48; `heiko_enabled=False` przywraca 48; restart po intencji (current==target, `dhw_active` False) → bez ponownego zapisu; uruchomiony test = brak zapisu CWU; stare testy S4 bez zmian |
| `experiments.py` | wg kontraktu |
| `catalog.py` | wg kontraktu |
| `vacation_exec.py` | wg kontraktu |

Test: `cd <WORKTREE>/heiko_predictive_control/heiko_predictive_control && PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pyyaml pytest -q -p no:cacheprovider tests/test_vacation.py tests/test_vacation_exec.py tests/test_experiments.py tests/test_catalog.py tests/test_g13_exec.py` (pełny zestaw robię sam po `wt land`).

DONE: nowe testy zielone (RED widoczne przed implementacją), wymienione pliki testowe zielone bez osłabiania istniejących asercji, `git status` w worktree = tylko pliki z tabeli.

NIE RÓB: `config.yaml`, `web.py`, szablonów/JS (UI i walidacja = S7), `g13_*`, `attic.py`, `main.py`, nowych opcji w `config.yaml`, podbijania wersji, CHANGELOG, commitów, innych encji w allowliście.

## Rollback
`wt drop` przed landem; po landzie `git revert`. Tryb domyślnie wyłączony, więc commit nie zmienia zachowania add-onu; zakres katalogu (16–60) to tylko widok.
