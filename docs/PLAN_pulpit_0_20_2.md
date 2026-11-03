# heiko_predictive_control 0.20.2 — Pulpit bez symulacji, z realnymi zapisami i jednym kosztem

## Context
Analiza „Szac. oszczędność dziś” (4,93–5,12 zł przy 3,06 zł realnie) na żywych danych (`/api/history/heiko`, 11 dni):
to różnica dwóch symulacji z dwoma błędami w tę samą stronę (model natywny ~1,5× za wysoko od 30.10; suma pierwszych
kroków planu zaniża jego koszt, 27.10 Komfort = 0,01 zł/dobę). Pętla A nigdy nie zapisuje planu → realna oszczędność 0.
Przegląd Pulpitu (screenshot 1440) pokazał też: dwa różne koszty pompy na jednym ekranie (3,06 zł licznik×cena vs Doba
podłoga+CWU 3,20 zł), nieprawdziwe zdanie w karcie pompy („add-on niczego do niej nie zapisuje” — testy i slot 52
zapisują), połowę karty pompy jako poglądowy model, brak informacji o tym, co add-on realnie zmienia w pompie
(trwa test #8 „Bezwładność −1”, następny krok 15:00 → −1).

Decyzje usera: usunąć kafelki „Szac. oszczędność dziś” i „Zmiany nastaw pompy” oraz linie „Model — …”; dodać kafelkę
„Zapisy do pompy”; jeden koszt pompy z kafla Doba + wczoraj; odchudzić kartę pompy. Zostają: tabela 36 h w foldzie,
wykres planu, 4 sensory HA (nieużywane w HA — sprawdzone), kolumny DB `baseline_cost_today_pln`/`sim_cost_today_*`.

**Kto korzysta:** user na Pulpicie (desktop + telefon w Companion). **Automatycznie:** nic nowego nie pisze do pompy —
wyłącznie UI/odczyt. **Za zgodą:** wydanie i instalacja (ten plan). **Sukces:** pasek pokazuje 3 kafelki
(Taryfa · Pompa dziś z wczoraj · Zapisy do pompy), jedna liczba kosztu pompy zgodna z Doba, karta pompy bez wierszy
poglądowych i z prawdziwym opisem, testy zielone, konsola czysta.

## Repo
`/config/addons/heiko_predictive_control` (main, czysty, HEAD 4463672 = 0.20.1). Krok 0: skopiować ten plan do
`docs/PLAN_pulpit_0_20_2.md`. Nie w oknie 02:45–03:15. UI: krótki przegląd skillem `impeccable` przed edycją.

## Wykonawca (haiku-worker, worktree `wt new`, jedna paczka W1)
Pliki (ścieżki od `heiko_predictive_control/heiko_predictive_control/`):
1. `templates/dashboard.html` pasek `.stat-strip`:
   - usuń `div.stat` „Zmiany nastaw pompy” (`#param-changes`) i „Szac. oszczędność dziś” (`#cost-savings`);
   - „Pompa dziś — realnie” → etykieta „Pompa dziś”, wartość `#pump-cost` „—”, sub `#pump-cost-sub` „podłoga + ciepła woda · wczoraj: —”;
   - nowa kafelka „Zapisy do pompy” renderowana z `writes` (z web.py): gdy `writes.test` → wartość = tytuł testu,
     sub = „następny krok {HH:MM} → {wartość}” albo „ostatni krok wykonany”; gdy brak testu → wartość „brak”,
     sub „pompa na własnej krzywej”; zawsze dopisek „· propozycje Doradcy: {n}” jako link do `{{ base }}/doradca`
     (sprawdź istniejącą ścieżkę Doradcy w `base.html`), gdy n > 0.
2. `web.py` `page_dashboard` (:129-142): usuń `changes = telemetry.changes_summary(...)` i `changes=changes`;
   dodaj `writes = {"test": …, "pending": …}`: `test = experiments.running_test(conn)` → `{title: experiments.KINDS[kind].title,
   next_at: schedule[step]["at"][11:16] | None, next_value: schedule[step]["value"] | None}` (schedule = `json.loads(test["schedule"])`,
   brak następnego kroku gdy `step >= len`); `pending = COUNT(*) FROM proposals WHERE status = advisor.STATUS_PENDING`.
   Usuń endpoint `/api/today_summary/<loop>` (:237-258) — jedyny konsument to `refreshCost` (usuwany).
   `telemetry.changes_summary` zostaje (używa `/api/changes` :445 i `analysis.py:284`).
3. `static/dashboard.js`: usuń `refreshCost()` w całości, jej wywołania/interwał i komentarz w :2 o `/api/today_summary`.
4. `static/day.js`: po renderze doby „dziś” ustaw `#pump-cost` = `fmt(floor.pln + dhw.pln)` (pomiń składnik
   z `configured: false`); raz po starcie pobierz `/api/day_summary?day=yesterday` i ustaw w `#pump-cost-sub`
   „podłoga + ciepła woda · wczoraj: X zł”. Brak elementów (inna strona) → nic nie rób.
5. `templates/_cards.html` karta `loop_heiko`:
   - `p.sub` (:13-17) → „Podłogówka całego domu · pętla A. Grzaniem steruje pompa (krzywa grzewcza + ograniczona
     nastawa z panelu). Add-on zapisuje do pompy tylko kroki testów, które uruchomisz, i zatwierdzone propozycje
     Doradcy; plan poniżej jest poglądowy.” (+ `pump_eq.note` bez zmian);
   - usuń `div.profiles` (:18-21) i wiersze „Optymalizator (poglądowo) — Komfort/Ekonomia” (:29-30);
   - usuń `div#heiko-cost` (:36);
   - trzy foldy (:42-53) → jeden `<details class="fold"><summary>Model i plan (poglądowo)</summary>` zawierający
     kolejno `h3`+`#heiko-savings`, `h3`+`#heiko-blocks` (klasa `history-table`), `h3`+`#heiko-model` (te same id —
     renderery w dashboard.js bez zmian). Wykres `#heiko-chart` i fold KPI bez zmian.
6. `static/app.css:141` `.stat-strip` — zostaje `repeat(3, …)` (3 kafelki); jeśli `h3` w foldzie potrzebuje stylu —
   `.fold-body h3 { font-size: 14px; margin: 12px 0 4px; }`.
7. Testy `tests/test_web.py`: usuń `test_today_summary_reports_actual_cost_from_meter` (:220-229);
   w `test_changes_api_shape_and_dashboard_counter` (:385) zamień asercję na: HTML `/` NIE zawiera „Zmiany nastaw
   pompy”, „Szac. oszczędność”, `cost-savings`, „niczego do niej nie zapisuje”; zawiera „Zapisy do pompy” i „brak”.
   Nowy test: z wstawionym testem `w_toku` (schedule z 2 krokami, step 0) i 1 propozycją `oczekuje` → HTML `/`
   zawiera tytuł testu, godzinę i wartość pierwszego kroku oraz „propozycje Doradcy: 1”. Sprawdź, czy inne testy
   (`grep -rn "today_summary\|heiko-cost\|Optymalizator (poglądowo)\|profiles" tests/`) nie wymagają aktualizacji.
- Komenda testu: `cd /config/addons/heiko_predictive_control/heiko_predictive_control && python -m pytest -q`
  (w worktree odpowiednia ścieżka).
- `DONE:` wszystkie testy zielone; `grep -rn "today_summary\|cost-savings\|param-changes\|refreshCost\|heiko-cost"`
  w `heiko_predictive_control/` zwraca 0 trafień (poza CHANGELOG); diff dotyka wyłącznie plików 1–7.
- `NIE RÓB:` nie ruszaj `cycle.py`, `db.py`, `publisher.py`, `main.py`, `experiments.py`, `advisor.py`, `day_summary.py`,
  schematu DB ani sensorów MQTT; nie zmieniaj `config.yaml`/CHANGELOG (robi `release`); bez commitu.

Po workerze: `claimcheck` → `verifier` (BASELINE `git status --short`, worktree, raport). REJECT → raz haiku-worker,
drugi → sonnet-worker.

## Wydanie (od razu, łańcuch bez przerw)
`simplify` → `code-review` (+ równolegle `silent-failure-hunter`, `pr-test-analyzer`, security general-purpose na
`4463672..HEAD`) → skill `release` (0.20.2, CHANGELOG: usunięte „Szac. oszczędność dziś”, „Zmiany nastaw pompy”,
linie modelu kosztu i endpoint `/api/today_summary`; nowa kafelka „Zapisy do pompy”; „Pompa dziś” = podłoga + CWU
z kafla Doba + wczoraj; karta pompy: prawdziwy opis, model i plan w jednym foldzie) → `ha_manage_updates` install z `backup=true`.

## Weryfikacja na żywo
playwright-ha `/35142871_heiko_predictive_control`: stopka v0.20.2; pasek 3 kafelki w jednym rzędzie (1440) i 1 kolumna
(390×844, bez poziomego scrolla); „Pompa dziś” = Doba podłoga + CWU (dziś ~3,20 zł), sub z wczoraj; „Zapisy do pompy”
pokazuje „Bezwładność −1 · następny krok 15:00 → −1” (albo kolejny krok, zależnie od godziny); karta pompy bez
„Optymalizator (poglądowo)” i Komfort/Ekonomia, jeden fold „Model i plan (poglądowo)” z 3 sekcjami; konsola bez
błędów po przeładowaniu; screenshoty `/home/claude/ha-config/playwright/heiko-0.20.2-{desktop,mobile}.png`.

## Rollback
`git revert` commitu → wydanie 0.20.3, albo przywrócenie add-onu z backupu z `ha_manage_updates`. Brak migracji danych.
