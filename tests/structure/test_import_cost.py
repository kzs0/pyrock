"""``import pyrock`` stays cheap: no avoidable modules, and a generous time budget.

Each measurement runs in a fresh interpreter. The budget is deliberately loose
(several times the cost on a developer machine) so that slow CI runners do not
flake; it exists to catch regressions such as an eager import of profiling,
the server or :mod:`asyncio`, not to track milliseconds.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys

IMPORT_BUDGET_MS = 100.0
"""Median wall time of ``import pyrock`` itself, its stdlib dependencies included."""

RUNS = 5

DEFERRED = (
    "json",
    "concurrent.futures",
    "pyrock.trace.http",
    "pyrock.trace.w3c",
    "pyrock.trace.otlp",
    "pyrock.trace.otlp.batch",
    "pyrock.metric.prometheus",
)
"""Loaded on first use only (the propagator, the export pipeline, the executor...)."""


def _run(code: str) -> str:
    # Measure a clean interpreter: coverage's subprocess patch (COVERAGE_*)
    # would otherwise start coverage in the child, which imports json & co.
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    out = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return out.stdout.strip().splitlines()[-1]


def _import_ms() -> float:
    code = "import time\nt = time.perf_counter()\nimport pyrock\nprint(time.perf_counter() - t)"
    return float(_run(code)) * 1000


def test_import_pyrock_within_budget() -> None:
    times = [_import_ms() for _ in range(RUNS)]
    median = statistics.median(times)
    assert median <= IMPORT_BUDGET_MS, f"import pyrock took {median:.1f} ms (runs: {times})"


def test_import_pyrock_defers_first_use_modules() -> None:
    loaded = set(
        json.loads(_run("import json, sys\nimport pyrock\nprint(json.dumps(sorted(sys.modules)))"))
    )
    # json is imported by the probe itself; check it through a fresh interpreter instead.
    eager = sorted(m for m in DEFERRED if m != "json" and m in loaded)
    assert not eager, f"`import pyrock` loaded first-use modules: {eager}"
    assert _run("import sys, pyrock\nprint('json' in sys.modules)") == "False"


def test_pytest_plugin_module_defers_its_extras() -> None:
    code = "import sys, pyrock.testing\nprint(sorted(m for m in {mods!r} if m in sys.modules))"
    assert _run(code.format(mods=DEFERRED)) == "[]"
