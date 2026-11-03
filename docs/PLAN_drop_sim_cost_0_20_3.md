# Plan 0.20.3 — usunięcie nieużywanych kolumn `sim_cost_today_*` / `baseline_cost_today_pln`

Zatwierdzone przez usera 2026-11-03 (opcja „wykonaj i wydaj 0.20.3”).

## Stan (sprawdzony grepem)
Kolumny `baseline_cost_today_pln`, `sim_cost_today_komfort_pln`, `sim_cost_today_ekonomia_pln` tabeli `cycles` nie mają
odbiorcy: JS/szablony ich nie czytają, sensory MQTT `heiko_savings_*` liczą się z planu. Jedyni czytelnicy to
`SELECT * FROM cycles` (`db.py` ~348/355, `stats.py` ~92). Brak indeksu na tych kolumnach; Alpine ma SQLite 3.40
(`DROP COLUMN` działa).

## Wykonawca (paczka W1, ryzyko: migracja → sonnet-worker, albo robi to sama sesja; worktree `wt new`)
Ścieżki od `heiko_predictive_control/heiko_predictive_control/heiko_predictive_control/` (testy: `…/tests/`).
1. `cycle.py` ~304-307: usuń `result[f"sim_cost_today_{name}_pln"] = …` i `result["baseline_cost_today_pln"] = …`;
   usuń `price0`, jeśli nic poza nimi go nie używa (sprawdź grepem przed usunięciem). ~494-495: usuń trzy klucze `None`
   (`baseline_cost_today_pln`, `sim_cost_today_komfort_pln`, `sim_cost_today_ekonomia_pln`) z wiersza cyklu poddasza.
2. `db.py`: usuń trzy kolumny ze `_SCHEMA` (~28-30) i z krotki `cols` w `insert_cycle` (~323-324). W `migrate()` po
   pętli `_ADDED_COLUMNS` dodaj idempotentne `ALTER TABLE cycles DROP COLUMN <nazwa>` dla każdej z trzech kolumn
   obecnej w `PRAGMA table_info(cycles)` (komentarz: 0.20.3 — kolumny bez odbiorcy).
3. `tests/test_attic_cycle.py` ~276-277: usuń te kolumny z lokalnego schematu testowego.
4. Nowy test migracji (np. w `tests/test_db.py` albo obok testów `migrate`): baza w starym schemacie z trzema kolumnami
   i jednym wierszem cyklu → po `db.migrate(conn)` kolumn nie ma, wiersz (ts, loop, setpointy) został, drugie
   `migrate` nic nie psuje; `insert_cycle` działa na zmigrowanej bazie.
5. `grep -rn "sim_cost_today\|baseline_cost_today" heiko_predictive_control/` poza `CHANGELOG.md`/`docs/` → 0 trafień.
- Komenda testu: `cd <WORKTREE>/heiko_predictive_control/heiko_predictive_control && <venv>/bin/python -m pytest -q -p no:cacheprovider`
  (venv ze scratchpada poprzedniej sesji mógł zniknąć — odtwórz z `requirements.txt` + pytest).
- `DONE:` pełny pytest zielony, grep z pkt 5 = 0, nowy test migracji przechodzi.
- `NIE RÓB:` `publisher.py`, sensory MQTT, `config.yaml`/`CHANGELOG` (robi `release`), `experiments.py`, `advisor.py`;
  bez commitu w worktree przez workera.

## Ryzyko i cofnięcie
Migracja jednokierunkowa: kod ≤0.20.2 na zmigrowanej bazie wywali `insert_cycle` („no such column”). Cofnięcie =
przywrócenie add-onu z backupu zrobionego przy instalacji (`backup=true`), nie samo zainstalowanie starej wersji.
`/api/history` przestaje zwracać te trzy pola (brak konsumentów — sprawdzone).

## Wydanie (od razu po etapie, bez przerw)
`simplify` → `code-review` (+ równolegle `pr-review-toolkit:silent-failure-hunter`, `pr-test-analyzer`, security
`general-purpose` read-only na `4c5eecd..HEAD`; repo i zakres podać jawnie) → `release` 0.20.3 (CHANGELOG po polsku,
neutralnie; wzmianka o jednokierunkowej migracji) → `ha_manage_updates` install `update.heiko_predictive_control_update`
z `backup=true` (przed tym `ha_manage_app action=check_updates`) → weryfikacja na żywo: `installed_version=0.20.3`,
nowe cykle zapisują się (Pulpit/`/api/history` odświeża wiersze), log add-onu bez błędów migracji, konsola czysta.
Restart HA tylko przez usera. Poza oknem commitów 02:45–03:15.
