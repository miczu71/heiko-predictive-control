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

- **A — Heiko (podłogówka):** optymalizacja kosztu. Krzywa grzewcza pompy wyłączona na
  stałe, setpoint ustawiany bezpośrednio w paśmie komfortu wokół wartości bazowej.
- **B — AC poddasze:** komfort w oknie pracy (domyślnie 8:00-16:00 dni robocze), koszt
  drugorzędny.

Każda pętla ma dwa profile — **Komfort** i **Ekonomia** — liczone równolegle, wybór
aktywnego profilu osobno per pętla. Każda pętla ma niezależny wyłącznik sezonowy —
wyłączony gasi tylko zapis do encji, add-on dalej zbiera dane i pokazuje dry-run
(co by ustawił + szacowaną oszczędność), żeby dało się ocenić przed włączeniem.

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

**0.3.0 — Etap 1 (Fundament) + pulpit z prywatnym układem domu.** Tylko odczyt. Zero
zapisów do pompy/AC — patrz `CHANGELOG.md`.

## Rozwój

Stack: Python 3.12, Flask (ingress web UI), paho-mqtt (discovery), APScheduler (cykl
decyzyjny co 15 min), SQLite (`/data/heiko_predictive_control.db` — historia cykli,
stan przełączników). Wzorzec add-onu spójny z innymi add-onami tego domu
(`fuel_tracker`, `pv_roi_tracker`, `nokia_tracker`).

```
pip install -r heiko_predictive_control/requirements.txt pytest pyyaml
cd heiko_predictive_control && pytest
```
