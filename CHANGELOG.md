# Changelog

## 0.23.0 — Tryb urlopowy, klima poddasza wyłączana po oknie pracy, kafel „Doba” od północy do północy
- **Nowe — tryb urlopowy (domyślnie wyłączony):** w oknie dat (`vacation_from`/`vacation_to`, włącznie) krzywa grzewcza dostaje przesunięcie bazowe − obniżka (1–3, domyślnie 2, zawsze obcięte do −3), a nastawa CWU spada do wartości urlopowej (domyślnie 20 °C). Po urlopie krzywa wraca do bazy, a CWU jednorazowo do 48 °C. Opcjonalnie włącza też `input_boolean.away_mode` (wyłącza tylko to, co sam włączył). Bezpiecznik: gdy najzimniejszy pokój jest poniżej progu (domyślnie 16 °C) przez 15 min, tryb wraca do bazy i wysyła powiadomienie do `notify.kacper` (histereza +1 °C). Start i koniec urlopu → `notify.family`. Ręczna zmiana krzywej wstrzymuje tryb do jutra. Kafel „Urlop” na Pulpicie, sekcja „Tryb urlopowy” w Opcjach, `GET /api/vacation`.
- **Zmiana — G13 w urlopie:** Szczyt G13 nie robi własnych zapisów (kafel: „urlop: szczyt −3”); docisk do −3 w szczycie `evening_peak` wykonuje tryb urlopowy **tylko przy włączonym trybie Szczyt G13** (`g13_peak_enabled`). Start testów jest w urlopie zablokowany, a klima poddasza traktuje urlop jak `attic_vacation_entity`.
- **Poprawka — klima poddasza nie wyłączyła się o 16:00 (09.11):** nieznany stan `binary_sensor.workday` oznacza teraz cykl bez komend (faza „brak dnia roboczego”), a nie dzień wolny, więc add-on nie zwalnia klimy po restarcie czy chwilowym braku sensora. Do tego klima włączona w oknie pracy przez kogoś innego jest o końcu okna wyłączana (z powiadomieniem `notify.family`); klima włączona po końcu okna zostaje nietknięta. Uwaga: reguła obejmuje każdy tryb pracy klimy włączonej w oknie (także chłodzenie).
- **Poprawka — kafel „Doba” zgadza się z panelem Energia:** dziś i wczoraj liczone z historii HA od północy do północy (run przez północ dzieli się między doby), a kWh pompy to licznik pompy z tej doby. Różnica do podłogi i CWU to nowa pozycja „Postój pompy”. Zakładka Koszty i rekordy w bazie bez zmian. `/api/day_summary` pobiera stany HA dopiero po chybieniu cache.
- **Nowe — zapis nastawy CWU:** allowlista zapisów obejmuje `dhw_setpoint` (16–60, krok 1). Zapis CWU wymaga integracji Heiko Heat Pump **1.14.3** (minimum encji 16) i restartu HA; na starszej integracji zapis CWU jest pomijany z ostrzeżeniem w logu, a krzywa działa dalej.
- **Poprawka (po przeglądzie):** zapis krzywej nie oznacza już zapisu CWU z tego samego cyklu jako „zastąpiony” (CWU jest potwierdzane osobno), a `last_write_id` nie wskazuje cudzego wiersza po odrzuceniu zapisu przez allowlistę.
- Testy: nowe dla trybu urlopowego (rdzeń, CWU, daty, Away, G13, widok, walidacja), klimy 16:00 i doby od północy.

## 0.22.4 — Szczyt G13: bezpiecznik dopiero od szczytu, nazwa pokoju w powiadomieniu i na kaflu
- **Poprawka — bezpiecznik nie działa w dogrzewaniu (13–16):** przesunięcie +1 grzeje bardziej, więc cofanie go do bazowego przy zimnym pokoju było bez sensu (a blokowało tryb do końca doby, także szczyt −3 o 16:00). Teraz bezpiecznik może zadziałać dopiero od początku szczytu. Licznik zimna liczy się od dogrzewania, więc gdy pokój jest poniżej progu od ≥15 min w chwili startu szczytu, tryb od razu zostaje na bazowym (bez zapisu −3 i cofania).
- **Nowe — nazwa pokoju:** powiadomienie („Szczyt G13: Salon ma 20,1 °C — poniżej 20,5 °C przez 15 min …”) i kafel „Szczyt G13” podają, który pokój był najzimniejszy (z ostatniego cyklu; bez nazwy: „najzimniejszy pokój”).
- **Migracja (jednorazowa):** przy pierwszym starcie czyści dzisiejsze zadziałanie bezpiecznika ze starej logiki, żeby tryb mógł wrócić do pracy tego samego dnia. Aktualizacja w trakcie szczytu czyści też prawdziwe zadziałanie z tego dnia — przy dalszym zimnie bezpiecznik zadziała ponownie po 15 min (drugie powiadomienie).
- Testy: nowe dla dogrzewania, startu szczytu, nazwy pokoju, widoku i migracji.

## 0.22.3 — Pulpit: kafle „Szczyt G13” i „Ogon grzania” w jednym kompaktowym pasku
- **Zmiana — Pulpit:** oba kafle stoją obok siebie w dwóch kolumnach (na telefonie jedna pod drugą), z niższym paddingiem i mniejszym nagłówkiem; zawartość i „Szczegóły” bez zmian.

## 0.22.2 — kolory stref G13 na paskach energii, zwijane kafle „Szczyt G13” i „Ogon grzania”
- **Poprawka — szare paski w „Doba: koszt ogrzewania” (i na stronie Koszty):** od 0.22.0 strefy pochodzą z pv_roi z nazwami G13 (`other`, `morning_peak`, `evening_peak`), których paleta nie znała — paski były szare, a legenda pokazywała „sieć other”. Teraz: poza szczytem = zielony, szczyt poranny = żółty, szczyt popołudniowy = czerwony, z polskimi etykietami.
- **Zmiana — Pulpit:** kafle „Szczyt G13” i „Ogon grzania” pokazują tytuł, odznakę i jedno zdanie (następna zmiana / stan odliczania); tabele energii i kosztu, liczniki odcięć i link do Opcji są pod „Szczegóły”.

## 0.22.1 — odcinanie „ogona”: licznik nie zeruje się przy spadku Tc, domyślnie margines 0 i 120 min
- **Zmiana — odliczanie jak w starej automatyzacji Pompa_fix:** startuje przy pierwszym `Tc > nastawa + margines` i nie zeruje się, gdy Tc chwilowo spadnie. Pompa robi „piłę” Tc (ok. 55 min, sprężarka nie staje), więc wymóg nieprzerwanego przebywania powyżej progu rzadko się spełniał. Odliczanie zeruje nadal wyjście z grzania, tryb inny niż Auto, pokój poniżej minimum, brak danych, trwający test i pauza.
- **Zmiana — domyślne wartości:** margines nad nastawą 0 K (było 1,0), opóźnienie 120 min (było 45); zakres marginesu 0…5 K (było 0,2…5). Już zapisane opcje nie zmieniają się same — wartości ustawia się w zakładce Opcje.
- **Poprawka:** po restarcie add-onu albo przerwie w odczytach dłuższej niż 15 min odliczanie startuje od nowa (stary znacznik nie daje natychmiastowego odcięcia). Kafel na Pulpicie pokazuje „Odliczanie N min” zamiast „Ogon trwa”.
- Opcja nadal domyślnie wyłączona (`tail_cut_enabled`); odcięcie wymaga pokoju ≥ „minimum pokoju”, po odcięciu obowiązuje „przerwa po odcięciu”.

## 0.22.0 — jedno źródło taryf (pv_roi), szczyt G13 w zakresie ±3, opcjonalne odcinanie „ogona” grzania

**Uwaga przy aktualizacji — usunięte opcje Supervisora:** `tariff_state_entity` i `tariff_price_entity` (stary sensor G12w). Taryfa pochodzi wyłącznie z harmonogramu pv_roi; stare wartości w opcjach add-onu są ignorowane. Nowa opcja `pv_roi_url` (domyślnie `http://29a4454d-pv-roi-tracker:8099`) służy do pobrania historii stref. Wymaga pv_roi 0.56.6 lub nowszego.

- **Zmiana — szczyt G13 mieści się w zakresie pompy −3…+3:** domyślne przesunięcie szczytu to −3 (wcześniej −4 było odrzucane przez pompę). Zapisana wartość poniżej −3 jest przy starcie obcinana do −3; zapis spoza ±3 jest odrzucany z ostrzeżeniem i wpisem w „Zapisach do pompy”. Powiadomienie o niepotwierdzonym zapisie nie mówi już o „przywróceniu”.
- **Zmiana — G13 z okien pv_roi:** szczyt to godziny strefy `evening_peak` z harmonogramu (święta, weekendy i lato bez ręcznych wyjątków); dogrzewanie zaczyna się 3 h przed szczytem. Dzień bez takiej strefy albo bez harmonogramu = brak zapisów.
- **Zmiana — koniec zgadywania taryfy z zegara:** godzina bez wiersza w harmonogramie to jawnie „brak danych taryfy” (koszt bez wyceny, KPI pomija godzinę). Cykl Heiko i plan są pomijane, gdy pv_roi milczy dłużej niż ~12 h. Plakietka na Pulpicie: „nieaktualne od …” (ostatni znany harmonogram) albo „brak danych taryfy”.
- **Zmiana — baza KPI v4:** liczona ze stref pv_roi (klient historii, strony po 168 h), z sygnaturą taryfy; zmiana stref/taryfy = ponowne przeliczenie. Stara baza zostaje do udanego przeliczenia, ponowna próba co godzinę.
- **Nowe — „Ogon grzania” (domyślnie wyłączone):** po `tail_delay_min` minutach, gdy Tc stoi ponad nastawą + `tail_margin_k`, add-on przełącza pompę na CWU, a po krótkiej przerwie wraca do Auto. Opcje w zakładce Opcje, kafel na Pulpicie, zapisy w „Zapisach do pompy” ze źródłem „add-on (tail_cut)”. Włącza użytkownik (`tail_cut_enabled`). Watchdog w HA (automatyzacja poza add-onem) przywraca Auto, gdy add-on milczy.
- **Zmiana — paleta stref paska „Doba”:** tania zielona, średnia bursztynowa, droga czerwona, PV niebieskie; w obu motywach średnia i droga są rozróżnialne.
- **Poprawka — walidacja `POST /api/settings`:** zakresy G13 i `tail_*` oraz `pv_roi_url` (tylko `http(s)://host`) sprawdzane po stronie serwera, błąd = 400 bez zapisu.
- Znane (nieblokujące): odcinanie ogona czeka na potwierdzenie pompy do ~140 s pod wspólnym zamkiem zapisów; ręczna zmiana trybu pompy w trakcie przerwy CWU jest nadpisywana powrotem do Auto; odliczanie ogona nie wygasa po restarcie; puste pole liczbowe w Opcjach nie przywraca wartości domyślnej; G13 wymaga pełnej doby w harmonogramie (przy braku jednej godziny tryb czeka do północy).
- Testy: 921 (wcześniej 801).

## 0.21.2 — Pulpit pokazuje aktywne przesunięcie trybu „Szczyt G13”

- **Poprawka — kafel „Zapisy do pompy”:** gdy działa tryb Szczyt G13 (bez testu), pokazuje „Szczyt G13 · +1” z fazą („przedgrzewanie do 16:00”, „szczyt do 21:00”) zamiast „brak / pompa na własnej krzywej”. Kafel nadal pokazuje test w toku, gdy taki jest.
- **Nowe — kafel „Szczyt G13”:** linia „Teraz: przesunięcie +1 (przedgrzewanie do 16:00)” z ostatniego zapisu trybu (tylko w dzień roboczy, w oknie 13–21); nagłówek wspomina przedgrzewanie 13–16.
- Pokazywane jest przesunięcie krzywej jako wartość bezwzględna (bazowe + dogrzanie/szczyt) z dopiskiem „bazowe X”; dane z wczoraj są ignorowane, a awaria odczytu stanu trybu nie psuje Pulpitu. Poza oknem, w dzień wolny, przy wstrzymanym/zablokowanym trybie i przy teście w toku kafel mówi jak dotąd „brak” albo pokazuje test.
- Testy: 801 (wcześniej 788).

## 0.21.1 — szczyt popołudniowy G13 na Pulpicie i w KPI

- **Poprawka — kafel taryfy na Pulpicie:** pokazuje „Szczyt poranny”, „Szczyt popołudniowy” albo „Poza szczytem” (strefa i cena brutto z harmonogramu pv_roi), zamiast jednego „SZCZYT” ze starego sensora G12w. Przy nieaktualnym harmonogramie zostaje poprzedni odczyt.
- **Zmiana — KPI „Udział energii w szczycie popołudniowym (stawka X zł/kWh)”:** do szczytu liczy się tylko strefa `evening_peak` (jedyna naprawdę droga), nie poranny szczyt. Tytuł pokazuje bieżącą stawkę szczytu popołudniowego. Baza z zimy liczona jest od nowa (okno 16–21 w dni robocze) i przez chwilę po starcie pokazuje „baza w trakcie liczenia”.
- Znane: okno 16–21 w bazie i dla godzin bez wiersza w `tariff_hours` to zimowe przybliżenie; docelowo wszystkie taryfy z pv_roi — `docs/ROADMAP_tariff_single_source.md`.

## 0.21.0 — tryb „Szczyt G13” i naprawa wyniku testu Bezwładność

- **Nowe — tryb „Szczyt G13” (domyślnie wyłączony):** w dni robocze (wg harmonogramu taryfy) o 13:00 przesunięcie krzywej = bazowe + dogrzanie (+1), o 16:00 bazowe − 4, o 21:00 powrót do bazowego. Bezpiecznik: najzimniejszy pokój stref dziennych poniżej progu (domyślnie 20,5 °C) przez 15 min → powrót do bazowego do 21:00 i powiadomienie. Zapis tylko przez istniejącą ścieżkę z potwierdzeniem ramką i wpisem w `pump_writes`; skok do ±4 dozwolony wyłącznie dla przesunięcia krzywej w tym trybie. Ręczna zmiana przesunięcia, odrzucony zapis albo brak potwierdzenia wstrzymują tryb do jutra. Tryb nie działa w trakcie testu, test nie wystartuje przy włączonym trybie, a główny wyłącznik „Sterowanie aktywne (Heiko)” gasi też tryb.
- **UI:** kafel „Szczyt G13” na Pulpicie (stan, następna zmiana, energia i koszt podłogówki w strefie szczytu dziś i z 7 dni, zadziałania bezpiecznika), sekcja w Opcjach (przełącznik, próg pokoi, przesunięcia 13–16 i 16–21), odznaka „model zidentyfikowany / niezidentyfikowany” przy teście Bezwładność.
- **Poprawka — test Bezwładność:** wymuszenie liczone było na 14 dniach cykli, więc przerwane testy je rozmywały i swobodne dopasowanie modelu się nie uruchamiało. Refit ocenia teraz także okno ostatniego testu; wynik zakończonego testu jest jednorazowo przeliczany po starcie. Test z wystarczającym wymuszeniem liczy się jako wykonany, Doradca nie proponuje go ponownie przez 30 dni.
- **Odporność:** powrót do bazowego po wyłączeniu trybu jest ponawiany, gdy HA odrzuci zapis (jedno powiadomienie na dobę); zapis przyjęty przez pompę mimo błędu usługi nie jest traktowany jak ręczna zmiana; puste opcje `g13_peak_*` = wartości domyślne; ostrzeżenie w logu przy braku `advisor_notify_service`.
- Testy: 788 (wcześniej 706).
- Znane (nieblokujące): zapis trybu unieważnia też niepotwierdzone zapisy slotu 52 (`supersede`); ręczna zmiana przed pierwszym zapisem trybu nie jest wykrywana; bezpiecznik liczy 15 min po próbkach cykli (co 15 min); `recheck` po jednorazowym przeliczeniu nie sprawdza, czy ocenione było okno testu; brak testu nieudanego refitu w `recheck_inertia`; po utracie harmonogramu taryfy w oknie pompa zostaje na −4 do 21:00 bez powiadomienia.

## 0.20.3 — usunięcie nieużywanych kolumn kosztu symulowanego

- **Usunięte:** kolumny `baseline_cost_today_pln`, `sim_cost_today_komfort_pln` i `sim_cost_today_ekonomia_pln` w tabeli `cycles`. Po 0.20.2 nikt ich nie czytał (sensory HA liczą się z planu), a pętla liczyła je co cykl na darmo. `/api/history` nie zwraca już tych trzech pól.
- **Migracja jednokierunkowa:** przy starcie `DROP COLUMN` usuwa kolumny z istniejącej bazy (idempotentnie, dane pozostałych kolumn zostają). Wersja ≤0.20.2 nie zapisze cyklu do zmigrowanej bazy („no such column”), więc cofnięcie wymaga przywrócenia add-onu z backupu, nie samego zainstalowania starej wersji.
- Testy: migracja starej bazy, idempotencja, zapis cyklu po migracji, świeża baza bez tych kolumn.

## 0.20.2 — Pulpit bez symulowanej oszczędności, z kafelką zapisów do pompy

- **Usunięte:** kafelki „Szac. oszczędność dziś” i „Zmiany nastaw pompy”, linie „Model — …” z kosztem planu oraz endpoint `/api/today_summary`. „Oszczędność” była różnicą dwóch symulacji, a pętla A nie zapisuje planu do pompy, więc realna oszczędność wynosiła 0, a liczba wprowadzała w błąd. Tabela 36 h, wykres planu i sensory HA zostają.
- **Nowa kafelka „Zapisy do pompy”:** trwający test (tytuł, godzina i wartość następnego kroku albo „ostatni krok wykonany”), „brak” gdy pompa działa na własnej krzywej, oraz link „propozycje Doradcy: N”.
- **„Pompa dziś”:** jedna liczba = podłoga + ciepła woda z kafla „Doba” (nowe pole `pump` w `/api/day_summary`), z podpisem „wczoraj: X zł”. Przy braku historii z HA pokazuje „—” zamiast „0,00 zł”.
- **Karta pompy:** prawdziwy opis tego, co add-on zapisuje do pompy (kroki testów i zatwierdzone propozycje Doradcy); bez przełącznika profili i wierszy „Optymalizator (poglądowo)”; trzy foldy scalone w „Model i plan (poglądowo)”.
- **Odporność:** nieczytelny plan testu nie wywala już Pulpitu (kafelka bez następnego kroku).
- Znane (nieblokujące): kolumny `baseline_cost_today_pln` / `sim_cost_today_*` nadal się zapisują bez odbiorcy (usunięte w 0.20.3); licznik propozycji nie uwzględnia wygaśnięcia; brak testów dla kroku P0, „ostatni krok wykonany” i nieznanego rodzaju testu.

## 0.20.1 — pasek pochodzenia energii wypełnia całą szerokość

- **Poprawka:** gdy zużycie składnika w dobie było poniżej 1 kWh (typowo klimatyzacja rano), segmenty paska pochodzenia energii zajmowały tylko część szerokości, a reszta była jasna jak tło. Segmenty mają teraz szerokość proporcjonalną do udziału, więc zawsze wypełniają cały pasek (kafel „Doba”, „Koszty”, /ogrzewanie).
- Test: `tests/test_source_bar_js.py` (node) pilnuje, by suma szerokości segmentów wynosiła 1.

## 0.20.0 — pochodzenie energii ze śladem baterii zamiast pasków stref taryfy

- **Pochodzenie każdej kWh:** kafel „Doba”, zakładka „Koszty” i strona /ogrzewanie pokazują, skąd naprawdę pochodzi energia podłogi, CWU i AC: sieć wg strefy taryfy z chwili ładowania baterii, PV albo „nieznane”, z adnotacją „przez baterię” i wyceną z chwili ładowania. Paski stref zużycia znikają z UI (pole `zones` zostaje w danych i API).
- **Skład baterii:** pętla A co cykl zapisuje skład baterii (tabela `battery_mix`, addytywna). Po starcie jednorazowy replay 7 dni z recordera HA odtwarza skład wstecz; zawartość baterii na początku okna to „nieznane” (wycena średnim kosztem, wypłukuje się w ok. dobę).
- **Przeliczenie:** rekordy CWU v8, podłoga v2, AC v4 — backfill przelicza ostatnie 7 dób (po replayu, w jednym łańcuchu startowym); starsze rekordy pokazują się jako „bez podziału”.
- API: `sources` ({origin: kwh, zł, kWh przez baterię}) w rekordach, `/api/day_summary` i `/api/costs`; `coverage.sources_from` dla CWU.
- Znane ograniczenia: `battery_mix` bez retencji (~96 wierszy/dobę), replay liczy w strefie czasowej procesu i bez licznika pompy (lekko zawyża „sieć”), brak testów DST i awarii zapisu replayu.
- Bez zapisów do pompy, klimatyzacji i HA.

## 0.19.0 — strefy taryfy per składnik w kaflu „Doba” i nowa zakładka „Koszty”

- **Kafel „Doba”:** pod każdym wierszem (podłoga, CWU, AC, Razem) pasek stref taryfy z legendą kWh · zł; energia rekordów bez podziału na strefy (starsze cykle CWU) pokazuje się jako szary segment „bez podziału”.
- **Zakładka „Koszty”:** przegląd kosztów podłogi, CWU i AC dla dowolnego zakresu dat (presety: 7 dni, 30 dni, ten i poprzedni miesiąc, sezon grzewczy; grupowanie auto/dzień/tydzień/miesiąc). Kafle KPI (zł, kWh, PV %) z różnicą względem poprzedniego okresu tej samej długości, wykres skumulowany zł/kWh wg składnika z linią T zewn., tabela stref taryfy oraz notka o zakresie danych (podłogówka od 19.10, strefy CWU od v7). Historia rośnie od zapisanych rekordów — bez odbudowy wstecz.
- API: `GET /api/costs?from=&to=&group=` (tylko odczyt, maks. 400 dni, `to` nie później niż dziś). Awaria doby „dziś” nie wyłącza już widoku — koszty liczą się z zapisanych rekordów.
- Bez migracji schematu i bez zapisów do pompy, klimatyzacji i HA.

## 0.18.0 — podłogówka: dziennik runów, kafel „Doba” na pulpicie i strona /ogrzewanie

- **Kafel „Doba” na stronie głównej:** dziś (na żywo) i wczoraj — podłogówka, CWU i klimatyzacja poddasza: kWh, koszt, rozkład na strefy taryfy i udział PV. Bez zapisów do pompy i klimatyzacji.
- **Nowa strona /ogrzewanie:** doba → strefy taryfy → pojedyncze runy podłogówki (kWh, zł, PV %, T zewn., T wody). Run = ciągłe `working_mode = 2`, przerwy ≤ 10 min scalane. Zapis nocą 00:30 i po starcie (backfill 7 dni); runy przez północ są kompletne dzięki 12-godzinnemu zapasowi okna.
- **Strefy taryfy w kosztach CWU i AC** (`zones`); rekordy CWU v7, AC v3 — backfill przelicza ostatnie 7 dni.
- **Poprawka:** brak serii rozładowania baterii nie jest już liczony jako PV — koszt wg taryfy i `split_complete: false`.
- API: `GET /api/day_summary?day=today|yesterday`, `GET /api/heating_runs?days=` (walidacja parametru).

## 0.17.5 — poddasze: koszt runu AC wg źródła energii (sieć / bateria / PV)

- **Koszt runu klimatyzacji wg źródła:** run z licznikiem energii (`attic_energy_entity`) jest wyceniany tak jak cykl CWU — przyrost licznika dzielony na pobór z sieci (po taryfie), rozładowanie baterii (po średnim koszcie energii w baterii) i PV (po bieżącej RCE). AC grzeje w dzień roboczy 08–16, gdy działa PV, więc wcześniejsza wycena zegarowa zawyżała koszt.
- **Nowe pola runu:** `pv_share` (udział PV w energii runu) i `split_complete`. W Statystykach: kolumna i kafelek „PV %” (średnia ważona energią), a przy kosztach bez pełnego podziału dopisek „(przybl.)” — brak licznika, brak którejś serii źródeł albo rekord sprzed 0.17.5.
- Rekordy runów mają `v: 2` — backfill (00:30 i po starcie) przelicza ostatnie 7 dni z recordera HA; starsze rekordy zostają z kosztem zegarowym.
- Bez zmian po stronie sterowania: add-on nadal niczego nie zapisuje do klimatyzacji ani pompy.

## 0.17.4 — poddasze: węższe pasmo AC, energia z licznika, dziennik runów; slot 52: próg deficytu 8 °C

- **Pasmo AC poddasza:** klimatyzacja wyłączana, gdy temperatura przekroczy cel o **0,5 °C** (było 0,7); dolna granica (−0,3 °C) bez zmian. Wcześniej poddasze trzymało 21,2–21,7 °C przy celu 21,0.
- **Doradca, slot 52 (opóźnienie grzałki AH):** propozycja „skróć” wymaga mediany zimowego deficytu **8 °C** (było 3 °C) — próg 3 °C odpalałby już przy pierwszych łagodnych chłodach, a nie przy mrozie.
- **Energia AC z licznika (A1):** nowa opcja `attic_energy_entity` (licznik kWh). Energia cyklu pętli B to różnica stanu licznika; przy braku encji, niedostępności, resecie licznika lub przerwie >30 min — całka mocy `attic_power_entity` jak dotąd.
- **Dziennik runów AC (A2):** każdy run klimatyzacji (start, czas, kWh, koszt wg strefy taryfy, temperatury, ΔT, °C/h, kWh/°C, temperatura zewnętrzna) zapisywany nocą o 00:30 i po starcie z recordera HA (ostatnie 7 dni); sekcja „Runy klimatyzacji poddasza” w Statystykach, `GET /api/attic_runs?days=60`. Tylko odczyt — add-on niczego nie zapisuje do klimatyzacji.
- Wymaga ustawionego `attic_power_entity` (od 02.11: `sensor.miernik_energii_klimatyzacje_power_b`); `attic_energy_entity` opcjonalne.

## 0.17.3 — poprawka: rozpoznanie „nadwyżka PV” / „ręcznie (kąpiel)” działa na prawdziwym logbooku

- Logbook HA nie podaje `entity_id` automatyzacji, która włączyła przełącznik kąpieli (tylko kontekst jej pierwszej akcji), więc 0.17.2 nie rozpoznawał żadnego cyklu. Teraz: włączenie przez użytkownika HA (`context_user_id`) = **„ręcznie (kąpiel)”**, każde inne = **„nadwyżka PV”** (jedyna automatyzacja włączająca ten przełącznik to „Grzej wodę przy nadwyżce z PV v2”). Brak wpisu w oknie 10 min przed startem zostawia „zmiana celu”.
- Rekordy cykli mają `v: 6` — backfill przelicza ostatnie 7 dni (zasięg recordera i logbooka), także cykle zapisane przez 0.17.2.

## 0.17.2 — CWU: typ cyklu „nadwyżka PV” i „ręcznie (kąpiel)” zamiast „zmiana celu”

- Cykl CWU, który startuje po zmianie celu na 58°C, jest teraz rozpoznawany po tym, kto włączył `input_boolean.temperatura_do_kapieli` (wpis logbooka HA): automatyzacja nadwyżki PV → **„nadwyżka PV”**, użytkownik (przycisk „podgrzej do kąpieli”) → **„ręcznie (kąpiel)”**. Inny autor albo brak wpisu zostawia dotychczasowe „zmiana celu”.


## 0.17.1 — poprawka: Statystyki nie ładowały się („Błąd wczytywania statystyk”)

- Wykres komfortu (linia „Na zewnątrz” z gradientem) wywoływał `createLinearGradient` z niepoliczonym obszarem wykresu, bo uPlot woła `fill` także dla znacznika legendy; cała strona Statystyki pokazywała błąd. Gradient wraca teraz do jednolitego koloru, gdy obszar nie jest jeszcze znany.

## 0.17.0 — taryfa z pv_roi, strona CWU, sugestie slotu 52, czytelny wykres komfortu

Wymaga pv_roi_tracker 0.55.1+ (encja `sensor.pv_roi_tracker_pv_tariff_zone`); bez niej działa zapas G12w z plakietką.

- **Strona CWU** (`/cwu`, także przeniesione z Raportu: tabela cykli, przebieg, model): kafle 30 dni (runy, mediana czasu, kWh, zł, zł/run, udział AH i PV), runy wg godziny na tle stref taryfy, czas runu a temperatura zewnętrzna, opłacalność grzałki w zł za zaoszczędzoną minutę; „Grupy wg ustawień grzałki” zwinięte na dole.
- **Koszt cyklu CWU** (`cost_pln`, `pv_share`, `zone`) z podziałem źródeł; stare cykle bez kosztu pokazują „—”.
- **Doradca: slot 52** (opóźnienie grzałki AH, 20–60 min, krok 5): propozycje na danych CWU; zapis do pompy dopiero po ręcznym „Zatwierdź i zapisz w pompie”, z allowlistą, blokadą przy trwającym teście i potwierdzeniem z ramki. Odrzucona propozycja milczy 7 dni (dotyczy też analizatorów curve i dhw).
- **Wykres „Czy w domu było ciepło?”**: pasy tła (poniżej minimum / minimum…cel / powyżej celu), pas od najzimniejszego pokoju do średniej, osobny odcień temperatury zewnętrznej.
- Harmonogram z pv_roi starszy niż 3 h (`generated_at`) jest odrzucany, więc nie udaje świeżego.
- Uwaga: w trybie zapasu pętla A liczy zegarem G12w (nie pomija cyklu); zapis slotu 52 nie sprawdza `heiko_enabled` (wymaga ręcznej akceptacji).

### Szczegóły taryfy z harmonogramu pv_roi zamiast G12w na sztywno

- **Cena, strefa i „szczyt” godziny** pochodzą z atrybutu `schedule` encji `tariff_schedule_entity` (domyślnie
  `sensor.pv_roi_tracker_pv_tariff_zone`), zapisywanego co cykl (nie częściej niż co 5 min) w nowej tabeli `tariff_hours`.
  Święta i taryfy inne niż G12w (np. G13) wynikają z harmonogramu. Cykle, streszczenia dób, cykle CWU, plan blokowy,
  doradca i okna testu Bezwładność używają tej ceny; historia sprzed zmiany zostaje wyceniona zegarowo (bez przeliczania).
- **Zapas**: brak encji, `unavailable`, niepoprawny harmonogram albo dane starsze niż 3 h — używane są najpierw
  `tariff_state_entity`/`tariff_price_entity`, potem stałe G12w (`tariff.py`). Ostrzeżenie w logu raz na godzinę,
  stan w `/api/live` (`tariff_source`) i plakietka „taryfa: zapas G12w — brak danych z pv_roi” w nagłówku UI.
- **KPI udziału szczytu**: baza z zimy zostaje liczona wg G12w; `/api/plan` zwraca `peak_basis` (taryfa bieżących dni),
  a opis KPI na pulpicie mówi, względem czego porównujemy.
- Nowa opcja `tariff_schedule_entity`.

## 0.16.0 — Doradca: test A/B pompy obiegowej P0 (1/6 vs 5/5) i propozycje nastawy

Cel: sprawdzić pomiarem, czy P0 5/5 wyrównuje temperatury pokoi lepiej niż 1/6 (kosztuje ok. 15 W). Zapis do pompy
tylko w teście uruchomionym przez usera; propozycje „zmień P0” niczego nie zapisują.

- **Miara rozrzutu** (Raport): dobowa nastawa P0, godziny standby i średni rozrzut stref (max−min) w standby.
- **Test „Pompa obiegowa P0: 1/6 vs 5/5”** (zakładka Testy): 6 dób A B A B A B, zmiana o północy, powrót do wartości
  wyjściowej. Start dopiero po zakończonym teście Bezwładność, przy włączonej krzywej i P0 = 1/6 albo 5/5. Allowlista
  tylko dwie encje P0 i pary 1/6, 5/5 (postój nigdy < 5 min). Zabezpieczenia jak w teście krzywej (Przerwij, ręczna
  zmiana wygrywa, potwierdzenie z ramki, komfort, restart add-onu). Werdykt: 5/5 wygrywa przy rozrzucie niższym
  o ≥ 0,3°C i niespadającej średniej stref (tolerancja 0,1°C); wynik z kWh/dobę i pewnością.
- **Analizator P0** w Doradcy + powiadomienie: powrót do 1/6 przy wyłączonej krzywej, zmiana na werdykt testu,
  ponowny test, gdy 3-dobowa średnia temp. zewn. wyjdzie poza pasmo testu ±3°C, propozycja pierwszego testu.
  Odrzucona propozycja milczy 14 dni.
- Poprawka: przerwanie testu kończy się także wtedy, gdy encje P0 są chwilowo niedostępne (start HA).

## 0.15.1 — raport COP tylko z wiarygodnych danych

- Raport (COP wg klas temp. zewn.) czyta statystyki COP dopiero od 2026-10-12, czyli od integracji `heiko_heatpump`
  1.14.0. Wcześniej COP liczono z Tw−Tc (zasobnik CWU vs woda grzewcza), co dawało średnie 9–25 i zawyżało każdą klasę.
- Braki danych podają datę odcięcia COP; usunięta stara uwaga „COP nierealny”.

## 0.15.0 — koszt i szczyt wg źródła energii: sieć, bateria, PV

Cel: raporty nie traktują już każdej kWh w godzinach szczytu G12w jako drogiej. Gdy pompa grzeje z nadwyżki PV albo
z baterii, ta energia nie kosztuje ceny szczytowej. Zero nowych zapisów do pompy.

- **Podział energii pompy co cykl** (nowy moduł `energy_source`): z przyrostów liczników — sieć = min(energia pompy,
  pobór z sieci), bateria = min(reszta, rozładowanie), PV = reszta. Wycena: sieć po cenie taryfy, PV po bieżącej RCE
  (utracony przychód z net-billingu; ujemna RCE = 0), bateria po średnim ważonym koszcie energii, która do niej weszła
  (ładowanie z sieci po taryfie, z PV po RCE). Domyślne encje: `sensor.power_meter_consumption`,
  `sensor.battery_total_charge`/`_discharge`, `sensor.battery_state_of_capacity`, `sensor.batteries_rated_capacity`,
  `sensor.current_rce_price_kwh` (nadpisywalne ustawieniem `<nazwa>_entity`).
- **Statystyki:** koszt wg źródła, wykres energii w trzech pasach (sieć tania / sieć w szczycie / panele i bateria),
  udział szczytu liczy tylko prąd z sieci.
- **KPI udziału szczytu:** szczyt tylko z sieci, liczony godzinowo; zimowa baza z LTS przeliczana raz tą samą metodą.
- **Doby i CWU:** `kwh_peak` w streszczeniu doby i koszt grzałki AH wg źródła energii.
- Dane sprzed 0.15.0 (cykle, streszczenia dób, cykle CWU) zostają z wyceną zegarową — nie da się ich przeliczyć
  bez historii liczników, a skasowanie oznaczałoby utratę dni spoza okna recordera.

## 0.14.0 — czytelniejszy interfejs: wykresy, podzakładki, język dla laika

Cel: add-on ma być zrozumiały bez znajomości kodów etapów i skrótów. Zero nowych zapisów do pompy.

- **Statystyki:** przełącznik zakresu 24 h / 7 dni / 30 dni (domyślnie 7 dni, zapamiętany w przeglądarce), 4 wykresy
  (komfort i pogoda, energia i koszt wg taryfy, czy model trafia, poddasze) i wniosek liczony z danych nad każdym.
  Agregacja godzinowa po stronie serwera, wykresy rysuje dołączony uPlot (MIT, bez CDN).
- **Raport w 4 podzakładkach:** Ciepła woda, Ogrzewanie, Energia, Pompa (adres z `#cwu` itd.). Tabele zwijają się do
  5 wierszy, dobowe zestawienia mają wykresy słupkowe. Grupy konfiguracji grzałki mają okres od–do, a slot 50 w jednej
  formie (wcześniej ta sama konfiguracja wychodziła jako dwie grupy).
- **Model CWU dla laika:** werdykt jednym zdaniem, wybór cyklu i wykres przebiegu wody minuta po minucie z odcinkiem
  grzałki AH i przerywaną linią „sama sprężarka”. Regresja i tabele są w „Szczegółach dla dociekliwych”. Dziennik
  cykli v3 zapisuje przebieg (backfill przelicza ostatnie 7 dni; starsze cykle są oznaczone „sprzed zapisu przebiegów”).
- **Doradca:** zamiast osobnego „Eksperymentu” proponuje test „Bezwładność −1” z przyciskiem „Przejdź do testu”.
  Dowody po polsku, historia zwijana.
- **Język:** bez kodów etapów (D1/A2/M3), słowniczek z dymkami (CWU, AH, COP, krzywa, R², slot…), zwijane historie
  na stronach Testy i Doradca. Poprawka komunikatów modelu CWU z 0.13.0 (bdd3160) wchodzi w tym wydaniu.
- Poprawki: dymki słowniczka nie poszerzają strony na telefonie; `/api/dhw_cycles` wysyła przebieg tylko dla 30
  najnowszych cykli; okres grupy liczony po czasie, nie po tekście.

## 0.13.0 — model grzania CWU i koszt grzałki AH (tylko odczyt)

Cel: policzyć z własnych danych, ile kosztuje grzałka AH w cyklu CWU i ile czasu oszczędza, zależnie od temperatury
zewnętrznej i wody — pod decyzję o opóźnieniu startu źródła dodatkowego (slot 52).

- **Dziennik cykli (v2):** każdy cykl dostaje energię całkowitą, temperaturę końcową oraz tempo grzania (°C/min)
  i średnią moc (kW) osobno dla odcinka samej sprężarki (od 5. minuty do wejścia AH) i odcinka z AH. Rekordy sprzed
  zmiany są przeliczane z recordera przy najbliższym backfillu (do 7 dni wstecz).
- **Model:** regresja tempa i mocy sprężarki od temperatury zewnętrznej i wody z odcinków wszystkich cykli (min. 8).
  Dla cyklu z AH kontrfaktyka „ta sama zmiana temperatury samą sprężarką” daje dodatkowe kWh i zł oraz zaoszczędzone
  minuty; za mało danych albo warunki poza zakresem uczenia (±3 °C) dają wynik z powodem, nie zgadywanie.
- **Raport:** sekcja „Model grzania CWU i koszt grzałki AH” — współczynniki, wykres tempa sprężarki wg temperatury
  zewnętrznej (cykle z AH w osobnym kolorze), suma kosztu AH i cena za zaoszczędzoną minutę. `/api/dhw_cycles` zwraca
  dodatkowo `model` i `ah_effect`.
- Zero zapisów do pompy. +10 testów (418 zielonych).

## 0.12.2 — zakładka Testy: wykonane testy oznaczone

Karta typu testu, którego ostatni zakończony przebieg spełnił miarę wyniku (test zapisu: wszystkie zapisy potwierdzone;
bezwładność: model zidentyfikowany), dostaje odznakę „wykonany <data> · zapisy n/n potwierdzone”, ma opis i harmonogram
zwinięte w „Szczegóły”, przycisk „Powtórz” zamiast „Start” i trafia na koniec listy. Przerwany test nie liczy się jako
wykonany.

- +1 test — 408 zielonych.

## 0.12.1 — potwierdzenie zapisu dopiero z ramki pompy

Integracja `heiko_heatpump` po `number.set_value` od razu pokazuje nową wartość (stan optymistyczny), a odczyt z ramki
pompy (`Curve_Parallel`, odpytanie co 60 s) zastępuje ją dopiero później. Krok testu wypadający < 1 min po zapisie
uznałby więc za potwierdzenie wartość, której pompa jeszcze nie odesłała.

- Zapis liczy się jako potwierdzony dopiero, gdy od niego minęło ≥ 2 min (`FRAME_DELAY_MIN`) i stan nadal się zgadza.
  Limit 10 min na potwierdzenie bez zmian.
- +1 test — 407 zielonych.

## 0.12.0 — Doradca D3a: silnik testów uruchamianych przez użytkownika (zakładka „Testy”)

Pierwszy kod z zapisem do pompy, ale **żaden zapis nie nastąpi sam**: test startuje tylko przyciskiem w nowej zakładce
„Testy” (dwa kliknięcia = zgoda na cały, z góry pokazany harmonogram) i tylko przy włączonym „Sterowanie aktywne (Heiko)”
w Opcjach (domyślnie wyłączone).

- `experiments.py` — jedyny moduł z zapisem do pompy: wyłącznie `number.set_value` na przesunięciu krzywej
  (`…heating_curve_parallel_shift`, sprawdzane przy zapisie), zakres ±4, krok ≤ 1, jeden test naraz.
- Typy testów: „Test zapisu” (+1 na 10 min i powrót, przy wyłączonej krzywej) i „Bezwładność −1” (okna 6–12 i 15–21
  przez 3 doby robocze, przy włączonej krzywej).
- Zabezpieczenia: potwierdzenie zapisu z ramki pompy w 10 min (inaczej przerwanie i powrót), ręczna zmiana przesunięcia
  przerywa test bez nadpisywania, najzimniejszy pokój poniżej minimum > 30 min przerywa i przywraca, restart add-onu
  w trakcie testu przywraca wartość wyjściową. Powiadomienia przez `advisor_notify_service`.
- Dziennik zapisów do pompy (`pump_writes`) i historia testów (`tests`) w bazie i w zakładce.
- Kolumna `curve_shift_c` w cyklach; `floor_learn.excitation()` liczy skoki przesunięcia jako wymuszenie.
- Test AST: wywołanie usługi dozwolone tylko w `experiments._write` (oprócz pętli B AC).
- Nawigacja na telefonie zawija się do dwóch wierszy (6 zakładek).
- +21 testów — 406 zielonych.

## 0.11.2 — wskazówka „CWU w szczycie” tylko przy poborze z sieci

Doradca wysłał 01.10 wskazówkę, że duża część podgrzewania CWU przypada na szczyt G12w. Liczył jednak samą porę
(dni robocze 6–13, 15–22), a automatyzacja HA podnosi nastawę CWU 48→58 °C właśnie przy nadwyżce PV — w południe,
czyli w oknie szczytu. Wskazówka była więc fałszywie dodatnia: CWU z PV to efekt zamierzony.

- Nowa opcja `grid_power_entity` (domyślnie `sensor.power_meter_active_power`; + oddawanie, − pobór), logowana
  w telemetrii co 15 min razem z resztą.
- `advisor.dhw_peak_share(..., grid_key=)`: próbka CWU w szczycie liczy się tylko przy poborze z sieci
  ≥ `GRID_IMPORT_W` (500 W); pokryta przez PV/baterię trafia do `pv_covered` (w `evidence` propozycji). Próbki bez
  odczytu sieci są pomijane — historia sprzed 0.11.2 go nie ma, więc wskazówka milczy, dopóki nie zbierze się
  ≥ 20 nowych próbek CWU. Obecna propozycja `dhw:peak_hint` wygaśnie przy najbliższym przebiegu doradcy.
- Tekst wskazówki mówi wprost o poborze z sieci. Zero zapisów do pompy, pętle sterowania bez zmian.
- +3 testy — 385 zielonych.

## 0.11.1 — poprawka: wilgotność nan/inf nie wywala pulpitu

`live.fmt_humidity` robił `round()` na wartości `nan`/`inf` (np. czujnik ESPHome/szablon po błędzie odczytu), co
rzucało wyjątek — pulpit i `/api/live` zwracały 500. Teraz taka wartość daje pustą wilgotność na plakietce, jak brak
danych. Pętle sterowania bez zmian, zero zapisów do pompy.

## 0.11.0 — pulpit: plakietki poza obrysem, wilgotność, przekrój mebli, okna z parapetem

Zmiana wyłącznie w rysowaniu modelu domu. **Pętle sterowania bez zmian, zero zapisów do pompy** (test AST bez zmian).

**Plakietki pokoi poza obrysem domu.** Temperatura nie zasłania już podłóg ani mebli: każdy pokój z czujnikiem ma
plakietkę w kolumnie po lewej lub prawej stronie sylwetki domu (strona wg położenia pokoju na ekranie), z cienką linią
do kropki na środku pokoju. Nakładające się plakietki w kolumnie są rozsuwane w dół. Plakietka ma kolor klasy
temperatury, odświeża się na żywo (`data-live`, `data-tclass`) i otwiera kartę pokoju. Pokoje bez czujnika
(schody) nie mają już podpisu. Nowe: `rooms.layout_callouts()`, `Scene.callouts`.

**Wilgotność na plakietce (tylko informacja).** Opcjonalne pole pokoju `humidity_entity`; `/api/live` zwraca
`rooms.<klucz>.hum` („55%” albo pusto). Nie wchodzi do żadnej reguły sterowania.

**Przekrój mebli na 120 cm (`CUT_H`).** Bryły wyższe są rysowane ucięte, z przerywanym obrysem górnej ściany; bryły
w całości powyżej są pomijane. `"cut": false` na bryle wyłącza przekrój (stopnie schodów z `stairs` mają go wyłączonego
zawsze).

**Otwory frontowe z parapetem.** `fronts[].z0` — dolna krawędź otworu (okno); `h` pozostaje górną krawędzią.

**Nowe materiały (CSS).** Bryły: `ceramic`, `glass`, `metal`. Otwory: `door`, `garage`. Ściany: `pegboard`.
Podłogi: `tile-dark`, `tile-light`, `concrete`.

| Pole | Gdzie | Domyślnie |
|---|---|---|
| `humidity_entity` | `rooms[]` | brak (bez wilgotności) |
| `cut` | `boxes[]` | `true` |
| `z0` | `fronts[]` | `0` |

Koszt: widok domu jest węższy o kolumny plakietek (na telefonie ok. 25%).

## 0.10.0 — dziennik cykli CWU i grzałki AH (A2), zero propozycji

Plan A2 analizatora grzałki AH — automat tabel budowanych dotąd ręcznie z recordera dla eksperymentów A3a–A3d
(`docs/HEIKO_AH_cost_report.md` §9–12 w prywatnym repo `/config`). Ani slot 49, ani slot 50 nie okazały się warunkiem
wejścia AH, więc decyzję o polityce (A4) muszą nieść pojedyncze cykle CWU, nie porównanie dobowe ON/OFF. **Zero zapisów
do pompy, zero propozycji** (test AST bez zmian).

**Nowe: `GET /api/dhw_cycles`, zakładka „Raport”.** Dla każdego cyklu CWU (`working_mode` w trybie CWU, ≥ 1 min):
cel, woda na starcie i w minucie 19 (deficyt), tempo, temperatura zewnętrzna, typ (zegar / zmiana celu / zapotrzebowanie),
minuta pierwszego ticku AH, minuty AH, kWh i PLN z fazy C licznika pompy (opcja `ah_energy_entity`, domyślnie
`sensor.energia_pompa_ciepla_energy_c`), i konfiguracja slotów 49/50/52 w chwili startu (z dziennika zmian od 0.9.3 —
sprzed niego „?”, uczciwie nieznane). Grupowanie po konfiguracji z licznikiem cykli, cykli z AH, medianą minuty ticku
i flagą małej próby (n < 5).

**Zapis trwały, nie tylko recorder (7 dni).** Nowa tabela `dhw_cycles`; harmonogram dobowy uzupełnia brakujące doby
z zasięgu recordera (backfill 7 dni), niezależnie od streszczeń dobowych (D1). Cykl wciąż trwający na granicy okna nie
jest zapisywany — kolejny przebieg złapie go w całości.

## 0.9.3 — katalog: dodatkowe źródła ciepła (sloty 47–52) i ograniczona nastawa (77/78); dziennik liczy tylko realne zmiany

Plan A1 analizatora grzałki AH. **Zero zapisów do pompy** (test AST bez zmian). Integracja `heiko_heatpump` 1.13.0 wystawiła
nowe encje; add-on je obserwuje i poprawnie loguje ich zmiany przed eksperymentem A3.

**Nowe w katalogu (klasa C — tylko obserwacja)**

| Slot | Encja (sufiks) | Opis |
|---|---|---|
| 47 | `backup_source_for_heating` (switch) | dodatkowe źródło przy ogrzewaniu |
| 48 | `backup_priority_heating` (select) | priorytet w buforze przy c.o. |
| 49 | `backup_source_for_dhw` (switch) | dodatkowe źródło przy c.w.u. |
| 50 | `backup_priority_dhw` (select) — klucz `backup_heater` | priorytet HWTBH względem AH przy CWU; **kanoniczny select zamiast aliasu switch** (odwrócona logika), jedna zmiana = jeden wpis |
| 51 | `backup_source_start_dependency` (number 0–600) | zależność temp. ↔ czas startu źródła |
| 52 | `backup_source_start_delay` (number, min) | czas do uruchomienia dodatkowego źródła |
| 77 | `reduced_setpoint` (switch) | ograniczona nastawa: **funkcja włączona** (nie „aktywna teraz”) |
| 78 | `reduced_setpoint_drop_rise` (number, °C) | o ile spada cel wody |

Katalog: 31 → 34 parametry. Usunięte: cztery parametry anty-legionelli (poza add-onem).

**Naprawa dziennika zmian.** `last_changed` odnawia się dla wszystkich encji przy przeładowaniu HA bez zmiany wartości, więc 33 z 37
wpisów z 26.09 miało `old = new` (31 naraz po restarcie), a dwa szybkie przełączenia w jednym cyklu 15 min dawały `off → off`.
Teraz dla encji z nowym `last_changed` add-on pobiera historię recordera od poprzedniego odczytu i loguje każde przejście wartości
(A→B, B→A); bez historii — zmiana tylko gdy stan faktycznie inny. Encja niedostępna zostaje przy ostatniej znanej wartości, więc zmiana
w czasie niedostępności nie ginie.
- Migracja jednorazowa: wpisy `old = new` trafiają do `param_changes_legacy`, a realne przejścia z ostatnich ~7 dni są odtwarzane z historii
  (bez historii — ponowi w następnym cyklu, niczego nie ruszając). `advisor_managed_keys` traci klucze `anti_leg_*`.

**HWTBH to licznik sygnału, nie grzałka** (jej w instalacji nie ma, licznik liczy urojone minuty): brak alarmu anomalii `hwtbh_min`,
zdanie o grzałkach w raporcie obejmuje tylko AH i HBH, kolumna raportu nazywa się „Sygnał HWTBH min”.

**Pulpit:** dwa wiersze zamiast jednego — „Ograniczona nastawa — funkcja (panel)” (switch 77) i „— aktywna teraz (wnioskowana)”.

## 0.9.2 — kalibracja reguły krzywej po odtworzeniu zimy: zapas nad minimum po ostygnięciu domu

Odtworzenie zimy 2025/26 (180 dób) ujawniło lukę w regule „−1”: wymagała +0,5°C zapasu najzimniejszego pokoju nad minimum
**przed** obniżeniem, a planer przewiduje, że dom po −1 ostygnie o 0,4–0,65°C. W 7 z 90 dni „−1” po obniżeniu ta sama reguła
od razu kazałaby „+1” (ping-pong).

- Reguła „−1”: najzimniejszy wybrany pokój **po przewidywanym ostygnięciu** (`end_temp_delta_c` z planera, bez oceny 0,7°C)
  musi nadal być ≥ minimum + 0,5°C (0,3°C przerwy nad progiem „+1”). Karta i powód propozycji pokazują to ostygnięcie
  (`evidence.cooling_after_c`).
- Wynik na zimie 2025/26: „−1” 90 → 76 dób (42%), „+1” bez zmian 35 dób (19%), ping-pong 7 → 0.
- Pozostałe progi bez zmian, bo dane ich nie podważają: margines średniej ±0,3°C, próg „+1” minimum + 0,2°C, CWU ≥ 4 cykle na
  dobę, wskazówka o szczycie CWU ≥ 40%, z-score 3,5 (dla CWU i anomalii nie ma danych zimowych do kalibracji).
- Test regresji: dla siatki temperatur żadna propozycja „−1” nie prowadzi do natychmiastowego „+1”.

## 0.9.1 — skutki przesunięcia krzywej po wycenie „długu cieplnego”

Znalezione zaraz po wdrożeniu 0.9.0 przy sprawdzeniu liczb odtworzenia zimy: oszczędność z obniżenia krzywej o 1 wychodziła
stała (~1,8 kWh/dobę) niezależnie od pogody i 3–8× za duża. Przyczyna: `curve.uniform_effects` liczył koszt samego horyzontu
36 h, a obniżona nastawa oszczędza tam głównie ciepło wyjęte z zasobnika domu, które trzeba oddać, żeby wrócić do temperatury
bazowej. Planer wycenia to w swoim celu, ale efekty pokazywane w propozycjach — nie.

- `cost_day_delta_pln` i `energy_day_delta_kwh` uwzględniają teraz dług cieplny (ΔT końcowa / g / COP, koszt po cenie taniej
  taryfy). Dla −1: ok. −0,2…−0,8 kWh/dobę zależnie od pogody (wcześniej ok. −1,8…−3).
- Nowe pola: `energy_horizon_delta_kwh` (surowa różnica w horyzoncie) i `end_temp_delta_c` (temperatura domu po 36 h);
  karta propozycji pokazuje temperaturę końcową i oznacza wycenę długu.
- Test regresji: dług zmniejsza „oszczędność”, ale jej nie znosi; w mrozie efekt jest większy niż w łagodnej pogodzie.

## 0.9.0 — Doradca, etap D2: silnik propozycji i zakładka „Doradca” (zero zapisów do pompy)

Doradca liczy propozycje zmian nastaw i pokazuje je w nowej zakładce **Doradca**. W tym wydaniu „Zatwierdź”
**zapisuje wyłącznie decyzję** (tryb próbny, `trial = 1`) — nic nie trafia do pompy; pierwszy zapis dopiero w D3,
z własnym wykonawcą i allowlistą. Test AST pilnuje, że silnik i analizatory nie mają ścieżki zapisu.

**Analizatory** (funkcje czyste w `analyzers/`, wejście: migawka danych, wyjście: szkice propozycji):

| Analizator | Co robi | Pewność |
|---|---|---|
| `curve` | Przy włączonej krzywej i zapasie komfortu (24 h: średnia stref ≥ cel + 0,3°C, a najzimniejszy wybrany pokój ≥ minimum + 0,5°C) proponuje **eksperyment −1** przesunięcia krzywej; przy zbyt zimnym domu **+1**. Skutki (zł/dobę, kWh/dobę, temperatura) z planera. Przesuwanie w czasie pokazane tylko informacyjnie. | niska, dopóki model nie ma zidentyfikowanej bezwładności |
| `dhw` | Histereza CWU +1 K przy ≥ 4 cyklach na dobę (klasa A: 3–10 K); wskazówka tekstowa, gdy > 40% CWU wypada w szczycie G12w. Nastawa CWU, grzałki i anty-legionella zostają przy automatyzacjach HA (tylko tekst). | niska |
| `anomalies` | Same alerty: czujniki ostrzegawcze z HA (opcja) oraz skoki w streszczeniach dobowych (krótkie cykle, minuty grzałek, cykle CWU, impulsy P0) — odporny z-score (mediana + MAD) z 14 dób. | wysoka (czujnik) / niska (statystyka) |

**Silnik** (`advisor.py`): ta sama propozycja nie dubluje się, tylko się odświeża (TTL od ostatniego potwierdzenia:
krzywa 6 h, histereza/CWU/anomalie 72 h, czujniki 24 h); zmiana wartości = poprzednia „zastąpiona”; ustanie warunków =
„wygasła”; jeden aktywny eksperyment naraz (48 h po zatwierdzeniu); przy „Zatwierdź” ponowne liczenie analizatorów i
sprawdzenie, że parametr nadal ma wartość `from` (inaczej propozycja jest „zastąpiona”, brak połączenia z HA = odmowa).
Przebieg co godzinę i o 0:30 (po streszczeniach dobowych).

**Powiadomienia:** tylko o nowej propozycji wyróżnionej soczewki (albo „obie” — alerty), raz na propozycję, z linkiem
do add-onu; soczewka „żaden” = bez powiadomień.

**Minimum komfortu z wybranych pokoi:** nowa opcja `comfort_min_entities` (w Opcjach: pola wyboru przy strefach
dziennych). Dotyczy bezpiecznika planera, alarmu „pokój za zimny”, raportu i oceny propozycji. Średnia stref liczy się
zawsze ze wszystkich stref. Puste (albo same nieznane encje) = wszystkie strefy — bezpiecznik nigdy nie zostaje bez pokoi.

**Odtworzenie zimy** (zakładka „Raport”, `GET /api/advisor/replay`): co analizator krzywej zaproponowałby w zeszłym sezonie
grzewczym (raz na dobę o 7:00, na statystykach LTS z 24 h), z szacunkiem skutków z planera. To jedyny sposób oceny
analizatora krzywej przed sezonem (krzywa jest wyłączona). Skutki bez zidentyfikowanej bezwładności to rząd wielkości,
nie prognoza — raport mówi o tym wprost; brak punktów krzywej = „brak oszacowania”, nie 0.

| Nowe | Opis |
|---|---|
| Tabela `proposals` | propozycje: analizator, soczewka, rodzaj, parametr, from → to, powód, dowody, skutki, pewność, TTL, status, decyzja, `trial` |
| `GET /advisor` | zakładka „Doradca” (propozycje obok siebie wg soczewek, dowody, pewność, historia) |
| `GET /api/proposals` | oczekujące + historia, pozostały czas, aktualna soczewka |
| `POST /api/proposals/<id>/decision` | `{"decision": "zatwierdzona" \| "odrzucona"}` — tylko status; 409 przy nieaktualnej, 503 bez HA |
| `GET /api/advisor/replay[?refresh=1]` | odtworzenie zimy (w tle, cache w bazie) |
| Opcje: `comfort_min_entities`, `advisor_lens`, `advisor_notify_service`, `advisor_link_path`, `advisor_anomaly_entities` | patrz README |
| `heiko_plan.uniform_shift` | skutki jednolitego przesunięcia ±1 w dokumencie planu (dla doradcy) |

Zmiany w istniejącym kodzie: `cycle.run_heiko_cycle` (minimum z wybranych pokoi, `uniform_shift`), `comfort` (`min_room_entities`),
`analysis.comfort` (minimum z wybranych, lista `min_rooms`), `ha_client.notify` (opcjonalne `data` z linkiem). Testy: 348.

## 0.8.4 — streszczenia: licznik bez zmian w dobie = 0 min

Doba, w której licznik czasu pracy grzałki się nie zmienił, miała w historii HA jeden punkt i była pomijana
(brak danych), więc średnie „min/dobę” liczyły się tylko z dób ze zmianą i były zawyżone (AH: 9 zamiast ok. 2,6
min/dobę na 7 dób). Teraz taka doba to 0 min; brak samego licznika nadal oznacza brak danych. Streszczenia z 0.8.3
są jednorazowo przeliczane (znacznik schematu podniesiony).

## 0.8.3 — grzałki: jednostki w minutach, licznik AH, poprawny opis rejestru 9.4

Znalezione przy weryfikacji z instrukcją Heiko: liczniki czasu pracy grzałek w integracji są w **minutach**
(`Time_AH`/`Time_HBH`/`Time_HWTBH`), a streszczenia dobowe nazywały je godzinami; brakowało też licznika **AH**
(grzałka pomocnicza jednostki wewnętrznej), który jest kluczowy dla oceny kosztu CWU.

- Streszczenia dobowe: `backup` = `ah_min` / `hbh_min` / `hwtbh_min` (minuty). Stare streszczenia (złe nazwy i
  jednostki) są jednorazowo kasowane przy starcie, brakujące doby (ostatnie 7) uzupełnia backfill.
- Telemetria loguje `ah_working_time`; raport i tabela „Własne dane” pokazują AH/HBH/HWTBH w min/dobę.
- Katalog: parametr `backup_heater` (encja „Backup Heater (HBH)”) opisany zgodnie z instrukcją — to pozycja menu
  **9.4 „Priorytet dla dodatkowego źródła ciepła w podgrzewaczu c.w.u.”** (HWTBH vs AH, rejestr 50), a nie włącznik
  grzałki HBH (HBH to 9.1/9.2, rejestry 47/48, niewystawione). ON (0.0) = „Niższe dla grzałki wewnętrznej AH” = AH ma
  pierwszeństwo jako wspomaganie CWU.

## 0.8.2 — dziennik zmian parametrów: źródło zmiany z logbooka

Znalezione na żywo: zmiana parametru wykonana kliknięciem w UI HA była zapisana ze źródłem „nieznane”.
Przyczyna: kontekst stanu encji jest nadpisywany przy kolejnym odświeżeniu z pompy (co ok. 3 min), a add-on
próbkuje co 15 min, więc `user_id` prawie zawsze już znika.

- Źródło zmiany jest teraz brane z **logbooka HA** (`GET /api/logbook`, tylko odczyt): użytkownik (UI/API) →
  „użytkownik HA”, automatyzacja/skrypt → „automatyzacja/skrypt”; kontekst stanu zostaje tylko jako zapasowy trop.
  Zmiana bez śladu w logbooku (panel pompy, integracja) nadal jest „nieznane”.
- Wpisy już zapisane jako „nieznane” są uzupełniane z logbooka przy kolejnych cyklach (kilka dni wstecz).
- Bez zmian w danych i w zapisach do pompy (nadal zero).

## 0.8.1 — Raport: kolejność klas temperatury

Poprawka UI zakładki „Raport” znaleziona przy weryfikacji na żywo: klasy temperatury zewnętrznej
(udział szczytu, praca pompy, COP) były ułożone jako napisy (0, 3, 6, 9, 12, 15, −12, −3, −6, −9) — teraz
rosnąco liczbowo. Nagłówek sezonu bez znaku „→” (brak glifu w czcionce). Bez zmian w logice i danych.

## 0.8.0 — Doradca, etap D1: dane i katalog (nadal zero zapisów do pompy)

Fundament pod „doradcę z zatwierdzaniem”: add-on zaczyna trwale zbierać dane pompy i opisuje, co
mierzy — **wyłącznie odczyt**. Nic z tego wydania nie zapisuje do pompy (test strukturalny AST
obejmuje nowe moduły i sprawdza, że używają tylko odczytów z `ha_client`).

**Nowe moduły**

| Moduł | Do czego |
|---|---|
| `catalog.py` | Katalog parametrów pompy z integracji `heiko_heatpump`: klasa bezpieczeństwa A/B/C, zakres, maks. krok, wpływ. Encje po **sufiksie** (`heiko_heat_pump_<klucz>`), pełne entity_id rozwiązywane w czasie pracy. `check_class_a()` — zakres klasy A i limit kroku. |
| `telemetry.py` | Próbka co cykl (15 min): sensory pompy, temperatury stref, temp. zewn., licznik energii → wąska tabela; wykrywanie każdej zmiany parametru z katalogu (dowolne źródło) → dziennik; retencja: surowe 2 lata, potem agregat godzinowy. |
| `summaries.py` | Dobowe streszczenia z historii HA (recorder trzyma ~7 dni): starty i długość pracy sprężarki, krótkie cykle, cykle CWU, przyrost licznika grzałek HBH/HWTBH, impulsy pompy obiegowej P0, energia szczyt/poza szczytem. Zadanie 00:20 + uzupełnianie brakujących dób z ostatnich 7. |
| `analysis.py` | Raport opisowy (funkcje czyste na LTS): komfort, energia i udział szczytu, praca pompy wg temp. zewn., COP, własne dane, hipotezy i luki w danych. |

**Klasy parametrów (decyzja 26.09)** — A: przesunięcie krzywej ±4 (krok ≤1), histereza ogrzewania 1–5,
histereza CWU 3–10, nastawa CWU 45–55 (krok ≤2), pompa obiegowa P0 (czasy, obroty) w pełnym zakresie
integracji; B: punkty krzywej, tryb/typ P0, magazynowanie CWU, grzałka HBH, anti-legionella (i wartości
spoza zakresu A); C (nigdy nie zapisywane): tryb pracy, zasilanie, tryb wakacyjny, włącznik krzywej.

**Endpointy (wszystkie GET)**

| Endpoint | Zwraca |
|---|---|
| `/report` | Strona „Raport” |
| `/api/report` | Ostatni raport z cache; `?refresh=1` przelicza w tle (LTS, ok. pół minuty–kilka minut) |
| `/api/catalog` | Parametry z katalogu: klasa, zakres A, maks. krok, bieżąca wartość z HA, `last_changed`, znacznik automatyzacji (`advisor_managed_keys`) |
| `/api/changes` | Dziennik zmian parametrów + licznik (dziś / 30 dni / łącznie) + stan telemetrii i rozmiar bazy (ostrzeżenie > 200 MB) |

**Tabele bazy:** `telemetry_keys`, `telemetry`, `telemetry_hourly`, `param_changes`, `daily_summary`,
`report_cache` (migracja automatyczna, istniejące dane bez zmian).

**Ustawienie:** `advisor_managed_keys` (lista kluczy katalogu po przecinku) — parametry, którymi zarządzają
automatyzacje HA; doradca nie będzie ich zapisywał. Ustawiane przez `POST /api/settings`.

**Pulpit:** kafelek „Zmiany nastaw pompy” (dziś / 30 dni). **Nowa zakładka „Raport”.**

**Zakres danych:** czujniki `heiko_heatpump` istnieją od 12.04.2026, więc COP/CWU/HBH/P0 z LTS są
dostępne dopiero od tej daty; komfort, energia i udział szczytu — z całej zimy. COP estymowany przez
integrację ma wartości nierealne dla pompy powietrznej (szacunek z nominalnego przepływu) — raport
oznacza to w „lukach w danych”.

## 0.7.2 — cel wody w trybie CWU

Zaobserwowane na żywo: encja celu wody (i nastawa stała) pokazuje cel **aktualnego trybu** — w CWU
48°C. Wcześniej wnioskowanie o ograniczonej nastawie widziałoby w CWU „cel powyżej krzywej” =
„nieaktywna” i zawyżało liczbę przejść (wymuszenie do uczenia inercji).

- W trybie CWU cel wody i `reduced_active` nie są zapisywane (NULL) — nie ma fałszywych przejść.
- Pulpit: w CWU „Cel wody wg pompy” pokazuje „— (CWU)”; tryb „Sanitary Hot Water” ma etykietę CWU.

## 0.7.1 — treść karty pompy

Karta pompy opisuje pętlę A jako obserwację (pompa steruje sama), a wartości optymalizatora są
oznaczone jako poglądowe. Bez zmian w logice.

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
