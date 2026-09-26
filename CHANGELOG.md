# Changelog

## 0.7.0 — Etap 3b′: obserwacja natywnego mechanizmu pompy (krzywa + ograniczona nastawa)

**Zmiana podejścia pętli A.** Zamiast zapisywać nastawę wody, add-on **obserwuje** to, co robi
pompa: krzywa grzewcza (włączana przez użytkownika na starcie sezonu) i natywna „ograniczona
nastawa” z tygodniowym zegarem (ustawiana na panelu pompy). **Nadal zero zapisów do pompy** —
gwarantuje to test strukturalny (analiza AST) obejmujący wszystkie moduły pętli A.

- **Cel wody wg pompy.** Cykl zapisuje aktualny cel wody z pompy (obejmuje krzywą, przesunięcie i
  ograniczenie) oraz stan krzywej. Karta pompy pokazuje ten cel zamiast nastawy stałej, która przy
  włączonej krzywej jest niedostępna.
- **Ograniczona nastawa — wnioskowana.** Zegar nie jest czytelny z HA, więc aktywność wnioskujemy:
  przy **włączonej** krzywej cel wody o ≥ 0,5°C poniżej krzywej (uwzględnia przesunięcie krzywej).
  Przy wyłączonej krzywej stan jest „nieznany”.
- **Bezwładność z wymuszenia.** Przejścia ograniczenia włączone↔wyłączone to wymuszenie, którego
  brakowało zimowym danym. Swobodna regresja (c, τ) jest dopuszczona dopiero przy ≥ 6 przejściach i
  średnim spadku ≥ 1°C oraz gdy jest o ≥ 10% lepsza od „nic się nie zmieni”; wtedy model dostaje
  status **zidentyfikowany** i znika ostrzeżenie o orientacyjnych oszczędnościach. Zidentyfikowany
  model nie jest nadpisywany przez dane bez wymuszenia.
- **KPI zmierzone: udział energii w szczycie** (ostatnie 7 dni) porównany z zimą sprzed sterowania
  **przy tym samym rozkładzie temperatur zewnętrznych** (klasy co 3°C). Baza z długoterminowych
  statystyk ostatniego sezonu liczona raz po starcie. To właściwa miara przesunięcia — nie zależy od
  modelu. Energia łącznie z CWU po obu stronach; święta liczone jak dni robocze.
- **Alarm komfortu** — powiadomienie (`notify_service`), gdy najzimniejszy pokój jest poniżej
  `heiko_room_min_c` przez ≥ 30 min; jedno na epizod. Podpowiada wyłączenie ograniczenia na panelu.
  Tylko powiadomienie, bez akcji na pompie.
- Optymalizator z 0.6.0 zostaje jako **poglądowy benchmark** („co by zrobił”), nie jako sterowanie.

| Encja MQTT | Znaczenie |
|---|---|
| Heiko: cel wody wg pompy | aktualny cel wody, °C |
| Heiko: ograniczona nastawa (wnioskowana) | aktywna / nieaktywna / nieznany |
| Heiko: udział energii w szczycie, 7 dni | % |
| Heiko: udział w szczycie z zimy, ta sama pogoda | % (baza porównawcza) |

| Opcja | Domyślnie | Znaczenie |
|---|---|---|
| `heiko_water_setpoint_entity` | sensor celu wody pompy | aktualny cel wody |
| `heiko_curve_shift_entity` | `number` przesunięcia krzywej | uwzględniane przy wykrywaniu ograniczenia |

API: `GET /api/plan` zwraca też `kpi` i `kpi_baseline`. Baza: kolumny `water_setpoint_c`, `curve_on`,
`reduced_active` (migracja automatyczna).

## 0.6.0 — Pętla A (faza cienia): model podłogówki i plan pod taryfę

Etap 3a. **Nic nie jest zapisywane do pompy** — pętla A nadal tylko obserwuje, ale
zamiast błędnego dry-runu liczy prawdziwy plan i uczy model domu.

**Naprawiony błąd dry-runu.** Dotychczasowa symulacja dodawała pasmo komfortu *pokoju*
(±1–2°C) do nastawy *wody* grzewczej, więc wyświetlane „symulowane nastawy” i oszczędności
nie miały sensu. Usunięte (`simulated_setpoint_c`, `simulate_cost_increment_pln`, model
`k_loss`/`k_gain`).

**Model dwustanowy** (`floor_model.py`): ciepło z pompy przechodzi przez filtr o stałej
czasowej `τ` (bezwładność wylewki), potem grzeje pokój; parametry `τ`, `g` (zysk), `c`
(strata) i `e` (moc grzania na K różnicy nastawa−pokój). Nowy model zastępuje poprzedni
tylko, gdy prognoza 6 h ma błąd ≤ 0,5°C i nie jest gorsza od „temperatura się nie zmieni”.

**Czego zimowe dane NIE rozstrzygają.** Dom regulowany termostatem (moc grzania podąża za
stratami) daje regresory skorelowane na −0,9…−1: swobodna regresja `g`/`c` zwraca wartości
absurdalne (stała czasowa domu ~250 h, R² < 0,15). Dlatego dopasowanie idzie inaczej:

- **Bilans energii** wyznacza pewnie `R = g/c` (odwrotność strat UA: średni napęd
  `Tr−To` ÷ średnia moc cieplna).
- **Bezwładność** (`c`, `τ`) dobierana z siatki po błędzie prognozy 6 h; do planu trafia
  `c` **konserwatywne** — największe, którego błąd mieści się w 10% od najlepszego
  (zyski słoneczne i wewnętrzne zaniżają `c`, więc ostrożniej zakładać mniej bezwładności).
- `e` (moc grzania) wyznaczana z regresji przez zero — tu dane wystarczają.
- Etykieta źródła na pulpicie: *bilans energii + inercja z ograniczeń*. Dopóki inercja
  nie zostanie zmierzona przy sterowaniu aktywnym, oszczędności są **orientacyjne**.
- Wartości domyślne (bez dopasowania) opisują dobrze ocieplony dom z wylewką
  (`c = 0,01`, pojemność ~13 kWh/K). Wcześniejsze założenie „lekki dom” było obalone przez
  dane, a stary współczynnik `k_loss` z EWMA nie zasila już modelu.

- **Bootstrap** z długoterminowych statystyk ostatniego pełnego sezonu grzewczego
  (15.10–15.04): energia pompy, temperatury stref, temp. zewnętrzna. Raz po starcie,
  powtarzany do skutku (zapytania idą po jednej encji — jedno duże przekraczało limit czasu).
- **Dobowe dopasowanie** o 4:30 z własnych cykli (faza cienia). Czas CWU jest wyłączony
  z pomiaru (moc cieplna do domu = 0).

**Planer** (`floor_plan.py`): mini-MPC na 36 h w krokach 15 min. Dla każdego bloku taryfy
(szczyt/poza szczytem × dzień/noc, ≤ 8 h) wybiera przesunięcie nastawy wody względem
**równoważnika krzywej grzewczej** (interpolacja 5 punktów krzywej przy prognozowanej temp.
zewnętrznej), minimalizując koszt przy paśmie komfortu; wartość ciepła zmagazynowanego na
końcu horyzontu liczona po cenie taniej taryfy. Oba profile (Komfort/Ekonomia) liczone za
każdym cyklem. Dni robocze i święta z `workday.check_date`, prognoza z `weather.get_forecasts`.

**Uczciwe liczenie oszczędności.** Pulpit pokazuje osobno: oszczędność planu i *„w tym z
samego przesunięcia”* (ten sam plan przy średniej temperaturze nie niższej niż na krzywej
natywnej). Reszta oszczędności to chłodniejszy dom w granicach pasma, nie przesunięcie.

**Bezpiecznik pokoi.** Najzimniejszy pokój stref dziennych poniżej `heiko_room_min_c`
(domyślnie 18,5°C) blokuje obniżanie nastawy w bieżącym bloku.

**Pulpit:** wykres przewidywanej temperatury (krzywa natywna vs profile, pasmo, szczyty
taryfy), plan bloków, oszczędność, jakość modelu (τ, c, g, e, błąd prognozy, błąd na żywo).
Koszt „realnie” liczony z licznika energii; model osobno. **MQTT:** nowe sensory modelu
(τ, błąd prognozy, źródło), najzimniejszego pokoju, bezpiecznika i oszczędności z przesunięcia.

| Opcja | Domyślnie | Znaczenie |
|---|---|---|
| `heiko_room_target_c` | `20.6` | środek pasma komfortu pokoi |
| `heiko_room_min_c` | `18.5` | minimum pojedynczego pokoju (bezpiecznik) |
| `heiko_water_min_c` / `heiko_water_max_c` | `20` / `32` | granice nastawy wody |
| `heiko_curve_ambient_entities` / `heiko_curve_water_entities` | encje `number` integracji | 5 par punktów krzywej (temp. zewn. → woda) |
| `heiko_water_temp_entity` | sensor skraplacza | temperatura wody grzewczej (uczenie) |
| `heiko_working_mode_entity` | sensor trybu pracy | rozróżnienie grzania od CWU |
| `heiko_bootstrap_outdoor_entity` | `sensor.openweathermap_temperature` | temp. zewn. z długą historią (bootstrap) |

Baza: nowe kolumny w `cycles` (`water_temp_c`, `heating_active`, `dhw_active`, `energy_kwh`,
`heat_kw`, `base_curve_c`, `plan_setpoint_c`, `model_err_c`, `min_room_c`, `min_room_name`)
— migracja automatyczna. Nowa zależność: `websocket-client` (statystyki LTS są dostępne
tylko przez WebSocket API).

## 0.5.0 — Czujnik obecności: nie grzej pustego poddasza

Nowa opcja `attic_presence_entity` (binary_sensor, on = ktoś jest) i próg
`attic_vacant_after_min` (domyślnie 60, 0 = wyłączone).

- **Pusto w oknie pracy.** Gdy czujnik pokazuje brak obecności dłużej niż próg, add-on
  **wyłącza klimatyzator** i oddaje sterowanie (faza **pusto — nikogo na poddaszu**). Licznik
  pustki startuje najwcześniej **od początku okna pracy** — dogrzewanie przed 8:00 nie czeka
  na obecność, bo wtedy nikt jeszcze nie musi być na miejscu. Dzień, w którym nikt nie
  przyszedł, kończy się więc po godzinie od startu okna.
- **Powrót.** Z obecnością zadziała zwykłe przejęcie (dogrzewanie ~4°C/h, gdy chłodno);
  poddasze może być przez chwilę chłodniejsze po dłuższej pustce.
- **Bezpiecznik.** Czujnik niedostępny, nieznany albo brak encji = reguła wyłączona (grzanie
  jak dotąd). Ręczne włączenie AC w czasie pustki zostaje nietknięte („włączony ręcznie").
- Próg jest długi celowo: czujnik mmWave ma zaniki 2–15 min, gdy ktoś siedzi nieruchomo
  (historia z 9 dni).
- Powiadomienie o starcie grzania (`notify_service`) tylko raz dziennie, nie po każdym
  powrocie z pustki.
- Pulpit: wiersz **Obecność** („jest" / „brak od 48 min"); Opcje: pola encji i progu.

| Opcja | Domyślnie | Znaczenie |
|---|---|---|
| `attic_presence_entity` | `""` | czujnik obecności; brak = reguła wyłączona |
| `attic_vacant_after_min` | `60` | minuty pustki wyłączające AC (od początku okna pracy); 0 = wyłączone |

Baza: kolumny `presence` i `vacant_min` w `cycles` (migracja automatyczna).

## 0.4.3 — Ręczna pauza dogrzewania poddasza + pola encji w Opcjach

**Pauza z HA.** Nowa opcja `attic_pause_entity` — dowolna encja `input_boolean`/`switch`/
`binary_sensor`: **włączona = AC poddasza nie grzeje** (urlop, wyjazd, święta „na
życzenie"). Działa razem z sensorem urlopu i `binary_sensor.workday` — pauza obowiązuje,
gdy włączy ją którekolwiek źródło.

- Włączenie w trakcie okna pracy, gdy add-on grzeje: klimatyzator jest wyłączany, a
  sterowanie oddawane (jak przy końcu okna). Nowa faza **wstrzymane (urlop / pauza)**.
- Włączona poza oknem pracy: w najbliższym oknie add-on nic nie robi.
- Encja niedostępna lub nieistniejąca = brak pauzy (grzanie nie zatrzyma się przez usterkę).
- Ręczna zmiana AC z tego dnia nadal obowiązuje po wyłączeniu pauzy (odpuszczenie do
  końca dnia).
- Faza przy urlopie zmieniona z „poza oknem" na „wstrzymane" (dzień roboczy, urlop).

**UI Opcje** (bez restartu, od następnego cyklu): pola `attic_pause_entity`,
`attic_vacation_entity`, `attic_window_entities` i `notify_service` — wcześniej dało się je
ustawić tylko przez API, bo opcje Supervisora zasiewają bazę add-onu tylko raz.

| Opcja | Domyślnie | Znaczenie |
|---|---|---|
| `attic_pause_entity` | `""` | przełącznik pauzy dogrzewania (on = AC nie grzeje) |

## 0.4.2 — Czyszczenie nieaktualnych wartości MQTT

- Encje **AC poddasze: planowany start dogrzewania** i **ostatnia nastawa zlecona**
  zostają puste (unknown), gdy wartość znika (koniec okna pracy, zwolnienie AC). Wcześniej
  pominięta publikacja zostawiała stary, retained stan z poprzedniego dnia.

## 0.4.1 — Poprawki po wdrożeniu 0.4.0

- **„Ostatni cykl" był `unavailable`** (błąd z 0.3.0): timestamp bez strefy czasowej jest
  odrzucany przez HA. Teraz publikowany ze strefą. Dodany osobny heartbeat pętli B —
  encja **AC poddasze: ostatni cykl** (timestamp, odświeżana co cykl pętli B), do
  watchdoga po stronie HA.
- **Planowany start dogrzewania** nie jest już pokazywany po zakończeniu okna pracy
  (wcześniej po 16:00 wisiało dzisiejsze 08:00).
- **Offset nastawy na pulpicie** pokazuje wartość domyślną (+1,5°C), gdy stan sterowania
  nie został jeszcze utrwalony (tryb obserwacji), spójnie z MQTT.

## 0.4.0 — Etap 2: pętla B steruje klimatyzacją poddasza

Pierwszy **zapis** add-onu: klimatyzator poddasza (`climate.*`) w oknie pracy. Pętla A
(Heiko/podłogówka) nadal tylko odczyt.

**Jak działa.** AC reguluje samo, add-on koryguje *offset* nastawy względem termometru
pokoju (czujnik klimatyzatora zwykle zawyża temperaturę):

- **Start dogrzewania** liczony z deficytu i uczonego tempa grzania:
  `wyprzedzenie = deficyt / tempo + margines` (Komfort 20 min, Ekonomia 10 min), nie
  więcej niż `attic_preheat_max_min`. Bez odczytu temperatury: `attic_preheat_lead_min`.
- **Dogrzewanie:** tryb `heat`, nastawa `cel + offset + 2°C`. Po osiągnięciu `cel − 0,3°C`
  → **utrzymanie**: nastawa `cel + offset`, offset uczony co cykl
  (`+0,3 × (cel − T)`, martwa strefa ±0,2°C, zakres −1…+4°C, dopiero 10 min po zapisie).
  Tempo dogrzewania uczone EWMA (α = 0,3, 1–10 °C/h).
- **Ochrona:** `T > cel + 0,7°C` → AC wyłączone (nadal przejęte), `T < cel − 0,3°C` → grzanie.
- **Przejęcie tylko wyłączonego AC.** Włączone przez człowieka przed oknem = ręczne użycie,
  add-on go nie dotyka.
- **Ręczna zmiana** (tryb/nastawa/wyłączenie, po 2 min karencji od własnego zapisu) →
  add-on odpuszcza **do końca dnia**.
- **Otwarte okno** (`attic_window_entities`) dłużej niż 2 min → pauza, po zamknięciu
  powrót. Drzwi tylko w logu.
- **Koniec okna / dzień nieaktywny** (weekend, urlop, brak `binary_sensor.workday`) →
  jeśli AC było przejęte: `climate.turn_off`, w przeciwnym razie zero zapisów.
- **Wyłączenie sterowania** (`attic_enabled` = off) przy przejętym i włączonym AC →
  jednorazowe `turn_off` (żeby nie zostawić go grzejącego do rana); poza tym tylko dry-run.
- **Zapis tylko przy realnej różnicy** (nastawa ≥ 0,5°C, tryb), najwyżej raz na ~4 min,
  twardy zakres 16–30°C. Nieudane wywołanie usługi = ponowna próba w następnym cyklu.
- Powiadomienia (`notify_service`): start ogrzewania danego dnia, ręczne przejęcie,
  błąd zapisu.

**Nowe opcje**

| Opcja | Domyślnie | Znaczenie |
|---|---|---|
| `attic_window_entities` | `""` | czujniki okien poddasza, po przecinku; otwarte >2 min = pauza |
| `attic_vacation_entity` | `""` | `binary_sensor` urlopu (on = AC nie startuje) |
| `attic_preheat_max_min` | `120` | górny limit wyprzedzenia dogrzewania |
| `attic_cycle_interval_min` | `5` | cykl pętli B (pętla A: `cycle_interval_min`, 15) |

`attic_preheat_lead_min` to teraz tylko wyprzedzenie awaryjne (brak czujnika).

**Nowe encje MQTT** (urządzenie „Heiko Predictive Control")

| Encja | Znaczenie |
|---|---|
| AC poddasze: faza | `poza oknem`, `czeka`, `dogrzewanie`, `utrzymanie`, `pauza`, `ręczna zmiana`, … |
| AC poddasze: przejęte przez add-on | binary, czy add-on aktualnie steruje AC |
| AC poddasze: offset nastawy (uczony) | °C |
| AC poddasze: tempo dogrzewania (uczone) | °C/h |
| AC poddasze: ostatnia nastawa zlecona | °C |
| AC poddasze: planowany start dogrzewania | timestamp |
| AC poddasze: energia dziś / koszt dziś | kWh / PLN (całkowanie mocy chwilowej × cena taryfy) |

Encje istniejące bez zmian. Pulpit: karta poddasza pokazuje fazę, przejęcie, planowany
start, nastawę, offset, okna i koszt dziś; Statystyki — kolumny fazy i zapisu.

**Baza.** Migracja dokłada kolumny do tabeli `cycles` (historia zachowana). Stan
sterowania w `settings["attic_ctrl_state"]`.

**Zmiany wewnętrzne.** Logika w nowym module `attic.py` (funkcje czyste, 42 testy, w tym
symulacja poranka); `cycle.run_attic_cycle` to cienki orkiestrator I/O; pętle A i B
mają osobne zadania harmonogramu. `attic_should_run` usunięte.

## 0.3.0 — Prywatny układ domu + realistyczne detale modelu

**Prywatność.** Układ domu (pokoje, encje, urządzenia, meble) nie jest już zaszyty w
kodzie tego publicznego repo. Add-on czyta go z pliku JSON w konfiguracji Home
Assistant (`map: homeassistant_config:ro`):

| Opcja | Domyślnie | Znaczenie |
|---|---|---|
| `house_layout_file` | `/homeassistant/heiko_predictive/house.json` | plik układu domu; brak/błąd → neutralny przykładowy dom + ostrzeżenie na pulpicie |

Repo zawiera tylko silnik rysowania i neutralny przykład `example_house.json`.
Domyślne encje w opcjach add-onu zamienione na przykładowe (`sensor.salon_temperature` itp.)
— istniejące instalacje zachowują swoje zapisane opcje.

**Realistyczne detale** (opcjonalne, sterowane z pliku układu):

| Element JSON | Co rysuje |
|---|---|
| `rooms[].floor_style: "wood"` | podłoga w deskę; temperatura w „pigułce" nad podłogą |
| `boxes[]` | meble jako bryły: `graphite`, `black`, `oak`, `cherry`, `fabric`, `neutral`, osobny materiał blatu (`top`) |
| `stairs[]` | bieg schodów ze stopniami |
| `walls[]` (`x0`/`y0`) | okno, cegła, TV, pas farby na tylnych ścianach |
| `fronts[]` | otwór w niewidocznej ścianie przedniej (np. drzwi tarasowe) jako obrys-duch |
| `rooms[].note`, `equipment[].note` | opis na karcie szczegółów |

Walidacja układu przy starcie (pokrycie rzutu, nakładanie, obrys vs pola, nawiewy w
istniejących pokojach). Karty szczegółów nie znają już kluczy konkretnego domu.
Logika sterowania bez zmian, nadal zero zapisów do pompy/AC.

## 0.2.3 — Pulpit: czytelność ikon urządzeń

Ikona pompy ciepła na jednostce zewnętrznej z przerywaną linią przez ścianę do
jednostki wewnętrznej; ikona klimatyzatora zawieszonego wysoko z pionową linią do
miejsca na rzucie. Uogólniony mechanizm linii prowadzącej (`leader_to`).

## 0.2.2 — Pulpit: urządzenia zgodnie z rzeczywistością

Obsługa klimatyzacji kanałowej (bez ikony jednostki, nawiewy w wybranych pokojach —
każdy otwiera kartę tej klimatyzacji), pokoi o kształcie innym niż prostokąt (obrys +
dodatkowe prostokąty), urządzeń z drugą jednostką połączoną linią. Rozmieszczenie
dopasowane do rzeczywistego domu.

## 0.2.1 — Pulpit: rozmieszczenie pokoi

Poprawione przypisanie pokoi do miejsc na rzucie; klatka schodowa jako osobny typ
pomieszczenia (etykieta „Schody", osobny kolor tła).

## 0.2.0 — Pulpit: izometryczny model domu

Nowy ekran główny: **model domu w rzucie izometrycznym** (ręczne SVG, bez biblioteki 3D),
rozsunięte kondygnacje, poddasze z dachem (tylna połać z panelami PV, przednia zdjęta).

- Aktywne miejsca: pokoje z termometrami (podłogi zabarwione wg temperatury:
  < 19,5 / 19,5–21 / 21–23,5 / ≥ 23,5°C), klimatyzatory (sterowane i tylko podgląd),
  pompa ciepła; dotknięcie → karta szczegółów.
- Pasek u góry: taryfa teraz, realny koszt pompy dziś, szacowana oszczędność aktywnego
  profilu.
- Wartości odświeżane co minutę bez przeładowania strony — endpoint `GET /api/live`.
- Nowa paleta (skandynawski minimalizm), ikony SVG zamiast emoji, poprawiona nawigacja
  na telefonie.

Nadal zero zapisów do pompy/AC.

## 0.1.1 — Fix: 404 na /favicon.ico w konsoli przeglądarki

Drobna poprawka po weryfikacji Playwright — pusta odpowiedź 204 zamiast 404 dla
`/favicon.ico`, zero wpływu na logikę.

## 0.1.0 — Etap 1: Fundament (tylko odczyt)

Pierwsza wersja. Szkielet add-onu: ingress web UI (Pulpit/Statystyki/Opcje), MQTT
discovery (heartbeat + kilka sensorów diagnostycznych), odczyt stanu z Home Assistant
(taryfa G12w, temperatury stref dziennych, poddasze, pompa, klimatyzacja poddasza).

**Zero zapisów do pompy Heiko lub klimatyzacji poddasza.** Oba wyłączniki sezonowe
(`Heiko`, `AC`) domyślnie WYŁĄCZONE i nieaktywne w tej wersji — nie ma jeszcze ścieżki
kodu, która by cokolwiek zapisywała. Cykl decyzyjny (co 15 min) liczy i pokazuje
**dry-run**: jaki setpoint ustawiłby w profilu Komfort i w profilu Ekonomia, oraz
szacowaną oszczędność PLN/dzień względem dzisiejszego realnego kosztu — bez wpływu na
pompę/AC.

Model termiczny: jeden współczynnik strat cieplnych, uczony online (EWMA) z naturalnej
odpowiedzi domu na krzywą natywną pompy. Seed startowy — wartość fizycznie sensowna dla
domu ocieplonego, z podłogówką (wolna bezwładność), doszlifowywana co cykl.
