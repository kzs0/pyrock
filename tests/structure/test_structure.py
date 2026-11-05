"""Structural guarantees: stdlib-only imports, dependency boundaries, laziness, API."""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import importlib.resources
import json
import subprocess
import sys
from pathlib import Path

import pytest

import pyrock

SRC = Path(__file__).resolve().parents[2] / "src" / "pyrock"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


MODULES: dict[str, Path] = {_module_name(p): p for p in sorted(SRC.rglob("*.py"))}


def _imports(name: str, path: Path) -> list[tuple[str, int]]:
    """Absolute module names imported by a module (relative imports resolved)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_pkg = path.name == "__init__.py"
    package = name if is_pkg else name.rpartition(".")[0]
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                if node.level > 1:
                    base = base[: -(node.level - 1)]
                mod = ".".join([*base, node.module] if node.module else base)
            else:
                mod = node.module or ""
            out.append((mod, node.lineno))
            # ``from pyrock import x`` may name a submodule
            if mod == "pyrock" or mod.startswith("pyrock."):
                out.extend((f"{mod}.{alias.name}", node.lineno) for alias in node.names)
    return out


def test_every_module_is_discovered() -> None:
    assert "pyrock" in MODULES
    assert len(MODULES) > 30


@pytest.mark.parametrize("name", sorted(MODULES))
def test_parses_as_python_311(name: str) -> None:
    """No 3.12+ syntax (PEP 695 generics, ``type`` statements) in src."""
    source = MODULES[name].read_text(encoding="utf-8")
    ast.parse(source, filename=str(MODULES[name]), feature_version=(3, 11))
    assert "from __future__ import annotations" in source


@pytest.mark.parametrize("name", sorted(MODULES))
def test_imports_only_stdlib_or_pyrock(name: str) -> None:
    bad = [
        f"{mod} (line {line})"
        for mod, line in _imports(name, MODULES[name])
        if mod.split(".")[0] not in sys.stdlib_module_names and mod.split(".")[0] != "pyrock"
    ]
    assert not bad, f"{name} imports non-stdlib modules: {bad}"


# Internal dependency boundaries (DESIGN §3). Keys are the first component under
# ``pyrock``; values are the components that module tree may import. ``None``
# means "top-level module: may import anything". Utility leaves (_types,
# _compat, _version) may be imported by any non-leaf module.
UTILITY = {"_types", "_compat", "_version"}
BOUNDARIES: dict[str, set[str] | None] = {
    # leaves: import nothing from pyrock
    "attr": set(),
    "env": set(),
    "privacy": set(),
    "_types": set(),
    "_compat": set(),
    "_version": set(),
    # services
    "trace": {"attr", "privacy", *UTILITY},
    "metric": {"attr", "privacy", *UTILITY},
    "logs": {"attr", "trace", "privacy", *UTILITY},
    "server": {"metric", *UTILITY},
    "profile": {"attr", "privacy", "trace", *UTILITY},
    "config": {"env", "privacy", "trace", "profile", *UTILITY},
    "_envdetect": {"attr", "privacy", "env", *UTILITY},
    "_push": {"metric", "privacy", *UTILITY},
    # top-level integration modules see everything
    "__init__": None,
    "_bedrock": None,
    "_context": None,
    "_noop": None,
    "_operation": None,
    "wsgi": None,
    "asgi": None,
    "http": None,
    "_cloud": None,
    "testing": None,
}


def _component(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "__init__"


def test_every_component_has_a_boundary_rule() -> None:
    missing = sorted({_component(m) for m in MODULES} - set(BOUNDARIES))
    assert not missing, f"add boundary rules for: {missing}"


@pytest.mark.parametrize("name", sorted(MODULES))
def test_dependency_boundaries(name: str) -> None:
    component = _component(name)
    allowed = BOUNDARIES[component]
    if allowed is None:
        return
    allowed = allowed | {component}
    bad = []
    for mod, line in _imports(name, MODULES[name]):
        if mod == "pyrock":
            bad.append(f"pyrock (line {line})")
        elif mod.startswith("pyrock."):
            target = _component(mod)
            if target not in allowed:
                bad.append(f"{mod} (line {line})")
    assert not bad, f"{name} ({component}) may only import {sorted(allowed)}; found {bad}"


@pytest.mark.parametrize("name", sorted(MODULES))
def test_every_module_imports(name: str) -> None:
    importlib.import_module(name)


LAZY_MODULES = (
    "pyrock.profile",
    "pyrock._cloud",
    "pyrock.trace.otlp.grpc",
    "pyrock.trace.otlp._h2",
    "pyrock.wsgi",
    "pyrock.asgi",
    "pyrock.server",
    "pyrock.http",
    "pyrock.testing",
)
HEAVY_STDLIB = ("asyncio", "ssl", "http.server", "urllib.request", "tracemalloc", "subprocess")


def _loaded_after(code: str) -> set[str]:
    script = f"import json, sys\n{code}\nprint(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_import_pyrock_is_lazy() -> None:
    loaded = _loaded_after("import pyrock")
    heavy = sorted(
        m for m in loaded for lazy in LAZY_MODULES if m == lazy or m.startswith(lazy + ".")
    )
    assert not heavy, f"`import pyrock` loaded lazy modules: {heavy}"
    stdlib = sorted(m for m in HEAVY_STDLIB if m in loaded)
    assert not stdlib, f"`import pyrock` loaded heavy stdlib modules: {stdlib}"


def test_lazy_attributes_load_on_demand() -> None:
    loaded = _loaded_after("import pyrock\npyrock.CloudConfig\npyrock.wsgi")
    assert "pyrock._cloud" in loaded
    assert "pyrock.wsgi" in loaded
    assert "pyrock.trace.otlp.grpc" not in loaded
    assert "pyrock.trace.otlp._h2" not in loaded


EXPECTED_ALL = {
    "AttrValue",
    "Bedrock",
    "CloudConfig",
    "Config",
    "ConfigError",
    "ContextThreadPoolExecutor",
    "Event",
    "Operation",
    "OperationScope",
    "Source",
    "SourceScope",
    "SpanContext",
    "SpanKind",
    "Step",
    "StepScope",
    "Thread",
    "bind",
    "cloud",
    "counter",
    "current",
    "current_operation",
    "current_span",
    "current_span_context",
    "debug",
    "error",
    "gauge",
    "histogram",
    "info",
    "init",
    "inject_headers",
    "log",
    "operation",
    "register",
    "source",
    "start_operation",
    "step",
    "track_loop",
    "warn",
    "warning",
}


def test_public_all_matches_contract() -> None:
    assert len(pyrock.__all__) == len(set(pyrock.__all__)), "duplicate names in __all__"
    assert set(pyrock.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_public_names_resolve(name: str) -> None:
    assert getattr(pyrock, name) is not None


@pytest.mark.parametrize("name", sorted(MODULES))
def test_module_all_resolves(name: str) -> None:
    module = importlib.import_module(name)
    names = getattr(module, "__all__", None)
    if names is None:
        pytest.skip(f"{name} has no __all__")
    missing = [n for n in names if not hasattr(module, n)]
    assert not missing, f"{name}.__all__ names missing attributes: {missing}"
    assert len(names) == len(set(names))


def test_no_public_name_collides_with_a_submodule() -> None:
    """A top-level function must never share its name with a submodule.

    Importing ``pyrock.<name>`` would rebind the attribute to the module.
    """
    submodules = {name.split(".")[1] for name in MODULES if name.count(".") >= 1}
    clashes = sorted(set(pyrock.__all__) & submodules)
    assert not clashes, f"public names shadowed by submodules: {clashes}"


def test_helpers_stay_functions_after_private_modules_load() -> None:
    import logging
    import types

    import pyrock._cloud
    import pyrock._operation
    import pyrock.logs

    for name in ("log", "operation", "cloud", "step", "source", "init"):
        assert isinstance(getattr(pyrock, name), types.FunctionType), name
    assert issubclass(pyrock.logs.CorrelationFilter, logging.Filter)
    cfg = pyrock.cloud("brk_test_key")
    assert isinstance(cfg, pyrock.CloudConfig)
    assert "brk_test_key" not in repr(cfg)


def test_py_typed_and_version() -> None:
    assert (importlib.resources.files("pyrock") / "py.typed").is_file()
    assert importlib.metadata.version("pyrock") == pyrock.__version__


def test_distribution_has_no_runtime_dependencies() -> None:
    assert not importlib.metadata.requires("pyrock")
