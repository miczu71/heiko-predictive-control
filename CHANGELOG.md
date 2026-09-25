# Changelog

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
