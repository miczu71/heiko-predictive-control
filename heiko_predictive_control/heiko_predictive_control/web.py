"""Flask web UI (ingress-safe: linki przez X-Ingress-Path, fetch-e w JS
wyłącznie względne) — Pulpit / Statystyki / Opcje."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from . import (__version__, advisor, analysis, attic, attic_runs, catalog, comfort, costs, day_summary, dhw_cycles, dhw_model, experiments, floor_learn,
               ha_client, heating_runs, layout, live, rooms, stats, tariff_feed, telemetry)
from . import floor_model, g13_view, kpi
from . import db as dbm
from .tariff import default_is_workday

logger = logging.getLogger(__name__)

COSTS_MAX_DAYS = 400                   # maks. zakres /api/costs
TRACE_CYCLES = 30                      # tyle najnowszych cykli CWU dostaje przebieg w /api/dhw_cycles (tyle rysuje UI)


def _running_test_view(conn) -> Optional[dict]:
    """Test w toku dla kafelki „Zapisy do pompy” na pulpicie: tytuł, godzina i wartość następnego kroku."""
    test = experiments.running_test(conn)
    if test is None:
        return None
    kind = experiments.KINDS.get(test["kind"])
    view = {"title": kind.title if kind else test["kind"], "next_at": None, "next_value": None}
    try:
        schedule = json.loads(test["schedule"])
        step = test["step"]
        if step < len(schedule):
            nxt = schedule[step]
            view["next_at"] = nxt["at"][11:16]
            view["next_value"] = (f"{nxt['value']:g}/{nxt['stop']:g}" if "stop" in nxt else f"{nxt['value']:g}")
    except (ValueError, KeyError, TypeError):
        logger.warning("pulpit: nieczytelny plan testu #%s", test["id"], exc_info=True)
        view["next_at"] = view["next_value"] = None
    return view


def create_app(db_path: str,
               get_settings: Callable[[], dict],
               set_setting: Callable[[str, object], None],
               get_state: Callable[[str], dict | None] = ha_client.get_state,
               get_numeric: Callable[[str], float | None] = ha_client.get_numeric_state,
               house: Optional[rooms.House] = None,
               house_warnings: Optional[list[str]] = None,
               get_states: Callable[[], list[dict] | None] = ha_client.get_all_states,
               compute_report: Callable = analysis.compute_report,
               replay_winter: Callable = advisor.replay_winter,
               is_workday: Callable[[date], bool] = default_is_workday,
               ) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["temp"] = live.fmt_temp
    if house is None:
        house, house_warnings = layout.load_house(None)
    house_warnings = house_warnings or []
    # Geometria domu jest stała — liczona raz przy starcie, nie per żądanie.
    scene = rooms.build_scene(house)
    attic_room, attic_eq = house.by_card("loop_attic")
    pump_eq = next((e for e in house.equipment if e.kind == "pump"), None)

    @app.context_processor
    def inject_version():
        return {"version": __version__}

    @app.after_request
    def cache_headers(resp):
        # WebView Androida (HA Companion) cache'uje HTML bez rewalidacji —
        # patrz CLAUDE.md "Frontend/Asset Caching". Statyki mają długi cache
        # dzięki ?v=<wersja> w base.html; HTML/API zawsze no-store.
        if request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def base() -> str:
        return request.headers.get("X-Ingress-Path", "")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    def db_conn():
        return dbm.get_conn(db_path)

    reduced_switch: dict = {"eid": None, "checked": float("-inf")}

    def reduced_enabled() -> str:
        """Stan switcha „ograniczona nastawa” (slot 77). Encję rozwiązujemy z katalogu (pełna lista stanów jest ciężka,
        a /api/live odpytuje się co minutę): sukces zostaje w pamięci, brak encji (starsza integracja) sprawdzamy
        najwyżej co 10 min, zniknięcie encji wymusza ponowne rozwiązanie."""
        if reduced_switch["eid"] is None and time.monotonic() - reduced_switch["checked"] > 600:
            reduced_switch["checked"] = time.monotonic()
            reduced_switch["eid"] = catalog.resolve_params(get_states() or []).get("reduced_setpoint")
        eid = reduced_switch["eid"]
        state = get_state(eid) if eid else None
        if eid and state is None:
            reduced_switch["eid"] = None
        return live.reduced_enabled_label((state or {}).get("state"))

    def tariff_status(conn=None) -> dict:
        """Źródło taryfy (feed z pv_roi albo zapas G12w) — dla plakietki w UI i `/api/live`."""
        own = conn is None
        conn = conn or db_conn()
        try:
            return tariff_feed.for_settings(conn, get_settings(), is_workday).status()
        finally:
            if own:
                conn.close()

    @app.context_processor
    def inject_tariff_source():
        try:
            return {"tariff_source": tariff_status()["source"]}
        except Exception:                                   # plakietka nie może wywrócić strony
            logger.exception("Stan taryfy nieczytelny")
            return {"tariff_source": "fallback"}

    def live_data(settings: dict) -> dict:
        """live.collect + stan sterowania poddaszem (z bazy) — ten sam słownik
        dla renderu strony i /api/live."""
        data = live.collect(settings, get_state, get_numeric, house)
        data["heiko"]["reduced_enabled"] = reduced_enabled()
        conn = db_conn()
        try:
            row = dbm.latest_cycle(conn, "attic")
            totals = dbm.attic_today_totals(conn, date.today().isoformat())
            heiko_row = dbm.latest_cycle(conn, "heiko")
            feed = tariff_feed.for_settings(conn, settings, is_workday)
            status = feed.status()
            hour = feed.at(datetime.now(ZoneInfo(settings.get("timezone") or "Europe/Warsaw")).replace(tzinfo=None))
        finally:
            conn.close()
        if hour is not None and status["source"] != "fallback":
            data["tariff"] = live.tariff_from_hour(hour)
        data["tariff_source"] = status["source"]
        data["tariff_status"] = status
        if heiko_row is not None:
            data["heiko"]["reduced"] = live.reduced_label(heiko_row["reduced_active"], heiko_row["curve_on"])
        # from_dict: przy braku utrwalonego stanu (dry-run) pokazuje domyślne wartości
        # — te same, które add-on publikuje przez MQTT.
        state = attic.AtticState.from_dict(settings.get("attic_ctrl_state")).as_dict()
        data["attic"].update(live.ctrl_summary(dict(row) if row else None, state, totals))
        return data

    @app.get("/")
    def page_dashboard():
        conn = db_conn()
        try:
            heiko = dbm.latest_cycle(conn, "heiko")
            attic = dbm.latest_cycle(conn, "attic")
            settings = get_settings()
            now = datetime.now(ZoneInfo(settings.get("timezone") or "Europe/Warsaw")).replace(tzinfo=None)
            test = _running_test_view(conn)
            g13_now = None
            if not test:
                try:
                    g13_now = g13_view.now_view(conn, settings, now, is_workday)
                except Exception:                      # kafel pomocniczy nie może położyć Pulpitu
                    logger.warning("pulpit: nie udało się odczytać stanu trybu Szczyt G13", exc_info=True)
            writes = {"test": test,
                      "g13": g13_now,
                      "pending": conn.execute("SELECT COUNT(*) FROM proposals WHERE status = ?",
                                              (advisor.STATUS_PENDING,)).fetchone()[0]}
        finally:
            conn.close()
        return render_template(
            "dashboard.html", base=base(), active="dashboard", writes=writes,
            heiko=dict(heiko) if heiko else None,
            attic=dict(attic) if attic else None,
            settings=settings, scene=scene, house=house, house_warnings=house_warnings,
            attic_room=attic_room, attic_eq=attic_eq, pump_eq=pump_eq,
            live=live_data(settings),
        )

    @app.get("/api/g13")
    def api_g13():
        """Kafel „Szczyt G13” na pulpicie (E4): stan trybu, następna zmiana, energia strefy szczytu, bezpiecznik."""
        settings = get_settings()
        now = datetime.now(ZoneInfo(settings.get("timezone") or "Europe/Warsaw")).replace(tzinfo=None)
        conn = db_conn()
        try:
            return jsonify(g13_view.build(conn, settings, now, is_workday))
        finally:
            conn.close()

    @app.get("/api/live")
    def api_live():
        return jsonify(live_data(get_settings()))

    @app.get("/statistics")
    def page_statistics():
        return render_template("statistics.html", base=base(), active="statistics")

    @app.get("/report")
    def page_report():
        return render_template("report.html", base=base(), active="report")

    @app.get("/cwu")
    def page_cwu():
        return render_template("cwu.html", base=base(), active="cwu")

    @app.get("/ogrzewanie")
    def page_ogrzewanie():
        return render_template("ogrzewanie.html", base=base(), active="ogrzewanie")

    @app.get("/koszty")
    def page_koszty():
        return render_template("koszty.html", base=base(), active="koszty")

    @app.get("/advisor")
    def page_advisor():
        return render_template("advisor.html", base=base(), active="advisor", settings=get_settings())

    @app.get("/tests")
    def page_tests():
        return render_template("tests.html", base=base(), active="tests")

    @app.get("/options")
    def page_options():
        settings = get_settings()
        by_id = {st.get("entity_id"): st for st in (get_states() or [])}
        zones = [{"entity": e, "name": ((by_id.get(e) or {}).get("attributes") or {}).get("friendly_name") or e}
                 for e in advisor.split_csv(settings.get("day_zone_temp_entities"))]
        chosen = set(comfort.min_room_entities(settings))
        return render_template("options.html", base=base(), active="options", settings=settings,
                                zones=zones, chosen=chosen)

    # ── API ──────────────────────────────────────────────────────────────

    @app.get("/api/latest")
    def api_latest():
        conn = db_conn()
        try:
            heiko = dbm.latest_cycle(conn, "heiko")
            attic = dbm.latest_cycle(conn, "attic")
        finally:
            conn.close()
        return jsonify({
            "heiko": dict(heiko) if heiko else None,
            "attic": dict(attic) if attic else None,
        })

    @app.get("/api/history/<loop>")
    def api_history(loop: str):
        if loop not in ("heiko", "attic"):
            return jsonify({"error": "unknown loop"}), 400
        limit = min(int(request.args.get("limit", 200)), 1000)
        conn = db_conn()
        try:
            rows = dbm.recent_cycles(conn, loop, limit)
        finally:
            conn.close()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/stats/<loop>")
    def api_stats(loop: str):
        """Szeregi do wykresów Statystyk: 24 h = każdy cykl, 7 i 30 dni = średnie godzinowe."""
        if loop not in ("heiko", "attic"):
            return jsonify({"error": "unknown loop"}), 400
        try:
            hours = int(request.args.get("hours", 168))
        except ValueError:
            return jsonify({"error": "hours must be a number"}), 400
        if hours not in stats.RANGES_H:
            return jsonify({"error": f"hours must be one of {stats.RANGES_H}"}), 400
        settings = get_settings()
        meta = ({"target_c": float(settings.get("heiko_room_target_c", 20.6)),
                 "room_min_c": float(settings.get("heiko_room_min_c", 18.5))} if loop == "heiko" else {})
        conn = db_conn()
        try:
            return jsonify(stats.build(conn, loop, hours, datetime.now(), meta))
        finally:
            conn.close()

    @app.get("/api/plan")
    def api_plan():
        """Plan pętli A + jakość modelu podłogówki (do pulpitu). Dokładność
        „na żywo” = RMSE błędu prognozy 1 kroku z ostatnich ~24 h cykli."""
        settings = get_settings()
        conn = db_conn()
        baseline = settings.get("peak_baseline")
        try:
            now = datetime.now()
            recent = kpi.recent_kpi(conn, now, baseline)
            price_row = conn.execute("SELECT gross FROM tariff_hours WHERE zone = ? AND hour = ?",
                                     (kpi.EVENING_ZONE, tariff_feed.hour_key(now))).fetchone()
            peak_price = None if price_row is None else round(price_row["gross"], 2)
            errs = [r["model_err_c"] for r in conn.execute(
                "SELECT model_err_c FROM cycles WHERE loop = 'heiko' AND model_err_c IS NOT NULL "
                "ORDER BY id DESC LIMIT 96")]
        finally:
            conn.close()
        model = floor_model.FloorModel.from_dict(settings.get("floor_model_state"))
        live_rmse = (sum(e * e for e in errs) / len(errs)) ** 0.5 if errs else None
        return jsonify({
            "plan": settings.get("heiko_plan"), "model": model.as_dict(),
            "bootstrap": settings.get("floor_bootstrap"), "refit": settings.get("floor_refit"),
            "live_rmse_c": None if live_rmse is None else round(live_rmse, 3),
            "live_samples": len(errs),
            "kpi": recent,
            "peak_price": peak_price,
            "kpi_baseline": None if not baseline else {k: baseline.get(k) for k in ("overall", "hours", "kwh", "at")},
        })

    # ── Doradca D1: raport, katalog parametrów, dziennik zmian (tylko odczyt) ──

    report_job = {"running": False, "error": None}

    def _run_report() -> None:
        conn = db_conn()
        try:
            report = compute_report(conn, get_settings(), datetime.now())
            analysis.save_report(conn, report)
            report_job["error"] = None
        except Exception as exc:                                    # raport w tle nie może wywrócić serwera
            logger.exception("Raport D1 nieudany")
            report_job["error"] = str(exc)
        finally:
            conn.close()
            report_job["running"] = False

    @app.get("/api/report")
    def api_report():
        """Ostatni policzony raport (cache w bazie). `?refresh=1` startuje przeliczenie w tle (LTS: kilka minut)."""
        if request.args.get("refresh") and not report_job["running"]:
            report_job["running"] = True
            threading.Thread(target=_run_report, daemon=True, name="report-d1").start()
        conn = db_conn()
        try:
            report = analysis.load_report(conn)
        finally:
            conn.close()
        return jsonify({"report": report, "running": report_job["running"], "error": report_job["error"]})

    @app.get("/api/catalog")
    def api_catalog():
        """Katalog parametrów pompy + bieżące wartości z HA. Klasy A/B/C, zakresy, znacznik automatyzacji."""
        states = get_states() or []
        by_id = {st.get("entity_id"): st for st in states}
        resolved = catalog.resolve_params(states)
        managed = {k.strip() for k in str(get_settings().get("advisor_managed_keys") or "").split(",") if k.strip()}
        items = []
        for p in catalog.CATALOG:
            row = catalog.describe(p)
            eid = resolved.get(p.key)
            st = by_id.get(eid) or {}
            row.update({"entity_id": eid, "state": st.get("state"), "last_changed": st.get("last_changed"),
                        "managed_by_automation": p.key in managed})
            items.append(row)
        return jsonify({"params": items, "found": len(resolved), "total": len(catalog.CATALOG)})

    @app.get("/api/dhw_cycles")
    def api_dhw_cycles():
        """Dziennik cykli CWU i grzałki AH (A2) + model grzania i koszt AH (M2) — tylko odczyt. `?days=` (domyślnie 60)."""
        days = min(int(request.args.get("days", 60)), 365)
        conn = db_conn()
        try:
            cycles = dhw_cycles.load_cycles(conn, days)
            feed = tariff_feed.for_settings(conn, get_settings(), is_workday)
            tz = ZoneInfo(get_settings().get("timezone") or "Europe/Warsaw")
            model = dhw_model.fit_model(cycles)
            ah = dhw_model.summarize(cycles, model, lambda t: feed.price(t.replace(tzinfo=None)), tz)
            today = datetime.now(tz).date()
            hour_levels = []                                  # strefy taryfy dla tła wykresu startów wg godziny (dziś)
            for h in range(24):
                row = feed.at(datetime(today.year, today.month, today.day, h))
                hour_levels.append({"hour": h, "level": row.level if row else None})
        finally:
            conn.close()
        for c in cycles[TRACE_CYCLES:]:                       # UI rysuje przebieg tylko 30 najnowszych — reszta to zbędny ładunek
            c.pop("trace", None)
        return jsonify({"cycles": cycles, "groups": dhw_cycles.group_by_config(cycles), "model": model, "ah_effect": ah,
                        "hour_levels": hour_levels})

    @app.get("/api/attic_runs")
    def api_attic_runs():
        """Dziennik runów klimatyzacji na poddaszu (A2) — tylko odczyt. `?days=` (domyślnie 60)."""
        days = min(int(request.args.get("days", 60)), 365)
        conn = db_conn()
        try:
            runs = attic_runs.load_runs(conn, days)
        finally:
            conn.close()
        return jsonify({"runs": runs})

    @app.get("/api/heating_runs")
    def api_heating_runs():
        """Dziennik runów podłogówki (E2) — tylko odczyt. `?days=` (domyślnie 60)."""
        days = max(1, min(request.args.get("days", 60, type=int) or 60, 365))
        conn = db_conn()
        try:
            runs = heating_runs.load_runs(conn, days)
        finally:
            conn.close()
        return jsonify({"runs": runs})

    @app.get("/api/day_summary")
    def api_day_summary():
        """Podsumowanie doby (E3): kWh / zł / udział PV — podłoga, CWU, AC. `?day=today|yesterday`. Tylko odczyt."""
        day = request.args.get("day", "today")
        if day not in ("today", "yesterday"):
            return jsonify({"error": "day musi być 'today' albo 'yesterday'"}), 400
        settings = get_settings()
        now = datetime.now(ZoneInfo(settings.get("timezone") or "Europe/Warsaw"))
        conn = db_conn()
        try:
            if day == "yesterday":
                data = day_summary.build_yesterday(conn, settings, now)
            else:
                data = day_summary.build_today(conn, settings, get_states() or [], now,
                                               is_workday=is_workday, cache=day_summary.CACHE)
        finally:
            conn.close()
        return jsonify(data)

    @app.get("/api/costs")
    def api_costs():
        """Koszty (K2): kWh / zł / udział PV w koszykach doba/tydzień/miesiąc + poprzedni okres. `?from=&to=&group=`. Tylko odczyt."""
        settings = get_settings()
        tz = ZoneInfo(settings.get("timezone") or "Europe/Warsaw")
        now = datetime.now(tz)

        def parse(name):
            try:
                return date.fromisoformat(request.args.get(name, "")) if len(request.args.get(name, "")) == 10 else None
            except ValueError:
                return None

        start, end = parse("from"), parse("to")
        group = request.args.get("group", "auto")
        if start is None or end is None:
            error = "from i to muszą mieć format YYYY-MM-DD"
        elif start > end:
            error = "from musi być <= to"
        elif (end - start).days + 1 > COSTS_MAX_DAYS:
            error = f"zakres maks. {COSTS_MAX_DAYS} dni"
        elif end > now.date():
            error = "to nie może być w przyszłości"
        elif group not in costs.GROUPS:
            error = f"group musi być jednym z: {', '.join(costs.GROUPS)}"
        else:
            error = None
        if error:
            return jsonify({"error": error}), 400
        conn = db_conn()
        try:
            data = costs.build(conn, settings, start, end, now, group,
                               today_fn=lambda: day_summary.build_today(conn, settings, get_states() or [], now,
                                                                        is_workday=is_workday, cache=day_summary.CACHE))
        finally:
            conn.close()
        return jsonify(data)

    @app.get("/api/changes")
    def api_changes():
        """Dziennik zmian parametrów (licznik zapisów do pamięci pompy — dowolne źródło) + stan telemetrii."""
        conn = db_conn()
        try:
            rows = conn.execute("SELECT ts, key, old, new, source FROM param_changes ORDER BY id DESC LIMIT 100").fetchall()
            summary = telemetry.changes_summary(conn, datetime.now())
            samples = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
            last = conn.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0]
            size = telemetry.db_size_bytes(conn)
        finally:
            conn.close()
        return jsonify({"changes": [dict(r) for r in rows], "summary": summary,
                        "telemetry": {"rows": samples, "last_ts": last, "db_bytes": size,
                                      "db_warn": size > telemetry.DB_WARN_BYTES}})

    # ── Doradca D2: propozycje (decyzje próbne; zapis do pompy tylko slot 52 po akceptacji, H4) i odtworzenie zimy ──

    @app.get("/api/proposals")
    def api_proposals():
        settings = get_settings()
        conn = db_conn()
        try:
            data = advisor.list_proposals(conn, datetime.now())
        finally:
            conn.close()
        data["lens"] = settings.get("advisor_lens") or advisor.LENS_OFF
        return jsonify(data)

    @app.post("/api/proposals/<int:proposal_id>/decision")
    def api_proposal_decision(proposal_id: int):
        decision = (request.get_json(silent=True) or {}).get("decision", "")
        now = datetime.now()
        revalidate = apply = None
        if decision == advisor.STATUS_APPROVED:
            states = get_states()
            if not states:
                return jsonify({"ok": False, "error": "brak połączenia z HA — nie mogę sprawdzić, czy warunki nadal są aktualne"}), 503
            conn = db_conn()
            try:
                revalidate = advisor.make_revalidator(conn, get_settings(), now, states)
            finally:
                conn.close()

            def apply(row: dict) -> tuple[bool, str]:
                entity = catalog.resolve_params(states).get(row["param_key"])
                if not entity:
                    return False, "nie znajduję encji slotu 52 w HA"
                wconn = db_conn()
                try:
                    ok = experiments.write_setting(wconn, entity, row["from_value"], row["to_value"], now)
                except ValueError:
                    return False, "zapis poza dozwolonym zakresem"
                except RuntimeError as exc:                          # trwa test krzywej/P0
                    return False, str(exc)
                finally:
                    wconn.close()
                return (True, "") if ok else (False, "HA nie przyjęło zapisu")
        conn = db_conn()
        try:
            result = advisor.decide(conn, proposal_id, decision, now, revalidate, apply)
        finally:
            conn.close()
        return jsonify(result), (200 if result["ok"] else 409)

    replay_job = {"running": False, "error": None}

    def _run_replay() -> None:
        conn = db_conn()
        try:
            result = replay_winter(get_settings(), datetime.now(), floor_learn.load_model(conn))
            advisor.save_replay(conn, result)
            replay_job["error"] = result.get("error")
        except Exception as exc:                                    # odtworzenie w tle nie może wywrócić serwera
            logger.exception("Odtworzenie zimy nieudane")
            replay_job["error"] = str(exc)
        finally:
            conn.close()
            replay_job["running"] = False

    @app.get("/api/advisor/replay")
    def api_advisor_replay():
        """Ostatnie odtworzenie zimy dla analizatora krzywej (cache). `?refresh=1` liczy w tle (LTS: minuty)."""
        if request.args.get("refresh") and not replay_job["running"]:
            replay_job["running"] = True
            threading.Thread(target=_run_replay, daemon=True, name="advisor-replay").start()
        conn = db_conn()
        try:
            replay = advisor.load_replay(conn)
        finally:
            conn.close()
        return jsonify({"replay": replay, "running": replay_job["running"], "error": replay_job["error"]})

    # ── Doradca D3a: testy uruchamiane przez usera (zapisy wyłącznie w experiments.py) ──

    def tests_overview(conn) -> dict:
        return experiments.overview(conn, get_settings(), datetime.now(), get_state, get_numeric, is_workday)

    @app.get("/api/tests")
    def api_tests():
        conn = db_conn()
        try:
            return jsonify(tests_overview(conn))
        finally:
            conn.close()

    @app.post("/api/tests/<kind>/start")
    def api_test_start(kind: str):
        """Start = zgoda na cały harmonogram; pierwszy krok od razu (ten sam wykonawca co zadanie co 5 min)."""
        settings, now = get_settings(), datetime.now()
        conn = db_conn()
        try:
            result = experiments.start(conn, settings, kind, now, get_state, get_numeric, is_workday)
            if result["ok"]:
                experiments.tick(conn, settings, now, get_numeric)
            return jsonify({**result, **tests_overview(conn)}), (200 if result["ok"] else 409)
        finally:
            conn.close()

    @app.post("/api/tests/abort")
    def api_test_abort():
        conn = db_conn()
        try:
            result = experiments.abort(conn, get_settings(), datetime.now())
            return jsonify({**result, **tests_overview(conn)}), (200 if result["ok"] else 409)
        finally:
            conn.close()

    @app.get("/api/settings")
    def api_get_settings():
        return jsonify(get_settings())

    @app.post("/api/settings")
    def api_set_settings():
        payload = request.get_json(silent=True) or {}
        for key, value in payload.items():
            set_setting(key, value)
        return jsonify({"ok": True, "settings": get_settings()})

    return app
