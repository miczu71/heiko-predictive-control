"""API definicji testów (E5a): CRUD, walidacja wejścia z sieci, podgląd, eksport, `test_overlay` kafla G13."""
import ast
import inspect
import json
from datetime import date, datetime, timedelta

import pytest

from heiko_predictive_control import db as dbm
from heiko_predictive_control import experiments as ex
from heiko_predictive_control import g13_view, ha_client
from heiko_predictive_control.testkit import defs, engine
from heiko_predictive_control.web import create_app

T0 = datetime(2026, 10, 7, 10, 0)                       # środa
SHIFT = "number.heiko_heat_pump_heating_curve_parallel_shift"
SETTINGS = {"heiko_enabled": True, "heiko_curve_switch_entity": "switch.curve", "heiko_curve_shift_entity": SHIFT,
            "timezone": "Europe/Warsaw", "day_zone_temp_entities": "sensor.salon"}

PREHEAT = {
    "key": "preheat_ab", "title": "Preheat A/B", "design": "alternate", "order": ["B", "A"],
    "arms": {"A": {"overrides": {"g13_peak_preheat_shift": 1}}, "B": {"overrides": {"g13_peak_preheat_shift": 0}}},
    "days": {"filter": "peak_day", "count": 20}, "coexist": ["g13_peak"],
    "measures": [{"name": "floor_cost", "window": "13:00→13:00", "per": "hdd"}, {"name": "min_room_at", "at": "21:00"},
                 "safety_trips", "kwh_day"],
    "verdict": {"primary": "floor_cost", "better": "lower",
                "guards": [{"measure": "min_room_at", "op": ">=", "value": 20.5}], "weather_band_c": 3, "min_n": 6},
}


def workday(d: date) -> bool:
    return d.weekday() < 5


@pytest.fixture(autouse=True)
def no_ha(monkeypatch):
    monkeypatch.setattr(ex.ha_client, "get_all_states", lambda: [])
    monkeypatch.setattr(ha_client, "get_all_states", lambda: [])
    monkeypatch.setattr(ex, "_day_kwh", lambda c, s, d, n: None)


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.migrate(c)
    engine.seed_builtin(c)
    return c


@pytest.fixture
def api(tmp_path):
    db_path = str(tmp_path / "t.db")
    c = dbm.get_conn(db_path)
    dbm.migrate(c)
    engine.seed_builtin(c)
    c.close()
    settings = dict(SETTINGS)
    app = create_app(db_path, lambda: settings, lambda k, v: settings.update({k: v}),
                     get_state=lambda e: {"state": "off"} if e == "switch.curve" else None,
                     get_numeric=lambda e: 0.0 if e == SHIFT else None, is_workday=workday)
    client = app.test_client()
    client.db_path, client.settings = db_path, settings
    return client


def rows(client):
    c = dbm.get_conn(client.db_path)
    try:
        return {r["key"]: r for r in c.execute("SELECT * FROM test_defs")}
    finally:
        c.close()


def own(key="moj_test", **extra):
    return {**PREHEAT, "key": key, "title": "Mój test", **extra}


# --- czyste funkcje ---

def test_validate_key():
    assert defs.validate_key("moj_test") is None and defs.validate_key("ab") is None
    for bad in ("", "a", "Abc", "1abc", "a-b", "a" * 41, None, 5, "a b"):
        assert defs.validate_key(bad), bad


def test_create_update_delete_cycle(conn):
    assert defs.create(conn, own(), T0) == (True, None, 201)
    assert defs.get(conn, "moj_test") == {"definition": own(), "builtin": False}
    changed = own(title="Zmieniony")
    assert defs.update(conn, "moj_test", changed, T0 + timedelta(hours=1)) == (True, None, 200)
    assert defs.get(conn, "moj_test")["definition"]["title"] == "Zmieniony"
    row = conn.execute("SELECT created, updated, builtin FROM test_defs WHERE key = 'moj_test'").fetchone()
    assert row["created"] != row["updated"] and row["builtin"] == 0
    assert defs.delete(conn, "moj_test") == (True, None, 200)
    assert defs.get(conn, "moj_test") is None
    assert defs.delete(conn, "moj_test")[2] == 404
    assert defs.update(conn, "moj_test", own(), T0)[2] == 404


def test_builtin_read_only_and_copy_allowed(conn):
    for key in ("zapis", "bezwladnosc", "p0_ab"):
        item = defs.get(conn, key)
        assert item["builtin"] is True
        ok, errors, status = defs.update(conn, key, item["definition"], T0)
        assert (ok, status) == (False, 403) and errors
        assert defs.delete(conn, key)[2] == 403
        assert defs.get(conn, key)["definition"] == item["definition"]
    copy = {**defs.get(conn, "zapis")["definition"], "key": "zapis_kopia", "title": "Kopia zapisu"}
    assert defs.create(conn, copy, T0) == (True, None, 201)
    assert defs.get(conn, "zapis_kopia")["builtin"] is False


def test_builtin_flag_in_row_also_read_only(conn):
    conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES ('wbud_x', ?, 1, 'x', 'x')",
                 (json.dumps(own("wbud_x")),))
    conn.commit()
    assert defs.update(conn, "wbud_x", own("wbud_x"), T0)[2] == 403
    assert defs.delete(conn, "wbud_x")[2] == 403
    assert defs.get(conn, "wbud_x")["builtin"] is True


def test_create_rejects_bad_duplicate_and_reserved_keys(conn):
    assert defs.create(conn, own("Zly-Klucz"), T0)[2] == 400
    assert defs.create(conn, {k: v for k, v in own().items() if k != "key"}, T0)[2] == 400
    assert defs.create(conn, own(), T0)[0] is True
    ok, errors, status = defs.create(conn, own(), T0)
    assert (ok, status) == (False, 409) and "już istnieje" in errors[0]
    ok, errors, status = defs.create(conn, own("p0_ab"), T0)
    assert (ok, status) == (False, 409) and "wbudowan" in errors[0]


def test_update_key_mismatch_is_400_and_invalid_not_saved(conn):
    defs.create(conn, own(), T0)
    ok, errors, status = defs.update(conn, "moj_test", own("inny_klucz"), T0)
    assert (ok, status) == (False, 400) and "key" in errors[0]
    ok, errors, status = defs.update(conn, "moj_test", {**own(), "nieznane_pole": 1}, T0)
    assert (ok, status) == (False, 400)
    assert defs.get(conn, "moj_test")["definition"] == own()


def test_list_all_flags_builtin_and_broken(conn):
    defs.create(conn, own(), T0)
    conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES ('zepsuty', '{', 0, 'x', 'x')")
    conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES ('niepelny', ?, 0, 'x', 'x')",
                 (json.dumps({"key": "niepelny", "title": "Niepełny"}),))
    conn.commit()
    items = defs.list_all(conn)
    assert [i["key"] for i in items] == ["zapis", "bezwladnosc", "p0_ab", "moj_test", "niepelny", "zepsuty"]
    by = {i["key"]: i for i in items}
    assert all(by[k]["builtin"] and by[k]["errors"] == [] for k in ("zapis", "bezwladnosc", "p0_ab"))
    assert by["moj_test"] == {"key": "moj_test", "title": "Mój test", "description": "", "builtin": False, "errors": []}
    assert by["niepelny"]["errors"] and by["zepsuty"]["errors"] == ["zapisany JSON jest uszkodzony"]


def test_export_all_has_only_own_definitions(conn):
    defs.create(conn, own("b_test"), T0)
    defs.create(conn, own("a_test"), T0)
    conn.execute("INSERT INTO test_defs (key, def_json, builtin, created, updated) VALUES ('zepsuty', '{', 0, 'x', 'x')")
    conn.commit()
    out = defs.export_all(conn)
    assert out["version"] == 1 and [d["key"] for d in out["definitions"]] == ["a_test", "b_test"]


def test_edit_does_not_change_running_test_and_delete_keeps_frozen_def(conn):
    defs.create(conn, own(), T0)
    res = ex.start(conn, SETTINGS, "moj_test", T0, lambda e: {"state": "on"} if e == "switch.curve" else None,
                   lambda e: 0.0, workday)
    assert res["ok"], res
    frozen = conn.execute("SELECT def_json FROM tests WHERE id = ?", (res["id"],)).fetchone()["def_json"]
    assert defs.update(conn, "moj_test", own(title="Po edycji"), T0)[0] is True
    assert conn.execute("SELECT def_json FROM tests WHERE id = ?", (res["id"],)).fetchone()["def_json"] == frozen
    assert defs.delete(conn, "moj_test")[0] is True
    test = conn.execute("SELECT * FROM tests WHERE id = ?", (res["id"],)).fetchone()
    assert engine.get_def(conn, "moj_test") is None
    assert engine.frozen_def(test)["title"] == "Mój test"
    history = ex.overview(conn, SETTINGS, T0, lambda e: None, lambda e: None, workday)["history"]
    assert history[0]["kind"] == "moj_test"


def test_preview_for_preheat_ab(conn):
    now = datetime(2026, 10, 7, 10, 0)
    out = defs.preview(conn, SETTINGS, PREHEAT, now, lambda e: {"state": "off"}, lambda e: 0.0, workday)
    assert out["ok"] is True and out["errors"] == []
    pv = out["preview"]
    assert set(pv) == {"schedule", "arms_by_day", "blocked", "end_at", "writes"}
    assert pv["writes"] is False and pv["schedule"] == []
    assert 0 < len(pv["arms_by_day"]) <= 14
    days = list(pv["arms_by_day"])
    assert days == sorted(days) and days[0] >= now.date().isoformat()
    assert list(pv["arms_by_day"].values())[:4] == ["B", "A", "B", "A"]
    assert pv["end_at"] and pv["blocked"] is None
    conn.execute("DELETE FROM test_defs WHERE key = 'moj_test'")
    assert conn.execute("SELECT COUNT(*) FROM tests").fetchone()[0] == 0      # podgląd nic nie zapisuje


def test_preview_blocked_when_g13_on_without_coexist(conn):
    without = {k: v for k, v in PREHEAT.items() if k != "coexist"}
    settings = {**SETTINGS, "g13_peak_enabled": True}
    pv = defs.preview(conn, settings, without, T0, lambda e: None, lambda e: 0.0, workday)["preview"]
    assert "Szczyt G13" in pv["blocked"]
    with_coexist = defs.preview(conn, settings, PREHEAT, T0, lambda e: None, lambda e: 0.0, workday)["preview"]
    assert with_coexist["blocked"] is None


def test_preview_invalid_has_null_preview(conn):
    out = defs.preview(conn, SETTINGS, {**PREHEAT, "wat": 1}, T0, lambda e: None, lambda e: 0.0, workday)
    assert out["ok"] is False and out["preview"] is None and out["errors"]
    assert defs.preview(conn, SETTINGS, [], T0, lambda e: None, lambda e: 0.0, workday)["preview"] is None


def test_preview_with_writes_has_schedule(conn):
    out = defs.preview(conn, SETTINGS, defs.get(conn, "zapis")["definition"], T0, lambda e: {"state": "off"},
                       lambda e: 0.0, workday)
    pv = out["preview"]
    assert pv["writes"] is True and pv["schedule"] and pv["end_at"] is None


# --- HTTP ---

def test_http_crud_cycle(api):
    listed = api.get("/api/test-defs").get_json()
    assert [d["key"] for d in listed["definitions"]][:3] == ["zapis", "bezwladnosc", "p0_ab"]
    assert all(d["builtin"] for d in listed["definitions"])
    created = api.post("/api/test-defs", json=own())
    assert created.status_code == 201 and created.get_json()["ok"] is True
    got = api.get("/api/test-defs/moj_test")
    assert got.status_code == 200 and got.get_json() == {"definition": own(), "builtin": False}
    put = api.put("/api/test-defs/moj_test", json=own(title="Nowy tytuł"))
    assert put.status_code == 200 and api.get("/api/test-defs/moj_test").get_json()["definition"]["title"] == "Nowy tytuł"
    assert api.delete("/api/test-defs/moj_test").status_code == 200
    assert api.get("/api/test-defs/moj_test").status_code == 404
    assert api.delete("/api/test-defs/moj_test").status_code == 404
    assert api.put("/api/test-defs/moj_test", json=own()).status_code == 404


def test_http_builtin_403_and_copy(api):
    zapis = api.get("/api/test-defs/zapis").get_json()
    assert zapis["builtin"] is True
    assert api.put("/api/test-defs/zapis", json=zapis["definition"]).status_code == 403
    assert api.delete("/api/test-defs/zapis").status_code == 403
    copy = {**zapis["definition"], "key": "zapis_kopia", "title": "Kopia"}
    assert api.post("/api/test-defs", json=copy).status_code == 201
    assert api.post("/api/test-defs", json=copy).status_code == 409
    assert api.post("/api/test-defs", json={**copy, "key": "zapis"}).status_code == 409
    assert api.post("/api/test-defs", json={**copy, "key": "BAD KEY"}).status_code == 400
    assert api.put("/api/test-defs/zapis_kopia", json={**copy, "key": "inny"}).status_code == 400


@pytest.mark.parametrize("body", [b"", b"[]", b'"x"', b"5", b"null", b"{", b"\xff\xfe", b"[" * 5000, b"{}"])
def test_http_bad_bodies_are_400_without_write(api, body):
    for method, url in (("post", "/api/test-defs"), ("put", "/api/test-defs/moj_test")):
        resp = getattr(api, method)(url, data=body, content_type="application/json")
        assert resp.status_code in (400, 404), (body[:10], resp.status_code)
        if resp.status_code == 400:
            assert resp.get_json()["ok"] is False and resp.get_json()["errors"]
    resp = api.post("/api/test-defs/validate", data=body, content_type="application/json")
    data = resp.get_json()
    assert resp.status_code == (200 if body == b"{}" else 400) and data["ok"] is False and data["errors"]
    assert set(rows(api)) == {"zapis", "bezwladnosc", "p0_ab"}


def test_http_oversized_body_413_without_write(api):
    big = json.dumps(own(description="x" * 70000)).encode()
    assert len(big) > defs.MAX_BYTES
    for url in ("/api/test-defs", "/api/test-defs/validate"):
        assert api.post(url, data=big, content_type="application/json").status_code == 413
    assert api.put("/api/test-defs/moj_test", data=big, content_type="application/json").status_code == 413
    assert "moj_test" not in rows(api)


def test_http_chunked_oversized_body_413(api):
    big = json.dumps(own(description="x" * 70000)).encode()
    resp = api.post("/api/test-defs", data=big, content_type="application/json",
                    environ_overrides={"CONTENT_LENGTH": ""})
    assert resp.status_code in (400, 413) and "moj_test" not in rows(api)


def test_http_schema_errors_are_400_with_message(api):
    cases = {
        "nieznane pole": {**own(), "bogus": 1},
        "opcja spoza rejestru": {**own(), "arms": {"A": {"overrides": {"heiko_enabled": 1}},
                                                  "B": {"overrides": {"g13_peak_preheat_shift": 0}}}},
        "writes spoza allowlisty": {**own(), "design": "single", "order": ["A"], "coexist": [],
                                    "arms": {"A": {"writes": [{"param": "dhw_hack", "value": 5}]}}},
    }
    deep = {"a": 1}
    for _ in range(10):
        deep = {"x": deep}
    cases["głębokie zagnieżdżenie"] = {**own(), "extra": deep}
    for name, body in cases.items():
        resp = api.post("/api/test-defs", json=body)
        data = resp.get_json()
        assert resp.status_code == 400 and data["ok"] is False and data["errors"], name
        assert all(isinstance(e, str) and e for e in data["errors"]), name
    assert "moj_test" not in rows(api)


def test_http_validate_returns_preview(api):
    resp = api.post("/api/test-defs/validate", json=PREHEAT)
    data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] is True and data["errors"] == []
    pv = data["preview"]
    assert pv["writes"] is False and len(pv["arms_by_day"]) <= 14 and list(pv["arms_by_day"].values())[0] == "B"
    assert pv["schedule"] == [] and pv["blocked"] is None
    api.settings["g13_peak_enabled"] = True
    blocked = api.post("/api/test-defs/validate", json={k: v for k, v in PREHEAT.items() if k != "coexist"}).get_json()
    assert "Szczyt G13" in blocked["preview"]["blocked"]
    bad = api.post("/api/test-defs/validate", json={**PREHEAT, "bogus": 1})
    assert bad.status_code == 200 and bad.get_json()["ok"] is False and bad.get_json()["preview"] is None
    assert "moj_test" not in rows(api) and "preheat_ab" not in rows(api)


def test_http_export(api):
    api.post("/api/test-defs", json=own())
    data = api.get("/api/test-defs/export").get_json()
    assert data == {"version": 1, "definitions": [own()]}


def test_http_edit_does_not_change_running_test(api):
    api.settings["heiko_enabled"] = True
    api.post("/api/test-defs", json=own())
    c = dbm.get_conn(api.db_path)
    now = datetime.now()
    assert ex.start(c, api.settings, "moj_test", now, lambda e: {"state": "on"}, lambda e: 0.0, workday)["ok"]
    before = c.execute("SELECT def_json FROM tests").fetchone()["def_json"]
    c.close()
    assert api.put("/api/test-defs/moj_test", json=own(title="Po")).status_code == 200
    assert api.delete("/api/test-defs/moj_test").status_code == 200
    c = dbm.get_conn(api.db_path)
    try:
        assert c.execute("SELECT def_json FROM tests").fetchone()["def_json"] == before
    finally:
        c.close()


# --- kafel G13 ---

def test_g13_view_test_overlay(conn):
    defs.create(conn, own(), T0)
    now = datetime.now()
    assert g13_view.build(conn, {**SETTINGS}, now)["test_overlay"] is None
    res = ex.start(conn, {**SETTINGS, "g13_peak_enabled": True}, "moj_test", now,
                   lambda e: {"state": "on"} if e == "switch.curve" else None, lambda e: 0.0, workday)
    assert res["ok"], res
    arm = engine.arm_for_day(conn.execute("SELECT * FROM tests").fetchone(), now.date())
    view = g13_view.build(conn, {**SETTINGS}, now)
    if arm is None:                                              # dziś nie jest dobą wariantu (weekend)
        assert view["test_overlay"] is None
        day = next(d for d in json.loads(conn.execute("SELECT arms_json FROM tests").fetchone()[0]))
        now = datetime.combine(date.fromisoformat(day), now.time())
        arm = engine.arm_for_day(conn.execute("SELECT * FROM tests").fetchone(), now.date())
        view = g13_view.build(conn, {**SETTINGS}, now)
    assert view["test_overlay"] == {"title": "Mój test", "arm": arm,
                                    "overrides": PREHEAT["arms"][arm]["overrides"]}
    assert {"status", "today", "week", "trips_7d", "next", "now"} <= set(view)


def test_no_pump_writes_in_defs():
    tree = ast.parse(inspect.getsource(defs))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree)
                                                                                 if isinstance(n, ast.Name)}
    assert "call_service" not in names
