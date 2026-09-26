"""Twarda gwarancja: żaden moduł pętli A (obserwacji) nie ma ścieżki zapisu do urządzeń.
Analiza AST (nie tekstu): liczy się faktyczne użycie `call_service`, nie wzmianka w docstringu.
Jedyne miejsce z zapisem poza `ha_client` to pętla B (klimatyzacja poddasza)."""
import ast
import inspect

import pytest

from heiko_predictive_control import (comfort, cycle, floor_learn, floor_model, floor_plan,
                                       kpi, live, publisher, web)

OBSERVING_MODULES = [comfort, floor_learn, floor_model, floor_plan, kpi, live, publisher, web]
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
