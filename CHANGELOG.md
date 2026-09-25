# Changelog

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
