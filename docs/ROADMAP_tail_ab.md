# Test A/B „ogona grzania” — roadmapa

Szczegóły i kontrakt wykonawcy: `PLAN_tail_ab_E1.md`.

- [x] E1 — nakładka `tail_cut_enabled`, miary `kwh_hdd`/`room_day`/`tail_cuts`, definicja `ogon_ab` (tylko repo, bez wydania). Rollback: `git revert` commita E1.
- [ ] E2 — wydanie 0.27.0, **nie wcześniej niż w niedzielę 2026-11-29** i tylko gdy żaden test nie ma statusu „trwa”. Rollback: wersja 0.26.0 z Supervisora.

## Handoff po E1 (2026-11-26)
- Zrobione: kod i testy w `main` (bez pusha, bez wydania). Wersja na żywo nadal 0.26.0.
- Do decyzji przy E2: `tail_cuts` liczy bieżącą, niepełną dobę jak pełną (wynik powstaje dopiero po zakończeniu testu, więc dziś nie szkodzi).
- 4 czerwone testy w `tests/test_dhw_cycles.py` (dane z 09.2026 poza oknem 60 dni) naprawione w E2.
- Wznowienie w niedzielę 2026-11-29: bramka „żaden test nie trwa”, potem E2 wg `PLAN_tail_ab_E1.md`.
