# PLAN S6 — urlop E3: daty, Away, przejęcia (Heiko 0.23.0, ryzyko)

Źródło: `/config/docs/ROADMAP_heiko_0_23.md` (S6 + decyzje „G13 w urlopie”), `/config/docs/ROADMAP_heiko_vacation.md`. Wersji NIE podbijać.
Zakres: daty od/do, Away, klima poddasza, wykluczenie G13 i Startu testów, docisk szczytu. Walidacja POST/UI/powiadomienia adresatów = S7.

## Kontrakt

**`vacation.py`** (czyste)
- `is_active(settings, today: date) -> bool`: `bool(vacation_enabled)` ORAZ `vacation_from <= today <= vacation_to` (ISO `YYYY-MM-DD`; puste/None = brak ograniczenia; ISO nieparsowalne = False, bez wyjątku). NIE zależy od `heiko_enabled`.
- `decide(..., in_peak: bool = False)`: po bezpieczniku — `tripped` → baza (jak dziś, cofa też docisk); `in_peak` → `-SHIFT_LIMIT` (−3, absolutnie); inaczej `_clamp(base - drop)`. Powód `"urlop: szczyt"` dla docisku, `"urlop"` poza nim.

**`vacation_exec.py`**
- `active(settings, today)` = `vacation.is_active(settings, today)` ORAZ `heiko_enabled` (zapisy do pompy). Wszystkie wywołania `active(settings)` w module dostają `now.date()`.
- **Away** (na początku `_run`, przed `running_test`, niezależnie od `heiko_enabled`): `desired = vacation.is_active(...) and settings.get("vacation_with_away")`. Stała `AWAY_ENTITY = "input_boolean.away_mode"`. `desired` i `away_owned` fałsz/fałsz → nic; `desired` i stan encji `off` (przez `get_state`) → `input_boolean.turn_on`, `st["away_owned"]=True`; `desired` i encja już `on` → bez wywołania, `away_owned` bez zmian (nie nasze). `not desired` i `away_owned` → `turn_off`, `away_owned=False`. Usługa przez nowy parametr `run(..., away_call=ha_client.call_service)`; wywołanie nieudane → stan bez zmian, ponowienie następnym tickiem, `logger.warning`. Stan zapisuj od razu (`dbm.set_setting`).
- **Docisk szczytu**: `in_peak = bool(settings.get("g13_peak_enabled")) and window nie-None, nie-empty i window.start <= now.hour < window.end`, gdzie `window = g13_peak.window_from_feed(tariff_feed.for_settings(conn, settings), now.date())`. Przekazane do `vacation.decide`. (Bez `g13_peak_enabled` urlop nie robi docisku — decyzja wykonawcza: brak trybu szczytu = brak zachowania szczytowego.)
- **Przejęcie bazy G13** (tylko gdy `st["base"] is None`, tzn. pierwszy tick aktywacji): `g13 = dbm.get_setting(conn, g13_exec.STATE_KEY) or {}`; jeśli `g13.get("base") is not None` i `current` ≈ któraś z `g13_exec._known(g13)` → `base_shift = g13["base"]` (zamiast `current`). Następnie wyczyść stan G13: `g13_exec._clear(g13)`, `g13["last"] = {"ts": ..., "reason": "urlop", "target": None}`, `dbm.set_setting(conn, g13_exec.STATE_KEY, g13)` (ten sam `experiments._lock`). G13 bez bazy nie wykona powrotu.
- `run`/`_run` dostają `away_call`; reszta logiki (CWU, ręczna zmiana, potwierdzenia) bez zmian.

**`g13_exec.py`**: na początku `_run`, po wyliczeniu `st`/`entity`: jeśli `vacation_exec`‑aktywny — NIE importuj `vacation_exec` (cykl), użyj `vacation.is_active(settings, now.date()) and settings.get("heiko_enabled")` — `return None` (bez zapisów, bez ruszania stanu). Dotyczy też gałęzi `not active(...)`: sprawdzenie urlopu jest PIERWSZE.

**`experiments.py` `_plan`**: nowa gałąź po `g13_peak_enabled`: `elif vacation.is_active(settings, now.date()): reason = "wyłącz tryb urlopowy (Opcje), żeby uruchomić test"` (import `vacation`, brak cyklu — `vacation` nic nie importuje z pakietu).

**`g13_view.py`**: `_status` zwraca `"vacation"` (przed `"blocked"`), gdy `vacation.is_active(settings, today)` i `g13_peak_enabled` i `heiko_enabled`; `STATUS_LABELS["vacation"] = "urlop: szczyt −3"`; `now_view` dla `vacation` zwraca None (jak nie‑active); `build` bez `next`/`now`. Frontend G13 (JS/szablon) bez zmian, o ile etykieta idzie z `status_label`.

**`main.py:440`**: `on_vacation = True if vacation.is_active(settings, now.date()) else (get_bool_state(vacation_entity) if vacation_entity else False)` (import `vacation`). Wynik `get_bool_state` None zostaje None, gdy urlop nieaktywny.

## Wykonawca (paczka W1, ryzyko → `sonnet-worker`, worktree `wt new /config/addons/heiko_predictive_control heiko-0_23-S6`)

Repo: `/config/addons/heiko_predictive_control`; moduły `heiko_predictive_control/heiko_predictive_control/heiko_predictive_control/`, testy `heiko_predictive_control/heiko_predictive_control/tests/`. Skille `addon-conventions`, `tdd` (testy najpierw, RED potwierdzone). Przed pracą przeczytaj `vacation.py`, `vacation_exec.py`, `g13_exec.py`, `g13_peak.py`, `g13_view.py`, `tests/test_vacation_exec.py` (wzorzec `Pump`/`go`).

| Plik | Zmiana |
|---|---|
| `tests/test_vacation.py` | `is_active`: enabled False; enabled bez dat; przed `from`; w oknie; `to` włącznie; po `to`; tylko `from`; tylko `to`; ISO śmieci → False. `decide` z `in_peak`: drop 1 baza 0 → −1 poza szczytem, −3 w szczycie; baza −2 drop 1 w szczycie → −3; bezpiecznik w szczycie → baza |
| `tests/test_vacation_exec.py` | Away (fake `away_call` + stan encji): wejście włącza i ustawia `away_owned`; encja już `on` → bez wywołania, przy wyjściu bez `turn_off`; wyjście po `vacation_to` wyłącza raz; `vacation_with_away=False` → brak wywołań; nieudane wywołanie ponawiane. Daty: przed `from` nic nie zapisuje, w oknie zapisuje, po `to` wraca do bazy i 48. Szczyt (feed z oknem 16–21, `g13_peak_enabled`): drop 1, dzień baza−1, 17:00 → −3, 21:00 → baza−1; bez `g13_peak_enabled` brak docisku. Wejście w urlop o 17:00 przy stanie G13 `base=0`, `last_written=-3`, pompa −3 → baza urlopu 0 (nie −3), stan G13 wyczyszczony; koniec urlopu o 18:00 → zapis bazy 0. Bezpiecznik w szczycie → baza |
| `tests/test_g13_exec.py` | w urlopie (aktywnym, `heiko_enabled`) `run` zwraca None i nie zapisuje ani nie zmienia stanu, także w oknie szczytu; po końcu urlopu G13 działa dalej ze swoją bazą |
| `tests/test_experiments.py` | `_plan`/`start` zablokowany przy aktywnym urlopie z komunikatem; poza oknem dat Start działa |
| `tests/test_g13_view.py` | status `vacation` z etykietą „urlop: szczyt −3”, `now`/`next` None |
| `vacation.py`, `vacation_exec.py`, `g13_exec.py`, `experiments.py`, `g13_view.py`, `main.py` | wg kontraktu |

Test: `cd <WORKTREE>/heiko_predictive_control/heiko_predictive_control && PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pyyaml pytest -q -p no:cacheprovider tests/test_vacation.py tests/test_vacation_exec.py tests/test_g13_exec.py tests/test_g13_peak.py tests/test_g13_view.py tests/test_experiments.py tests/test_attic.py` (pełny zestaw robię sam po `wt land`, w tle).

DONE: nowe testy zielone (RED widoczne przed implementacją), istniejące asercje nieosłabione, `git status` w worktree = tylko pliki z tabeli.

NIE RÓB: `config.yaml`, `web.py`, szablonów/JS, `attic.py`, `cycle.py`, `catalog.py`, nowych zapisów do pompy poza `experiments._write`, podbijania wersji, CHANGELOG, commitów.

## Rollback
`wt drop` przed landem; po landzie `git revert`. Urlop domyślnie wyłączony, więc zmiana nie zmienia zachowania add-onu; jedyna zmiana przy wyłączonym urlopie to ścieżka `is_active` = False (nic).
