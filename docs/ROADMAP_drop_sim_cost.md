# ROADMAP — usunięcie kolumn `sim_cost_today_*` (heiko_predictive_control 0.20.3)

Źródło prawdy dla tej pracy. Szczegóły kroków, rollback i sekcja Wykonawca: `docs/PLAN_drop_sim_cost_0_20_3.md`.

| Etap | Zakres | Status |
|---|---|---|
| E0 | Odczyt (grep użyć kolumn), plan, zgoda usera na plan + wydanie 0.20.3 | ZROBIONE 2026-11-03 (plan zatwierdzony; brak zmian w kodzie) |
| E1 | Edycje `cycle.py`/`db.py`/testy + migracja `DROP COLUMN` + test migracji, potem wydanie 0.20.3 (ostatni etap → łańcuch simplify → code-review → release → instalacja z backupem → weryfikacja na żywo) | KOD ZROBIONY 2026-11-03 (705 testów zielone); wydanie 0.20.3 w toku |

Poprzednie wydanie: 0.20.2 (`docs/HANDOFF_0_20_2.md`), HEAD przed pracą = `4c5eecd`; nieśledzone pliki docs
(HANDOFF_0_20_2, ten ROADMAP, PLAN) zacommituj razem z etapem E1.
