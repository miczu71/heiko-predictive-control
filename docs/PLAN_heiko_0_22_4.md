# Heiko 0.22.4 — bezpiecznik G13 dopiero od szczytu, nazwa pokoju w powiadomieniu i kaflu

## Kontekst
Dziś (2026-11-09) o 13:04 tryb Szczyt G13 ustawił dogrzewanie (+1), a o 13:24 bezpiecznik cofnął przesunięcie do 0
(najzimniejszy pokój ~20,15 °C, poniżej 20,5 od ~09:40). User słusznie: bezpiecznik w dogrzewaniu jest nielogiczny —
+1 grzeje bardziej, powrót do bazowego grzeje mniej. Do tego zadziałanie blokuje tryb do końca doby, więc o 16:00
nie będzie też −3. Komunikat nie mówi, który pokój był za zimny (dane są w `cycles.min_room_name`, ale nie trafiają
do trybu).

Decyzje usera:
- bezpiecznik nie działa w dogrzewaniu 13–16; decyzja o szczycie zapada od 16:00,
- próg 20 °C user ustawia sam w Opcjach (bez zmiany domyślnej),
- dzisiejszy stan bezpiecznika ma zostać wyczyszczony po wydaniu (żeby dziś o 16:00 zadziałał szczyt).

Repo: `/config/addons/heiko_predictive_control` (= `~/ha-config/addons/heiko_predictive_control`), gałąź `main`,
wersja 0.22.3 → **0.22.4**. Termin: wydanie przed 16:00, inaczej dzisiejszy szczyt i tak przepada (wtedy wydanie
normalnie, bez pośpiechu).

## Zmiany

1. **`g13_peak.py` — bezpiecznik tylko w szczycie** (`decide`, `_breaker`):
   - w dogrzewaniu (`hour < window.start`) `_breaker` dalej liczy `cold_since` (zimny pokój od 15:40 → wiadomo o 16:00),
     ale **nie ustawia `tripped`**; zadziałanie możliwe tylko przy `hour >= window.start`.
   - efekt: 13–16 zawsze bazowe + dogrzanie; o 16:00, jeśli pokój < progu od ≥15 min → od razu bazowe (bez zapisu −3
     i cofania po 15 min); inaczej −3 i bezpiecznik pilnuje dalej.
   - docstring modułu poprawiony („Bezpiecznik działa tylko w oknie szczytu”).
2. **`g13_exec.py` — nazwa pokoju**:
   - `_min_room` zwraca `(c, name)` z `dbm.latest_cycle` (kolumna `min_room_name` już jest); do `decide` idzie `c`.
   - przy `decision.event == "bezpiecznik"`: `st["trip_room"] = {"name": name, "c": c}` i powiadomienie
     „Tryb Szczyt G13: {name} ma {c:.1f} °C — poniżej {próg} °C przez 15 min, wracam do bazowego do {end}:00.”
     (brak nazwy → „najzimniejszy pokój”).
3. **`g13_view.py` + `static/g13.js` — kafel**: `build` zwraca `trip_room` (gdy status `tripped`); JS:
   „Bezpiecznik: {name} ({c} °C) był za zimny, przesunięcie wróciło do bazowego do końca szczytu.”
   (fallback do obecnego tekstu bez nazwy). Escaping przez `HPC.esc`.
4. **`db.py` — jednorazowe wyczyszczenie dzisiejszego zadziałania** (wzorzec `_migrate_g13_range_0_22`):
   `_migrate_g13_preheat_trip_0_22_4` z markerem `g13_preheat_trip_schema`: jeśli `g13_peak_state.tripped`, zeruje
   `tripped`/`cold_since` i usuwa ostatni wpis z `trips` tylko gdy to dzisiejsza data (zadziałanie w starej logice
   mogło być w dogrzewaniu). Stan starszych dni i tak zeruje się nową dobą. Po restarcie add-onu tryb w dogrzewaniu
   zapisze znów +1, o 16:00 −3 (albo bazowe, gdy pokój < progu).
5. **Testy** (`tests/test_g13_peak.py`, `test_g13_exec.py`, `test_g13_view.py`, test migracji): zimno w dogrzewaniu
   nie wyzwala; zimno od 15:40 → o 16:00 od razu bazowe; zimno od 16:00 → −3, potem bazowe o 16:15; nazwa pokoju
   w powiadomieniu i w `/api/g13`; migracja czyści tylko dzisiejsze, idempotentna.
6. **CHANGELOG 0.22.4**, bump `config.yaml` + `__version__`.

Wykonawca: robię sam (jedna paczka, ~8 edycji, sterowanie pompą + termin 16:00 — delegacja z weryfikacją zjadłaby
czas). Kolejność: tdd (testy czerwone → kod) → `simplify` → `code-review` (+ `silent-failure-hunter`,
`pr-test-analyzer`, security subagent równolegle) → `release` → weryfikacja na żywo.

## Weryfikacja
- `uv run pytest` w repo add-onu — cały zestaw zielony (obecnie 921+).
- Po aktualizacji add-onu (`ha_manage_updates`, backup add-onu): log startu bez błędów migracji;
  `number.heiko_heat_pump_heating_curve_parallel_shift` w ciągu ~5 min = +1 (jeśli przed 16:00), o 16:00 = −3 albo
  0 z powiadomieniem z nazwą pokoju; kafel Szczyt G13 na Pulpicie (Playwright przez ingress) pokazuje stan/nazwę.
- Przypomnienie dla usera: próg 20 °C w Opcjach → Szczyt G13 → „Bezpiecznik — minimum pokoi”.

## Rollback
Aktualizacja z `backup=true` → przywrócenie backupu add-onu (albo instalacja 0.22.3 — migracja nie zmienia schematu,
tylko stan JSON w `settings`). Gdyby coś szło źle w szczycie: wyłącznik trybu w Opcjach (powrót do bazowego).
