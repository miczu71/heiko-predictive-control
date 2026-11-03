"""Pasek pochodzenia energii (static/app.js, HPC.sourceBar): segmenty muszą wypełnić cały pasek."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "heiko_predictive_control" / "static" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="brak node")

SCRIPT = """
const fs = require("fs"), vm = require("vm");
const ctx = { window: {}, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\\nthis.HPC = HPC;", ctx);
ctx.HPC.esc = s => String(s);
const [sources, total] = JSON.parse(process.argv[2]);
console.log(ctx.HPC.sourceBar(sources, total));
"""


def flex_values(sources, total):
    out = subprocess.run(
        ["node", "-e", SCRIPT, str(APP_JS), json.dumps([sources, total])],
        capture_output=True, text=True, check=True,
    ).stdout
    return [float(chunk.split(";")[0].split('"')[0]) for chunk in out.split("flex:")[1:]]


@pytest.mark.parametrize("sources,total", [
    ({"grid:offpeak": {"kwh": 0.343}, "pv": {"kwh": 0.107}}, 0.45),  # doba z małym zużyciem (AC)
    ({"grid:offpeak": {"kwh": 1.097}, "grid:peak": {"kwh": 0.501}, "pv": {"kwh": 0.642}}, 2.24),
    ({"pv": {"kwh": 0.2}}, 0.5),  # część energii bez podziału → segment „bez podziału”
])
def test_segments_sum_to_full_bar(sources, total):
    values = flex_values(sources, total)
    assert len(values) >= 1
    assert sum(values) == pytest.approx(1.0)
