# Test A/B „ogona grzania” — roadmapa

Szczegóły i kontrakt wykonawcy: `PLAN_tail_ab_E1.md`.

- [x] E1 — nakładka `tail_cut_enabled`, miary `kwh_hdd`/`room_day`/`tail_cuts`, definicja `ogon_ab` (tylko repo, bez wydania). Rollback: `git revert` commita E1.
- [x] E2 — wydanie 0.27.0 (2026-11-28, po zakończeniu testu P0). Rollback: wersja 0.26.0 z Supervisora.

## Handoff po E1 (2026-11-26)
- Zrobione: kod i testy w `main` (bez pusha, bez wydania). Wersja na żywo nadal 0.26.0.
- Do decyzji przy E2: `tail_cuts` liczy bieżącą, niepełną dobę jak pełną (wynik powstaje dopiero po zakończeniu testu, więc dziś nie szkodzi).
- 4 czerwone testy w `tests/test_dhw_cycles.py` (dane z 09.2026 poza oknem 60 dni) naprawione w E2.
- Wznowienie w niedzielę 2026-11-29: bramka „żaden test nie trwa”, potem E2 wg `PLAN_tail_ab_E1.md`.

## Po E2 — otwarte (z review, nieblokujące)
- Werdykt `ogon_ab` nie sprawdza, czy w dobach A coś odcięto; doby z awarią liczą się jak zwykłe.
- `kwh_hdd` to średnia dobowych ilorazów (ciepła doba waży mocno); brak progu pokrycia danych dla doby.
- Definicja nie wymaga `pump_energy_entity`; `writes` + `tail_cut_enabled` przechodzi walidację, choć nie działa.
- Brak testu end-to-end `ogon_ab`.
- Doradca, reguła „skróć” slot 52: patrzy tylko na zł/zaoszczędzoną minutę (bez kWh), liczy cykle, w których AH nie wchodzi, i ponawia propozycję tuż po ręcznej zmianie — czeka na decyzję usera.
