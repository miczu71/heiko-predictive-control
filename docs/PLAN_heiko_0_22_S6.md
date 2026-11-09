# PLAN S6 — E1 `tail_cut.py`: odcinanie „ogona” grzania (Heiko 0.22.0)

Źródła: `/config/docs/ROADMAP_heiko_0_22.md` (S6), `/config/docs/ROADMAP_heiko_tail_cut.md` (E1). Opt-in: `tail_cut_enabled=false` domyślnie, **zero zapisów przy wyłączonym**.
Fakty z integracji `heiko_heatpump` (odczyt): usługa `heiko_heatpump.set_mode` z polem `mode` (`"dhw"`=3, `"auto"`=4, `"heating"`=1, `"standby"`=0);
`sensor.heiko_heat_pump_mode_setting` i `sensor.heiko_heat_pump_working_mode_2` mają stan tekstowy i atrybut `raw_value` (liczba): mode_setting 4 = Auto, 3 = DHW; working_mode_2 `raw_value == 2` = grzanie (jak `heating_runs.HEATING_CODE`).
Potwierdzenie zapisu trybu NIE idzie przez `experiments._confirm_writes` (ten czyta `get_numeric` encji liczbowej) — robi je sam `tail_cut` odczytem `raw_value` `mode_setting`.

## Kontrakt

**`experiments.py`** (jedyne miejsce z `call_service`, obok `_write`):
- `MODE_SERVICE = ("heiko_heatpump", "set_mode")`, `MODE_CODES = {"dhw": 3, "auto": 4}`, `MODE_WRITE_ID = "heiko_heatpump.set_mode"` (wartość `pump_writes.entity_id`).
- `_write_mode(conn, old_code, mode, now, writer) -> tuple[bool, int]`: `mode` spoza `MODE_CODES` → `logger.warning` + `ValueError` (jak `_write`); wywołanie `(writer or ha_client.call_service)(*MODE_SERVICE, {"mode": mode})`; wpis w `pump_writes` (`ts, test_id=NULL, entity_id=MODE_WRITE_ID, old=old_code, new=MODE_CODES[mode], ok`) BEZ `supersede` (nie ruszać cudzych wierszy); zwraca `(ok, id_wiersza)`. `commit`.
- `_write` i `_WRITE_RULES` bez zmian (test allowlisty number.set_value zostaje).

**`PKG/tail_cut.py`** (nowy). Stan w `settings` pod `STATE_KEY = "tail_cut_state"` (JSON, zapis jak w `g13_exec._save`): `tail_since` (iso|None), `forced` (bool — my trzymamy `dhw`), `cut_at` (iso|None), `pause_until` (iso|None), `cuts` (lista iso, ostatnie 60), `alerted` (bool), `last` (`{ts, reason}`).
Opcje (przez `settings.get`, domyślne w kodzie; opcje Supervisora dochodzą w S7): `tail_cut_enabled` False, `tail_margin_k` 1.0, `tail_delay_min` 45, `tail_min_pause_min` 30, `tail_min_room_c` 20.5, `tail_dhw_hold_min` 2 (puste/`None` = domyślna, jak `g13_exec._num`). Encje: Tc = `heiko_water_temp_entity` albo `cycle._DEFAULT_WATER_TEMP`; nastawa = `heiko_water_setpoint_entity` albo `cycle.DEFAULT_WATER_TARGET_ENTITY`; tryb pracy = `heiko_working_mode_entity` albo `cycle._DEFAULT_MODE`; `MODE_SETTING_ENTITY = "sensor.heiko_heat_pump_mode_setting"` (opcja `heiko_mode_setting_entity` opcjonalnie).
`active(settings)`: `tail_cut_enabled` i `heiko_enabled` (jak `g13_exec.active`).
`run(conn, settings, now, *, get_state=ha_client.get_state, get_numeric=ha_client.get_numeric_state, writer=None, notify=None, sleep=time.sleep) -> dict | None` — całość pod `experiments._lock`. Kolejność w jednym przebiegu:
1. **Trzymamy `dhw` (`forced`)**: powrót do `auto`, gdy `now - cut_at ≥ tail_dhw_hold_min` ALBO trwa test (`experiments.running_test`) ALBO pokój < próg (lub brak świeżego odczytu pokoju — `g13_exec._min_room`) ALBO opcja wyłączona. Inaczej `None` (czekamy). Powrót = `_set_mode("auto")` (niżej); sukces → `forced=False`, `pause_until = cut_at + tail_min_pause_min`, `alerted=False`; porażka → `forced` zostaje, jedno powiadomienie na epizod (`alerted`), kolejny przebieg ponawia.
2. Nie `active` → `tail_since=None`, `None` (bez żadnego zapisu do pompy). Trwa test → `tail_since=None`, `None`. `now < pause_until` → `None`.
3. Odczyty: `raw_value` trybu pracy, Tc, nastawa, `raw_value` `mode_setting`, pokój. Którykolwiek `None` → `tail_since=None`, `None`. Cięcie tylko gdy tryb pracy `== 2` (grzanie) **i** `mode_setting == 4` (Auto — nie zmieniamy trybu wybranego przez usera) **i** pokój ≥ `tail_min_room_c`; inaczej `tail_since=None`.
4. `Tc ≥ nastawa + tail_margin_k` → `tail_since = tail_since or now`; w przeciwnym razie `tail_since=None`. `now - tail_since ≥ tail_delay_min` → **odcięcie**: stan `forced=True, cut_at=now` zapisany PRZED zapisem do pompy (odporność na restart), `_set_mode("dhw")`; sukces → `cuts += [now]`, `tail_since=None`, powiadomienie (`experiments._notify`, tytuł `"Heiko: ogon grzania"`: „Odcięto ogon grzania (Tc X, nastawa Y, od N min). Powrót do Auto za M min.”); porażka potwierdzenia → natychmiast `_set_mode("auto")`, `forced=False`, `pause_until = now + tail_min_pause_min`, powiadomienie alarmowe.
`_set_mode(conn, settings, mode, old_code, now, get_state, writer, notify, sleep) -> bool`: do 2 prób: `experiments._write_mode` → `sleep(CONFIRM_WAIT_S=70)` → odczyt `raw_value` `mode_setting` równy `MODE_CODES[mode]` = potwierdzone. Wiersz `pump_writes` dostaje `confirmed_at = iso(now)` (potwierdzone) albo `experiments.NO_CONFIRMATION` (po wyczerpaniu prób) — **nigdy NULL** po powrocie z funkcji (inaczej `experiments.tick` wziąłby go do swojego potwierdzania). `ValueError` z `_write_mode` → `False` + `logger.error`.
`recover_on_start(conn, settings, now, *, get_state, writer, notify, sleep) -> dict | None`: stan `forced` albo (`mode_setting` raw == 3 i stan wskazuje, że to my) → `_set_mode("auto")`, `forced=False`; bez `forced` nic nie robi.
Każdy wyjątek w odczycie → `logger.exception` w jobie (jak `run_g13_peak`), nie cichy sukces.

**`telemetry.py`** (`detect_changes`): zmiana parametru `working_mode` (klucz katalogu), której czas leży ≤ 3 min od wiersza `pump_writes` z `entity_id == experiments.MODE_WRITE_ID`, `ok = 1`, i `new` zgodnym z nową wartością (`DHW`→3, `Auto`→4, porównanie po nazwie stanu lowercase) dostaje `source = "add-on (tail_cut)"` zamiast „nieznane”/innego. Reszta bez zmian; brak wiersza = dotychczasowe źródło.

**`main.py`**: `run_tail_cut()` (wzór `run_g13_peak`) co **1 min**, `next_run_time=now+50 s`, `max_instances=1, coalesce=True`; `recover_tail_cut()` jednorazowo `now+17 s` (wzór `recover_tests`). Import `tail_cut`.

## Wykonawca (jedna paczka W1, „ryzyko: zapis do pompy” → `sonnet-worker`; potem `claimcheck` + jeden `verifier`)
Repo: `/config/addons/heiko_predictive_control`, kod `heiko_predictive_control/heiko_predictive_control/` (`PKG`), testy `heiko_predictive_control/tests/`. Skille: `addon-conventions`, `tdd` (testy czerwone przed kodem — zaraportuj).

| Plik | Zmiana |
|---|---|
| `PKG/experiments.py` | `MODE_SERVICE`, `MODE_CODES`, `MODE_WRITE_ID`, `_write_mode` wg kontraktu; docstring modułu: jedno zdanie o usłudze `set_mode` dla `tail_cut` |
| `PKG/tail_cut.py` | nowy, wg kontraktu |
| `PKG/telemetry.py` | oznaczenie źródła `add-on (tail_cut)` w `detect_changes` |
| `PKG/main.py` | dwa joby (`run_tail_cut`, `recover_tail_cut`) |
| `tests/test_tail_cut.py` | nowy: fixture z bazą w pamięci jak `test_g13_exec.py`, atrapy `get_state/get_numeric/writer/notify/sleep` |
| `tests/test_no_pump_writes.py` | `experiments`: `call_service` dozwolone w `{"_write","_write_mode"}`; asercje `MODE_CODES == {"dhw": 3, "auto": 4}`, `MODE_SERVICE == ("heiko_heatpump","set_mode")`; `tail_cut` NIE zawiera `call_service` (zapis tylko przez `experiments._write_mode`) |
| `tests/test_experiments.py` | `_write_mode`: `dhw`/`auto` przechodzą, `"heating"`/`"standby"`/`""` → `ValueError`, wiersz w `pump_writes`, nie unieważnia cudzych niepotwierdzonych wierszy |
| `tests/test_telemetry.py` | `working_mode` zmieniony ≤ 3 min po wierszu `pump_writes` z `MODE_WRITE_ID` → `source == "add-on (tail_cut)"`; bez wiersza → bez zmian |

Testy w `test_tail_cut.py` (minimum): wyłączone → zero wywołań `writer` (przy ogonie i bez); ogon < `tail_delay_min` → brak; ogon ≥ delay → `dhw` + wiersz `confirmed_at` ustawiony; Tc spada poniżej marginesu → `tail_since` czyszczone; pokój < próg → brak odcięcia, a przy `forced` → natychmiastowe `auto`; tryb pracy ≠ 2 → brak; `mode_setting` ≠ Auto → brak; test w toku → brak (i powrót `auto` przy `forced`); powrót `auto` po `tail_dhw_hold_min`; `pause_until` blokuje kolejne odcięcie; brak potwierdzenia `dhw` → `auto` + alarm + `NO_CONFIRMATION` w wierszu; brak potwierdzenia `auto` → `forced` zostaje, jedno powiadomienie na epizod, ponowienie w następnym przebiegu; `recover_on_start` przy `forced` → `auto`, bez `forced` → zero zapisów; brak kolizji z G13: `g13_exec.run` i `tail_cut.run` na jednej bazie — wiersze `pump_writes` obu trybów nie zmieniają sobie `confirmed_at`, G13 nie widzi wiersza `MODE_WRITE_ID` jako swojego; brak odczytu (`None`) → brak zapisu.

Test: z `/config/addons/heiko_predictive_control/heiko_predictive_control`: `/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider tests/test_tail_cut.py tests/test_no_pump_writes.py tests/test_experiments.py tests/test_telemetry.py`, potem pełny (`timeout 580`, baseline 878 passed).
**DONE:** testy zielone; pełny pytest 0 failed (878 + nowe, podaj bilans); `grep -rn "call_service" PKG/tail_cut.py` — 0 trafień; zero zapisów przy `tail_cut_enabled=false` (test).
**NIE RÓB:** nie ruszaj `g13_exec.py`, `g13_peak.py`, `tariff_feed.py`, `db.py`, `cycle.py`, `web.py`, `config.yaml`, `run.sh`, szablonów/UI (to S7); nie zmieniaj `_write`/`_WRITE_RULES`/`_confirm_writes`; nie dodawaj opcji Supervisora; nie podbijaj wersji; `CHANGELOG.md` nie edytuj; nie commituj; nie „poprawiaj” sąsiedniego kodu.

## Rollback
`git revert` commitu S6 (moduł i joby znikają; `tail_cut_enabled` nigdzie nie jest jeszcze opcją). Awaryjnie ręcznie `heiko_heatpump.set_mode` `auto`.
