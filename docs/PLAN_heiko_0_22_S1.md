# PLAN S1 — G13 w zakresie przesunięcia −3…+3 (Heiko 0.22.0)

Źródło: `/config/docs/ROADMAP_heiko_0_22.md` (S1), `/config/docs/ROADMAP_heiko_g13_peak.md` (korekta 09.11, K1/K2).

## Fakty (potwierdzone na żywo 09.11)
Historia `number.heiko_heat_pump_heating_curve_parallel_shift` 06.11: 16:03:29 → −4, 16:03:34 → powrót do +1. Pompa przyjmuje tylko −3…+3;
HA przyjął usługę (`ok=1`), pompa cofnęła wartość. `_confirm_writes` oznacza taki zapis po 10 min jako `NO_CONFIRMATION` i powiadamia,
ale treść powiadomienia mówi „przywrócenia przesunięcia”, co dla zapisu trybu G13 jest mylące.

## Zmiana
1. Zakres przesunięcia krzywej −3…+3 wszędzie (testy Doradcy, G13, Opcje).
2. Domyślne `g13_peak_peak_shift` = −3.
3. Zapis spoza zakresu = `ValueError` (jak dziś) **plus** `logger.warning` (nie cichy zapis). Wpisu w `pump_writes` nie dodajemy (G13 już łapie `ValueError`, powiadamia i wstrzymuje tryb do jutra).
4. Migracja zapisanych `g13_peak_peak_shift` / `g13_peak_preheat_shift` spoza −3…3 → obcięcie do granicy (klucz wersji w `settings`).
5. K2: treść powiadomienia dla zapisu G13 bez potwierdzenia (`test_id is None`, encja = przesunięcie krzywej) mówi o zapisie trybu Szczyt G13, nie o „przywróceniu”; test regresji „pompa cofnęła zapis → NO_CONFIRMATION + powiadomienie”.

## Wykonawca (sonnet-worker; paczka „ryzyko” — zapis do pompy; jedna paczka)
Repo: `/config/addons/heiko_predictive_control`, kod w `heiko_predictive_control/heiko_predictive_control/`, testy w `heiko_predictive_control/tests/`.
Skille: `addon-conventions`, `tdd` (najpierw testy czerwone, potem kod).

| Plik | Zmiana |
|---|---|
| `heiko_predictive_control/experiments.py` | l.27 `SHIFT_MIN, SHIFT_MAX, MAX_STEP = -3, 3, 1` (`G13_MAX_STEP = 2 * SHIFT_MAX` zostaje, daje 6); komentarze „±4” → „±3” (l.28, docstring `_write`); w `_write` przed `raise ValueError` dodaj `logger.warning("Odrzucony zapis %s: %s → %s (poza granicami)", ...)`; w `_confirm_writes` dla `w["test_id"] is None` i encji z `ALLOWED_ENTITY_SUFFIX` komunikat: `Pompa nie potwierdziła zapisu przesunięcia {new:g} (tryb Szczyt G13) — sprawdź ręcznie.` (pozostałe gałęzie bez zmian) |
| `heiko_predictive_control/g13_peak.py` | l.20 `SHIFT_LIMIT = 3.0`; l.28 `peak_shift: float = -3.0`; komentarze |
| `heiko_predictive_control/g13_exec.py` | l.45 domyślne `-3.0`; docstring l.4 „±4” → „±3” |
| `heiko_predictive_control/templates/options.html` | l.79–82: etykiety „(−3…3)”, `min="-3" max="3"`, domyślne szczytu `-3` |
| `heiko_predictive_control/db.py` | nowa `_migrate_g13_range_0_22(conn)` wołana obok `_migrate_catalog_0_9_3` (l.255): jeśli `get_setting(conn, "g13_range_schema") == 1` → return; dla kluczy `g13_peak_peak_shift`, `g13_peak_preheat_shift`: gdy wartość liczbowa i poza −3…3 → `set_setting` na obciętą, `logger.warning`; na końcu `set_setting(conn, "g13_range_schema", 1)` |
| `heiko_predictive_control/tests/test_g13_peak.py`, `test_g13_exec.py` | `-4` jako wartość szczytu → `-3` (także `calls == [1.0, -3.0 ...]`, stany `intent`/`last_written`); pozostałe liczby bez zmian |
| `heiko_predictive_control/tests/test_experiments*.py` (grep `-4`/`4` jako granica zakresu) | dostosuj granice do ±3 |
| nowe testy (w istniejących plikach) | `_write` odrzuca −4 i +4 (`ValueError`, `caplog` ma ostrzeżenie), przyjmuje −3 i +3; `Config().peak_shift == -3.0`; migracja: −4 → −3, 4 → 3, wartość w zakresie bez zmian, brak klucza bez zmian, drugie wywołanie nie zmienia (klucz wersji); `_confirm_writes`: zapis G13 (`test_id=None`) niepotwierdzony po `CONFIRM_MIN` → `confirmed_at == NO_CONFIRMATION` i dokładnie jedno powiadomienie z tekstem „tryb Szczyt G13” |

Komenda testu (z `/config/addons/heiko_predictive_control/heiko_predictive_control`, na pierwszym planie):
`/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider` (baseline: 801 passed; jeśli ten interpreter nie ma pytest, zgłoś — nie instaluj niczego globalnie).

**DONE:** pytest zielony (≥ 801 + nowe testy, 0 failed); `grep -rn -E '(-4|SHIFT_LIMIT = 4|±4|−4)' heiko_predictive_control/ ` w kodzie (poza `tests/` i komentarzem o historii zakresu) bez trafień dotyczących zakresu przesunięcia; nowe testy czerwone przed zmianą kodu (zaraportuj to).
**NIE RÓB:** nie ruszaj `tariff*.py`, `kpi.py`, `cycle.py`, `web.py`, `g13_view.py`, `config.yaml` (wersji nie podbijaj), `CHANGELOG.md`; nie commituj; nie dodawaj zależności; nie zmieniaj zachowania poza opisanym (bez „poprawiania” sąsiedniego kodu); nie zmieniaj `P0_*`/`SLOT52_*`.

## Rollback
`git revert` commitu S1. Zapisane w bazie wartości obcięte migracją (−4 → −3) nie wracają — to zamierzone (pompa i tak nie przyjmuje −4).
