# Heiko Predictive Control

Home Assistant add-on: predykcyjne sterowanie pompą ciepła Heiko (podłogówka, cały dom)
i klimatyzacją poddasza pod taryfę G12w.

**Cel: rachunek za prąd.** Przesunięcie grzania podłogówki do tańszych godzin taryfy,
wykorzystując bezwładność wylewki jako darmowy magazyn ciepła. Osobno: wymuszony komfort
na poddaszu (klimatyzacja) w godzinach pracy (domyślnie dni robocze 8:00-16:00),
priorytet komfortu nad kosztem tam.

Pełny design i uzasadnienie architektury: dokument `BLUEPRINT_heiko_predictive_addon.md`
w repo konfiguracji Home Assistant tego domu (nie w tym repo — ten add-on jest jedną z
wielu integracji tamtej konfiguracji).

## Dwie pętle sterujące

- **A — Heiko (podłogówka):** optymalizacja kosztu przez natywne funkcje pompy: krzywa
  grzewcza + „ograniczona nastawa” z tygodniowym zegarem na szczyty G12w (ustawia użytkownik
  na panelu). Add-on **niczego nie zapisuje do pompy** — obserwuje skutek, uczy dwustanowy model
  domu, mierzy udział energii w szczycie i pilnuje komfortu (powiadomienie).
- **B — AC poddasze:** komfort w oknie pracy (domyślnie 8:00-16:00 dni robocze), koszt
  drugorzędny. **Steruje od 0.4.0:** przejmuje wyłączony klimatyzator, dogrzewa z
  wyprzedzeniem liczonym z uczonego tempa, utrzymuje temperaturę korygując offset nastawy,
  odpuszcza po ręcznej zmianie, pauzuje przy otwartym oknie i na urlop/święta lub gdy włączony
  jest ręczny przełącznik pauzy `attic_pause_entity` oraz wyłącza AC, gdy czujnik obecności
  pokazuje pustkę dłużej niż próg (szczegóły: `CHANGELOG.md`).

Każda pętla ma dwa profile — **Komfort** i **Ekonomia** — liczone równolegle, wybór
aktywnego profilu osobno per pętla. Każda pętla ma niezależny wyłącznik sezonowy —
wyłączony gasi tylko zapis do encji, add-on dalej zbiera dane i pokazuje dry-run
(co by ustawił + szacowaną oszczędność), żeby dało się ocenić przed włączeniem.

## Doradca — dane i katalog (0.8.0), propozycje bez zapisu (0.9.0)

Kierunek pętli A: **doradca z zatwierdzaniem** — add-on analizuje dane i parametry pompy, a zmiany
proponuje; wykona jedną zmianę dopiero po zatwierdzeniu w UI (późniejsze etapy). Wydanie 0.8.0 to
fundament, **bez żadnego zapisu do pompy**:

- **Katalog parametrów** pompy z klasami bezpieczeństwa A/B/C, zakresami i limitem kroku (`catalog.py`,
  `GET /api/catalog`); encje identyfikowane po sufiksie, bez pełnych identyfikatorów domu.
- **Telemetria** sensorów pompy i temperatur co 15 min (surowe 2 lata) oraz **dziennik każdej zmiany
  parametru** (panel, HA, automatyzacja) z licznikiem na pulpicie.
- **Dobowe streszczenia** pracy: sprężarka, cykle CWU, grzałki, pompa obiegowa, energia szczyt/poza szczytem.
- **Raport** (zakładka „Raport”): komfort, energia i udział szczytu, praca pompy wg temperatury,
  COP, hipotezy i luki w danych.
- **Propozycje (0.9.0, D2)** — zakładka „Doradca”: analizatory krzywej (eksperyment ±1 przy zapasie komfortu),
  CWU (histereza, wskazówka o porze względem taryfy) i anomalii (alerty). Obie soczewki, Komfort i Oszczędność,
  liczone obok siebie; wyróżniona soczewka decyduje o powiadomieniach. **„Zatwierdź” zapisuje tylko decyzję —
  niczego nie ustawia w pompie** (pierwszy zapis dopiero w D3). Ta sama propozycja się odświeża, nie dubluje;
  przy zatwierdzeniu analiza jest liczona ponownie. Odtworzenie zimy w „Raporcie” pokazuje, co analizator krzywej
  zaproponowałby w zeszłym sezonie.
- **Minimum komfortu z wybranych pokoi (0.9.0)** — w Opcjach wybierasz, które strefy dzienne liczą się do minimum
  (bezpiecznik, alarm, ocena propozycji); średnia stref zawsze ze wszystkich.

| Opcja | Znaczenie |
|---|---|
| `comfort_min_entities` | strefy liczone do minimum komfortu; puste = wszystkie |
| `advisor_lens` | wyróżniona soczewka i powiadomienia: `żaden` / `komfort` / `ekonomia` |
| `advisor_notify_service` | usługa `notify` telefonu, na który idą powiadomienia doradcy |
| `advisor_link_path` | ścieżka linku w powiadomieniu (otwiera add-on w aplikacji HA) |
| `advisor_anomaly_entities` | czujniki ostrzegawcze z HA (aktywny = alert), po przecinku |

## Pulpit

Izometryczny model domu z aktywnymi miejscami: pokoje z termometrami (podłoga
zabarwiona wg temperatury albo realistyczna z temperaturą w „pigułce"), klimatyzatory
(również kanałowe — nawiewy w pokojach) i pompa ciepła. Dotknięcie otwiera kartę
szczegółów; wartości odświeżają się co minutę (`GET /api/live`).

**Układ domu jest prywatny** — plik JSON w konfiguracji Home Assistant, wskazany opcją
`house_layout_file` (domyślnie `/homeassistant/heiko_predictive/house.json`, add-on ma
do niej dostęp tylko do odczytu). To repo zawiera jedynie silnik rysowania (`rooms.py`),
wczytywanie i walidację (`layout.py`) oraz neutralny przykład
[`example_house.json`](heiko_predictive_control/heiko_predictive_control/example_house.json)
— wzór formatu: kondygnacje, pokoje, urządzenia, opcjonalnie meble (`boxes`), schody,
okna/cegła/TV na ścianach (`walls`), otwory w ścianach przednich (`fronts`).

## Status

**0.9.0 — Doradca, etap D2: silnik propozycji i zakładka „Doradca”** (zero zapisów do pompy, decyzje tylko próbne).
0.8.x — D1: dane i katalog (telemetria, dziennik zmian parametrów, dobowe streszczenia, raport). Poprzednio 0.7.0 — Etap 3b′: pętla A obserwuje natywny mechanizm pompy** (krzywa grzewcza + „ograniczona
nastawa” z zegarem, ustawiana na panelu): cel wody wg pompy, udział energii w szczycie vs zima,
uczenie bezwładności z wymuszenia, alarm komfortu; zero zapisów do pompy. (0.6.0: model
podłogówki i poglądowy plan optymalizatora.) Pętla B (AC poddasza) zapisuje do klimatyzatora
od 0.4.0 (tylko gdy `attic_enabled`). Patrz `CHANGELOG.md`.

## Rozwój

Stack: Python 3.12, Flask (ingress web UI), paho-mqtt (discovery), APScheduler (cykl
pętli A co 15 min, pętli B co 5 min), SQLite (`/data/heiko_predictive_control.db` — historia cykli,
stan przełączników). Wzorzec add-onu spójny z innymi add-onami tego domu
(`fuel_tracker`, `pv_roi_tracker`, `nokia_tracker`).

```
pip install -r heiko_predictive_control/requirements.txt pytest pyyaml
cd heiko_predictive_control && pytest
```
