"""Fork safety end to end (DESIGN §15): ``os.fork()`` after ``init()``.

The child runs a small scenario and reports observations through a pipe as
JSON; the parent checks what the collector received and its own state.
"""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

import pytest

from tests.e2e._export_helpers import prom, running
from tests.support.collectors import OTLPHTTPCollector

pytestmark = [
    pytest.mark.usefixtures("clean_env"),
    pytest.mark.fork,
    pytest.mark.linux_only,
    pytest.mark.skipif(
        not (hasattr(os, "fork") and sys.platform.startswith("linux")),
        reason="needs os.fork on Linux",
    ),
    # 3.12+ warns about forking a multi-threaded process; that is the scenario under test.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

CHILD_TIMEOUT = 15.0


def in_child(scenario: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Fork, run ``scenario`` in the child, and return what it reported."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never return into pytest
        os.close(read_fd)
        code = 0
        try:
            payload: dict[str, Any] = {"ok": True, **scenario()}
        except BaseException:
            payload = {"ok": False, "error": traceback.format_exc()}
            code = 1
        try:
            os.write(write_fd, json.dumps(payload).encode())
        finally:
            os._exit(code)
    os.close(write_fd)
    chunks: list[bytes] = []
    deadline = time.monotonic() + CHILD_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.kill(pid, signal.SIGKILL)
                raise AssertionError("forked child did not finish in time")
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)
    result: dict[str, Any] = json.loads(b"".join(chunks) or b"{}")
    assert result.get("ok"), result.get("error", "child reported nothing")
    return result


def _policy() -> Any:
    from pyrock import privacy

    return privacy.Policy(resource_keys=["process.pid"])


def test_child_spans_are_exported_with_child_pid(otlp_collector: OTLPHTTPCollector) -> None:
    with running(
        trace_url=otlp_collector.traces_url,
        disable_environment_detection=False,
        data_policy=_policy(),
    ) as b:

        def child() -> dict[str, Any]:
            with b.operation("in_child"):
                pass
            b.shutdown(timeout=5)
            return {"pid": os.getpid()}

        report = in_child(child)
        otlp_collector.wait_for_spans(1)
        span = otlp_collector.span("in_child")
    assert span["resource"]["process.pid"] == report["pid"]
    assert report["pid"] != os.getpid()


def test_parent_keeps_exporting_after_fork(otlp_collector: OTLPHTTPCollector) -> None:
    with running(
        trace_url=otlp_collector.traces_url,
        disable_environment_detection=False,
        data_policy=_policy(),
    ) as b:
        with b.operation("before_fork"):
            pass
        in_child(dict)
        with b.operation("after_fork"):
            pass
    names = sorted(otlp_collector.span_names())
    assert names == ["after_fork", "before_fork"]
    assert otlp_collector.span("after_fork")["resource"]["process.pid"] == os.getpid()


def test_child_batch_worker_restarts(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url) as b:
        with b.operation("warm_up"):
            pass  # starts the parent's batch worker thread

        def child() -> dict[str, Any]:
            for _ in range(3):
                with b.operation("child_work"):
                    pass
            b.shutdown(timeout=5)
            return {"exported": b.trace_export_stats().exported}

        report = in_child(child)
    assert report["exported"] == 3
    assert otlp_collector.span_names().count("child_work") == 3


def test_child_does_not_start_observability_server() -> None:
    with running(server_enabled=True, server_addr="127.0.0.1:0") as b:
        parent_address = b.server_address

        def child() -> dict[str, Any]:
            address = b.server_address
            return {"address": list(address) if address else None}

        report = in_child(child)
        assert b.server_address == parent_address
    assert parent_address is not None
    assert report["address"] is None


def test_child_metric_series_are_reset() -> None:
    with running() as b:
        c = b.counter("forked_total", "F")
        c.inc(5)

        def child() -> dict[str, Any]:
            before = prom(b).value("forked_total")
            c.inc()
            return {"before": before, "after": prom(b).value("forked_total")}

        report = in_child(child)
        parent_value = prom(b).value("forked_total")
    assert report["before"] in (None, 0)
    assert report["after"] == 1
    assert parent_value == 5


def test_parent_metrics_unaffected_by_child() -> None:
    with running() as b:
        with b.operation("shared_op"):
            pass

        def child() -> dict[str, Any]:
            for _ in range(10):
                with b.operation("shared_op"):
                    pass
            return {}

        in_child(child)
        assert prom(b).value("shared_op_count") == 1


def test_parent_shutdown_after_fork_is_clean(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url) as bedrock:
        in_child(dict)
        with bedrock.operation("final"):
            pass
    assert bedrock.closed
    assert otlp_collector.span_names() == ["final"]
