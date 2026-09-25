"""Flask web UI (ingress-safe: linki przez X-Ingress-Path, fetch-e w JS
wyłącznie względne) — Pulpit / Statystyki / Opcje."""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Optional

from flask import Flask, jsonify, render_template, request

from . import __version__, ha_client, layout, live, rooms
from . import db as dbm

logger = logging.getLogger(__name__)


def create_app(db_path: str,
               get_settings: Callable[[], dict],
               set_setting: Callable[[str, object], None],
               get_state: Callable[[str], dict | None] = ha_client.get_state,
               get_numeric: Callable[[str], float | None] = ha_client.get_numeric_state,
               house: Optional[rooms.House] = None,
               house_warnings: Optional[list[str]] = None,
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

    @app.get("/")
    def page_dashboard():
        conn = db_conn()
        try:
            heiko = dbm.latest_cycle(conn, "heiko")
            attic = dbm.latest_cycle(conn, "attic")
        finally:
            conn.close()
        settings = get_settings()
        return render_template(
            "dashboard.html", base=base(), active="dashboard",
            heiko=dict(heiko) if heiko else None,
            attic=dict(attic) if attic else None,
            settings=settings, scene=scene, house=house, house_warnings=house_warnings,
            attic_room=attic_room, attic_eq=attic_eq, pump_eq=pump_eq,
            live=live.collect(settings, get_state, get_numeric, house),
        )

    @app.get("/api/live")
    def api_live():
        return jsonify(live.collect(get_settings(), get_state, get_numeric, house))

    @app.get("/statistics")
    def page_statistics():
        return render_template("statistics.html", base=base(), active="statistics")

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
        baseline = sum((r["baseline_cost_today_pln"] or 0) for r in rows)
        komfort = sum((r["sim_cost_today_komfort_pln"] or 0) for r in rows)
        ekonomia = sum((r["sim_cost_today_ekonomia_pln"] or 0) for r in rows)
        return jsonify({
            "baseline_pln": round(baseline, 2),
            "komfort_pln": round(komfort, 2),
            "ekonomia_pln": round(ekonomia, 2),
            "savings_komfort_pln": round(baseline - komfort, 2),
            "savings_ekonomia_pln": round(baseline - ekonomia, 2),
            "cycles": len(rows),
        })

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
