# PLAN S4 — urlop E1: rdzeń (Heiko 0.23.0, ryzyko)

Źródło: `/config/docs/ROADMAP_heiko_0_23.md` (S4), `/config/docs/ROADMAP_heiko_vacation.md`. Wersji NIE podbijać.
Wzorzec: `g13_peak.py` (czyste `decide`) + `g13_exec.py` (wykonanie). Zakres S4: tylko krzywa. CWU (S5), daty/Away/G13/AC (S6), UI i walidacja `POST /api/settings` (S7) — poza zakresem.

## Kontrakt

**`vacation.py`** (czyste, bez I/O)
- `SHIFT_LIMIT = 3.0`, `BREAKER_HOLD = timedelta(minutes=15)`, `RESUME_MARGIN = 1.0` (°C histerezy).
- `Config(drop: int = 2, min_room_c: float = 16.0)`; `State(cold_since: datetime | None = None, tripped: bool = False)` (frozen).
- `Decision(target: float | None, reason: str, state: State, base: float | None, event: str | None)`.
- `decide(now, *, current_shift, base_shift, min_room_c, state, cfg) -> Decision`:
  - `base = current_shift if base_shift is None else base_shift`; `drop` obcięte do 1–3 (`max(1, min(3, round(drop)))`).
  - Bezpiecznik: `min_room_c is None` → stan bez zmian (brak odczytu nie wyzwala ani nie zeruje). `room >= cfg.min_room_c` → `cold_since=None`, a jeśli `tripped` i `room >= cfg.min_room_c + RESUME_MARGIN` → `tripped=False`. `room < cfg.min_room_c` → `cold_since = cold_since or now`; po `BREAKER_HOLD` → `tripped=True`, `event="bezpiecznik"` (tylko w ticku zadziałania).
  - `tripped` → `Decision(base, "bezpiecznik", …)`; inaczej `Decision(clamp(base - drop, -3, 3), "urlop", …)`. `base` zawsze zwracane (do zapamiętania).

**`vacation_exec.py`** — kopia struktury `g13_exec.py` (czytaj go w całości; zachowaj `_known`, `_close`, `_apply` z intencją PRZED zapisem, `maybe`, `prev`, `last_write_id`, `nc_id`, powrót po wyłączeniu z ponawianiem):
- `STATE_KEY = "vacation_state"`, `TITLE = "Heiko: tryb urlopowy"`, `ROOM_MAX_AGE` i `_coldest_room` jak w G13 (import z `g13_exec`, nie kopiuj).
- `active(settings)` = `vacation_enabled` (domyślnie False) i `heiko_enabled`.
- `_config(settings)`: `vacation_curve_drop` (domyślnie 2), `vacation_min_room_c` (domyślnie 16.0), pusta opcja = domyślna.
- `run(conn, settings, now, *, get_numeric, writer, notify)` pod `experiments._lock`; zapis **wyłącznie** `experiments._write(..., max_step=experiments.G13_MAX_STEP)`.
- Bez okna taryfy i bez „końca okna”: urlop działa całą dobę, więc w ścieżce aktywnej nie ma `after_window`; powrót do bazy tylko po wyłączeniu (jak gałąź `not active` w G13).
- Ręczna zmiana przesunięcia (wartość spoza `_known`) → `suspended_day = dziś`, `_clear`, komunikat (przez `experiments._notify`) „Tryb urlopowy wstrzymany do jutra: przesunięcie zmieniono ręcznie na X”. Wstrzymanie wygasa następnego dnia (`suspended_day != today`).
- Zapis odrzucony/niepotwierdzony → jak G13 (wstrzymanie do jutra, `maybe`, powiadomienie).
- Bezpiecznik: `event == "bezpiecznik"` → `trips` (ostatnie 30 dat), `trip_room {name, c}`, powiadomienie przez istniejące `experiments._notify` (usługa `advisor_notify_service`, bez zmian w `experiments.py`); wybór adresata (`notify.kacper`/`notify.family`) to S7.
- Ścieżka wyłączona: powrót do `base` (jeśli `current` ∈ `_known`), ponawianie przy odrzuceniu, potem `_clear`.
- `experiments.running_test(conn)` ≠ None → `None` (nic nie rób).

**`main.py`**: import `vacation_exec`; `run_vacation()` wzorem `run_g13_peak` (log `"Tryb urlopowy: %s"`), job `interval` 5 min, `next_run_time = now + 55 s`, `max_instances=1, coalesce=True`.

## Wykonawca (paczka W1, ryzyko → `sonnet-worker`, worktree `wt new <repo> heiko-0_23-S4`)

Repo: `/config/addons/heiko_predictive_control`; pakiet `heiko_predictive_control/heiko_predictive_control/heiko_predictive_control/`, testy `heiko_predictive_control/heiko_predictive_control/tests/`. Skille `addon-conventions`, `tdd` (testy najpierw, RED potwierdzone).

| Plik | Zmiana |
|---|---|
| `tests/test_vacation.py` (nowy) | `decide`: baza z bieżącego, `base − drop`, obcięcie do −3 (baza −2, drop 3 → −3), drop poza 1–3 obcięty, bezpiecznik po 15 min poniżej progu, brak odczytu nie wyzwala/nie zeruje, powrót z histerezą (próg+1), `event` tylko raz |
| `tests/test_vacation_exec.py` (nowy) | wzorem `test_g13_exec.py` (`Pump`, `conn`): włączenie (zapis −X, stan base/intent), wyłączenie (powrót do bazy), restart w trakcie (nowy proces, ten sam stan → bez ponownego zapisu i bez „ręcznej zmiany”), odrzucony zapis (wstrzymanie, `maybe`), brak potwierdzenia, ręczna zmiana, bezpiecznik (powrót do bazy + powiadomienie, po ustąpieniu wznowienie), obcięcie do −3, `heiko_enabled=False` = brak zapisu i powrót, uruchomiony test = brak działania, wstrzymanie wygasa następnego dnia |
| `vacation.py` (nowy) | wg kontraktu |
| `vacation_exec.py` (nowy) | wg kontraktu |
| `main.py` | `run_vacation` + job |

Test: `cd /config/addons/heiko_predictive_control/heiko_predictive_control && PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pyyaml pytest -q -p no:cacheprovider tests/test_vacation.py tests/test_vacation_exec.py tests/test_g13_exec.py` (w worktree: ścieżka worktree zamiast `/config/addons/...`); pełny zestaw robię sam po `wt land`.

DONE: nowe testy zielone (RED widoczne przed implementacją), `test_g13_exec.py` i `test_g13_peak.py` bez zmian i zielone, `git status` w worktree = tylko pliki z tabeli.

NIE RÓB: `config.yaml`, `web.py`, szablonów/JS (UI i walidacja = S7), `g13_*`, `attic.py`, `catalog.py`, `dhw_setpoint` (S5), podbijania wersji, CHANGELOG, commitów.

## Rollback
`wt drop <repo> heiko-0_23-S4` przed landem; po landzie `git revert`. Tryb domyślnie wyłączony (`vacation_enabled` False), więc commit nie zmienia zachowania add-onu.
