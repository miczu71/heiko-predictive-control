"""Flask web UI (ingress-safe: linki przez X-Ingress-Path, fetch-e w JS
wyłącznie względne) — Pulpit / Statystyki / Opcje."""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Callable, Optional

from flask import Flask, jsonify, render_template, request

from . import __version__, analysis, attic, catalog, ha_client, layout, live, rooms, telemetry
from . import floor_model, kpi
from . import db as dbm

logger = logging.getLogger(__name__)


def create_app(db_path: str,
               get_settings: Callable[[], dict],
               set_setting: Callable[[str, object], None],
               get_state: Callable[[str], dict | None] = ha_client.get_state,
               get_numeric: Callable[[str], float | None] = ha_client.get_numeric_state,
               house: Optional[rooms.House] = None,
               house_warnings: Optional[list[str]] = None,
               get_states: Callable[[], list[dict] | None] = ha_client.get_all_states,
               compute_report: Callable = analysis.compute_report,
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

    def live_data(settings: dict) -> dict:
        """live.collect + stan sterowania poddaszem (z bazy) — ten sam słownik
        dla renderu strony i /api/live."""
        data = live.collect(settings, get_state, get_numeric, house)
        conn = db_conn()
        try:
            row = dbm.latest_cycle(conn, "attic")
            totals = dbm.attic_today_totals(conn, date.today().isoformat())
            heiko_row = dbm.latest_cycle(conn, "heiko")
        finally:
            conn.close()
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
            changes = telemetry.changes_summary(conn, datetime.now())
        finally:
            conn.close()
        settings = get_settings()
        return render_template(
            "dashboard.html", base=base(), active="dashboard", changes=changes,
            heiko=dict(heiko) if heiko else None,
            attic=dict(attic) if attic else None,
            settings=settings, scene=scene, house=house, house_warnings=house_warnings,
            attic_room=attic_room, attic_eq=attic_eq, pump_eq=pump_eq,
            live=live_data(settings),
        )

    @app.get("/api/live")
    def api_live():
        return jsonify(live_data(get_settings()))

    @app.get("/statistics")
    def page_statistics():
        return render_template("statistics.html", base=base(), active="statistics")

    @app.get("/report")
    def page_report():
        return render_template("report.html", base=base(), active="report")

    @app.get("/options")
    def page_options():
        return render_template("options.html", base=base(), active="options",
                                settings=get_settings())

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

    @app.get("/api/today_summary/<loop>")
    def api_today_summary(loop: str):
        if loop not in ("heiko", "attic"):
            return jsonify({"error": "unknown loop"}), 400
        conn = db_conn()
        try:
            rows = dbm.today_cycles(conn, loop, date.today().isoformat())
        finally:
            conn.close()
        actual = sum((r["energy_kwh"] or 0) * (r["price_pln_kwh"] or 0) for r in rows
                     if "energy_kwh" in r.keys())
        baseline = sum((r["baseline_cost_today_pln"] or 0) for r in rows)
        komfort = sum((r["sim_cost_today_komfort_pln"] or 0) for r in rows)
        ekonomia = sum((r["sim_cost_today_ekonomia_pln"] or 0) for r in rows)
        return jsonify({
            "actual_pln": round(actual, 2),
            "baseline_pln": round(baseline, 2),
            "komfort_pln": round(komfort, 2),
            "ekonomia_pln": round(ekonomia, 2),
            "savings_komfort_pln": round(baseline - komfort, 2),
            "savings_ekonomia_pln": round(baseline - ekonomia, 2),
            "cycles": len(rows),
        })

    @app.get("/api/plan")
    def api_plan():
        """Plan pętli A + jakość modelu podłogówki (do pulpitu). Dokładność
        „na żywo” = RMSE błędu prognozy 1 kroku z ostatnich ~24 h cykli."""
        settings = get_settings()
        conn = db_conn()
        baseline = settings.get("peak_baseline")
        try:
            recent = kpi.recent_kpi(conn, datetime.now(), baseline)
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
