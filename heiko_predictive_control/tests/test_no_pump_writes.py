"""Twarda gwarancja: żaden moduł pętli A (obserwacji) nie ma ścieżki zapisu do urządzeń.
Analiza AST (nie tekstu): liczy się faktyczne użycie `call_service`, nie wzmianka w docstringu.
Jedyne miejsce z zapisem poza `ha_client` to pętla B (klimatyzacja poddasza)."""
import ast
import inspect

import pytest

from heiko_predictive_control import (advisor, analysis, catalog, comfort, cycle, floor_learn, floor_model, floor_plan,
                                       kpi, live, publisher, summaries, telemetry, web)
from heiko_predictive_control.analyzers import anomalies, curve, dhw
import heiko_predictive_control.analyzers as analyzers_pkg

OBSERVING_MODULES = [advisor, analysis, catalog, comfort, floor_learn, floor_model, floor_plan, kpi, live, publisher,
                     summaries, telemetry, web, analyzers_pkg, anomalies, curve, dhw]
WRITE_NAMES = {"call_service"}


def _uses(tree: ast.AST, names: set[str], skip_functions: frozenset[str] = frozenset()) -> list[str]:
    found = []

    def visit(node):
        if isinstance(node, ast.FunctionDef) and node.name in skip_functions:
            return
        if isinstance(node, ast.Name) and node.id in names:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in names:
            found.append(node.attr)
        elif isinstance(node, ast.arg) and node.arg in names:
            found.append(node.arg)
        for child in ast.iter_child_nodes(node):
            visit(child)
    visit(tree)
    return found


@pytest.mark.parametrize("module", OBSERVING_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_observing_module_has_no_service_calls(module):
    assert _uses(ast.parse(inspect.getsource(module)), WRITE_NAMES) == []


def test_cycle_module_writes_only_in_attic_loop():
    tree = ast.parse(inspect.getsource(cycle))
    assert _uses(tree, WRITE_NAMES, frozenset({"run_attic_cycle"})) == []
    assert _uses(ast.parse(inspect.getsource(cycle.run_heiko_cycle)), WRITE_NAMES | {"notify"}) == []


def test_comfort_alarm_only_notifies_the_user():
    tree = ast.parse(inspect.getsource(comfort))
    assert _uses(tree, {"notify"}) != []                       # powiadomienie jest jedyną akcją na zewnątrz
    source = inspect.getsource(comfort)
    assert "climate" not in source and "number." not in source and "switch." not in source


def test_the_detector_itself_works():
    assert _uses(ast.parse("def f(call_service=None):\n    call_service('a', 'b', {})"), WRITE_NAMES)
    assert _uses(ast.parse("x = ha_client.call_service"), WRITE_NAMES)
    assert _uses(ast.parse('"""wzmianka call_service w docstringu"""'), WRITE_NAMES) == []


def test_new_advisor_modules_only_read_from_ha():
    """D1: telemetria, streszczenia i analiza używają wyłącznie odczytów z ha_client."""
    read_only = {"get_all_states", "get_history", "get_logbook", "get_statistics", "get_state", "check_workday"}
    for module in (telemetry, summaries, analysis, catalog):
        tree = ast.parse(inspect.getsource(module))
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "ha_client"}
        assert used <= read_only, (module.__name__, used - read_only)


def test_advisor_d2_uses_only_reads_and_notify_from_ha():
    """D2: silnik doradcy czyta z HA (stany, LTS) i najwyżej powiadamia usera — bez zapisu do pompy."""
    allowed = {"get_statistics", "get_numeric_state", "notify"}
    tree = ast.parse(inspect.getsource(advisor))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "ha_client"}
    assert used <= allowed, used - allowed


def test_analyzers_do_not_touch_ha_at_all():
    for module in (analyzers_pkg, anomalies, curve, dhw):
        assert "ha_client" not in inspect.getsource(module), module.__name__


def test_decision_endpoint_never_calls_a_service(monkeypatch):
    """„Zatwierdź” w D2 = zmiana statusu w bazie. Każde wywołanie usługi HA wywraca test."""
    from datetime import datetime, timedelta

    from heiko_predictive_control import db as dbm
    from heiko_predictive_control import ha_client
    from heiko_predictive_control.analyzers import Draft

    def boom(*a, **k):
        raise AssertionError("decyzja doradcy wywołała usługę HA")
    monkeypatch.setattr(ha_client, "call_service", boom)
    conn = dbm.get_conn(":memory:")
    dbm.migrate(conn)
    now = datetime(2026, 1, 14, 7, 0)
    (pid,) = advisor.sync(conn, [Draft(analyzer="curve", dedupe_key="curve:curve_shift", lens="ekonomia", kind="eksperyment",
                                       reason="x", confidence="niska", ttl_h=6, param_key="curve_shift",
                                       from_value=0.0, to_value=-1.0)], now)
    assert advisor.decide(conn, pid, "zatwierdzona", now + timedelta(minutes=1))["ok"]
