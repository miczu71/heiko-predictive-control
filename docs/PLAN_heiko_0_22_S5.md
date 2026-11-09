# PLAN S5 — G13 z okien `evening_peak` z pv_roi (Heiko 0.22.0)

Źródło: `/config/docs/ROADMAP_heiko_0_22.md` (S5), decyzja T0-2: G13 bierze okna ze stref pv_roi — święta, lato i zmiany taryfy same.

## Kontrakt

**`g13_peak.py`** — koniec stałych godzin:
- Usunąć `PREHEAT_FROM_H`, `PEAK_FROM_H`, `WINDOW_END_H`, `WORKDAY_PROBE_H`, `workday_from_feed`. Zostaje `PREHEAT_LEAD_H = 3`, `BREAKER_HOLD`, `SHIFT_LIMIT`, `PEAK_ZONE = "evening_peak"`.
- `@dataclass(frozen=True) class Window: start: int; end: int` (godziny lokalne doby, `end` wyłącznie, `end ≤ 24`);
  `preheat_from -> max(0, start - PREHEAT_LEAD_H)`; `empty -> start == end`. `NO_PEAK = Window(0, 0)`.
- `window_from_feed(feed, day) -> Window | None`: odczyt `feed.at(datetime(day, h))` dla h = 0..23 (bez względu na świeżość feedu);
  brak któregokolwiek wiersza → `None` („brak harmonogramu taryfy”); inaczej pierwszy ciągły odcinek godzin ze `zone == PEAK_ZONE` → `Window(start, end)`;
  żadnej takiej godziny → `NO_PEAK`.
- `decide(now, *, window: Window | None, current_shift, base_shift, min_room_c, state, cfg)` (zamiast `workday`):
  `window is None` → `Decision(None, "brak harmonogramu taryfy")`; `window.empty` → `Decision(None, "brak szczytu evening_peak w taryfie")`;
  dogrzanie `preheat_from ≤ hour < start` (`+preheat_shift`), szczyt `start ≤ hour < end` (`+peak_shift`), `hour ≥ end` z `base_shift` ≠ bieżące → „koniec okna”;
  reszta bez zmian (bezpiecznik, `_clamp`, `Decision.base`). Okno z `end == 24`: „koniec okna” realizuje wtedy `g13_exec` przy zmianie doby (istniejąca ścieżka).
- `Decision` dostaje pole `window: Window | None = None` (okno użyte do decyzji — do zapamiętania w stanie).

**`g13_exec.py`**: `feed = tariff_feed.for_settings(...)`, `window = g13_peak.window_from_feed(feed, now.date())` liczone **przed** gałęzią „po oknie”;
`after_window = (window is not None and not window.empty and now.hour >= window.end) or (window_end zapamiętane w stanie i now.hour >= st["window_end"]) or st.get("day") not in (None, today)`
(brak okna i brak zapamiętanego końca → tylko zmiana doby). Po `decide`: gdy `decision.window` niepuste → `st["window_end"] = window.end`; czyszczone razem z `_clear(st)`/nową dobą (jak `base`).
`decide(..., window=window, ...)`.

**`g13_view.py`**: `_next_change(now, window, cfg)` z okna: kroki `preheat_from`, `start`, `end` (`end==24` → „00:00 następnej doby” nie pokazuj — brak kroku); `_now_phase(now, window, st)` fazy z `window.start/end`;
`now_view`/`build` liczą `window` przez `window_from_feed`. Klucz `workday` w `build()` zostaje (zgodność z `static/g13.js`): `None` = brak harmonogramu, `False` = brak szczytu, `True` = jest okno;
dodać `"window": {"start": "16:00", "end": "21:00"} | None`. `static/g13.js:18-19`: komunikaty „Brak harmonogramu taryfy — tryb nic nie zmienia.” (bez zmian) i „Brak szczytu evening_peak w taryfie dziś — bez zmian przesunięcia.”.
Opis trybu w `templates/options.html`/`config.yaml` jeśli wymienia „16–21”/„13–16” na sztywno → „szczyt `evening_peak` z taryfy pv_roi, dogrzanie 3 h wcześniej” (tylko tekst, jeśli trafienie istnieje).

## Wykonawca (jedna paczka W1, „ryzyko: zapis do pompy” → `sonnet-worker`; potem `claimcheck` + jeden `verifier`)
Repo: `/config/addons/heiko_predictive_control`, kod `heiko_predictive_control/heiko_predictive_control/` (`PKG`), testy `heiko_predictive_control/tests/`. Skille: `addon-conventions`, `tdd` (testy czerwone przed kodem — zaraportuj).

| Plik | Zmiana |
|---|---|
| `PKG/g13_peak.py` | kontrakt wyżej; docstring modułu opisuje okna z taryfy, nie 16–21 |
| `PKG/g13_exec.py` | okno z feedu, `after_window`, `st["window_end"]` |
| `PKG/g13_view.py`, `PKG/static/g13.js` | wg kontraktu |
| `PKG/templates/options.html`, `config.yaml` | tylko jeśli opis trybu ma godziny na sztywno (`grep -n "16–21\|13–16\|16:00\|21:00"`) |
| `tests/test_g13_peak.py` | `step(...)` przyjmuje `window` (domyślnie `Window(16, 21)`); testy: zima 16–21 (dogrzanie 13–16), lato 19–22 (dogrzanie 16–19, szczyt 19–22, powrót 22:00), `NO_PEAK` (święto/G12w) → `None` + powód, `None` → „brak harmonogramu taryfy”, okno `end=24`, bezpiecznik w oknie letnim; `window_from_feed`: pełna doba, dziura w dobie → `None`, brak strefy → `NO_PEAK`, `stale`/`none` bez wpływu; usunąć `workday_from_feed` |
| `tests/test_g13_exec.py` | helper `feed()` wstawia 24 wiersze doby (`zone` `evening_peak` w oknie, inaczej `other`; parametr okna, domyślnie 16–21); testy: zima, lato 19–22 (zapisy o 16/19/22), dzień wolny/G12w (zero zapisów, powód), brak harmonogramu, powrót do bazowego po `end` z oknem z feedu i po restarcie (`window_end` w stanie) |
| `tests/test_g13_view.py` | `next`/`now`/`workday`/`window` z okna letniego i `NO_PEAK`/`None` |

Test: z `/config/addons/heiko_predictive_control/heiko_predictive_control`: `/tmp/claude-950/v/bin/python -m pytest -q -p no:cacheprovider tests/test_g13_peak.py tests/test_g13_exec.py tests/test_g13_view.py`, potem pełny (`timeout 580`, baseline 859 passed).
**DONE:** testy G13 zielone; pełny pytest 0 failed (≥ 859 − usunięte + nowe, podaj bilans); `grep -rnE "PREHEAT_FROM_H|PEAK_FROM_H|WINDOW_END_H|WORKDAY_PROBE_H|workday_from_feed" PKG tests` — 0 trafień; zero zapisów do pompy w dzień bez `evening_peak` (test).
**NIE RÓB:** nie ruszaj `experiments.py`, `tariff_feed.py`, `db.py`, `cycle.py`, `web.py`; nie zmieniaj `Config` ani domyślnych przesunięć; nie podbijaj wersji; `CHANGELOG.md` nie edytuj; nie commituj; nie zgaduj okna z zegara ani stałych (to cel etapu); nie „poprawiaj” sąsiedniego kodu.

## Rollback
`git revert` commitu S5 (G13 wraca do stałych 13/16/21 i dnia roboczego z `is_peak` o 17:00).
