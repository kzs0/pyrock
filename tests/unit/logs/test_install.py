"""Install/restore helpers used by init() (log_install root/pyrock/none) plus edge cases."""

from __future__ import annotations

import io
import json
import logging
import threading

import pytest

from pyrock import logs, privacy
from pyrock.logs import ATTRS_KEY, BedrockHandler, JSONFormatter, TextFormatter
from tests.unit.logs._helpers import as_json, make_record


@pytest.fixture
def root_state():
    root = logging.getLogger()
    pyl = logging.getLogger("pyrock")
    saved = (list(root.handlers), root.level, list(pyl.handlers), pyl.level, pyl.propagate)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    pyl.handlers[:] = saved[2]
    pyl.setLevel(saved[3])
    pyl.propagate = saved[4]


def test_root_install_and_restore(root_state: None) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    stream = io.StringIO()
    restore = logs._install(
        "root", level=logging.WARNING, stream=stream, service="svc", static_attrs={"env": "p"}
    )
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], BedrockHandler)
    logging.getLogger("x.y").info("dropped")
    logging.getLogger("x.y").warning("kept", extra={"k": 1})
    extra = logging.NullHandler()
    root.addHandler(extra)
    restore()
    restore()
    assert root.handlers == [*before, extra]
    rec = json.loads(stream.getvalue())
    assert (rec["msg"], rec["service"], rec["env"], rec["k"], rec["logger"]) == (
        "kept",
        "svc",
        "p",
        1,
        "x.y",
    )


def test_pyrock_install_text(root_state: None) -> None:
    pyl = logging.getLogger("pyrock")
    stream = io.StringIO()
    restore = logs._install("pyrock", level=logging.DEBUG, stream=stream, log_format="text")
    assert pyl.propagate is False
    pyl.debug("hi there")
    restore()
    assert pyl.propagate is True
    assert 'msg="hi there"' in stream.getvalue()


def test_none_and_unknown() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    logs._install("none", level=logging.INFO)()
    assert root.handlers == before
    with pytest.raises(ValueError, match="unknown log install target"):
        logs._install("bogus", level=logging.INFO)


@pytest.mark.parametrize("order", ["lifo", "fifo"])
def test_nested_installs_restore_in_any_order(root_state: None, order: str) -> None:
    root = logging.getLogger()
    orig_handlers, orig_level = list(root.handlers), root.level
    a = logs._install("root", level=logging.ERROR, stream=io.StringIO())
    b = logs._install("root", level=logging.DEBUG, stream=io.StringIO())
    first, second = (b, a) if order == "lifo" else (a, b)
    first()
    if order == "fifo":
        assert root.level == logging.DEBUG  # b still active
    second()
    assert root.handlers == orig_handlers
    assert root.level == orig_level


def test_custom_levels_and_edges() -> None:
    assert as_json(JSONFormatter(), make_record(level=25))["level"] == "INFO+5"
    assert as_json(JSONFormatter(), make_record(level=5))["level"] == "DEBUG-5"
    with pytest.raises(ValueError, match="level"):
        logs.parse_level(True)  # type: ignore[arg-type]
    rec = as_json(JSONFormatter(), make_record(extra={ATTRS_KEY: {"v": [float("nan")], "n": None}}))
    assert rec["n"] is None
    line = TextFormatter().format(
        make_record(extra={ATTRS_KEY: {"d": {"a": 1}, "n": None, "e": ""}})
    )
    assert 'd="{\\"a\\":1}"' in line
    assert "n=null" in line
    assert 'e=""' in line


def test_exception_value_and_handle_error() -> None:
    rec = as_json(
        JSONFormatter(), make_record(extra={ATTRS_KEY: {"err": KeyError("x"), "e2": ValueError()}})
    )
    assert rec["error.type"] == "builtins.KeyError"
    assert "err" not in rec

    class Broken(io.StringIO):
        def write(self, s: str) -> int:
            raise OSError("disk")

    handler = BedrockHandler(Broken())
    logging.raiseExceptions, prev = False, logging.raiseExceptions
    try:
        handler.handle(make_record())
    finally:
        logging.raiseExceptions = prev


def test_filter_statics_cached_and_not_shadowing() -> None:
    stream = io.StringIO()
    h = BedrockHandler(
        stream,
        static_attrs={"env": "p", "msg": "evil", "name": "x"},
        formatter=JSONFormatter(static_attrs={"env": "formatter"}),
    )
    log = logging.Logger("t")
    log.addHandler(h)
    log.info("a")
    log.info("b")
    a, b = map(json.loads, stream.getvalue().splitlines())
    assert a["msg"] == "a"
    assert a["env"] == "formatter"
    assert b["msg"] == "b"


def test_canonical_failure_with_unnormalizable_error_value() -> None:
    stream = io.StringIO()
    log = logging.Logger("pyrock")
    log.addHandler(
        BedrockHandler(
            stream, formatter=JSONFormatter(policy=privacy.Policy(allow_error_details=True))
        )
    )
    logs.log_canonical(
        log,
        operation="op",
        duration_ms=1,
        success=False,
        attributes={"error": True, "error.type": object(), "error.message": "m" * 5},
    )
    rec = json.loads(stream.getvalue())
    assert "error.type" not in rec
    assert rec["error.message"] == "mmmmm"


def test_threads_install(root_state: None) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    barrier = threading.Barrier(8)

    def work() -> None:
        barrier.wait()
        for _ in range(50):
            logs._install("root", level=logging.INFO, stream=io.StringIO())()

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert root.handlers == before
