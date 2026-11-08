"""Cloud components in isolation: endpoint normalization, config validation,
retry and fan-out exporters, and the metrics pusher (DESIGN §16).

Fake exporters record calls; waits use events and the push sink, never sleeps.
"""

from __future__ import annotations

import datetime
import threading
import time
from collections.abc import Sequence
from typing import Any

import pytest

from pyrock._cloud import (
    CloudConfig,
    FanOutExporter,
    RetryExporter,
    cloud,
    grpc_endpoint,
)
from pyrock._push import MetricsPusher, PushStats
from pyrock.metric import Registry
from pyrock.trace import Span
from pyrock.trace.otlp import ExportError, PermanentExportError
from tests.support.collectors import MetricsPushSink

KEY = "brk_unit_key"


class FakeExporter:
    """Raises the scripted errors in order, then succeeds; records every call."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls: list[float | None] = []
        self.shutdowns = 0
        self.forks = 0

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        self.calls.append(timeout)
        if self.errors:
            raise self.errors.pop(0)

    def shutdown(self, *, timeout: float | None = None) -> None:
        self.shutdowns += 1

    def _after_fork_in_child(self) -> None:
        self.forks += 1


SPANS: list[Any] = [object()]


# ------------------------------------------------------------------ grpc_endpoint


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://ingest.bedrock.dev", "ingest.bedrock.dev:4317"),
        ("HTTP://collector:4317/v1/traces", "collector:4317"),
        ("http://collector?x=1", "collector:4317"),
        ("collector:9000", "collector:9000"),
        ("collector", "collector:4317"),
        ("[::1]", "[::1]:4317"),
        ("http://[::1]:55680/", "[::1]:55680"),
    ],
)
def test_grpc_endpoint_normalization(given: str, expected: str) -> None:
    assert grpc_endpoint(given) == expected


# ------------------------------------------------------------------ CloudConfig


def test_cloud_accepts_timedeltas_and_lists() -> None:
    cfg = cloud(
        KEY,
        push_interval=datetime.timedelta(seconds=5),
        retry_backoff=[datetime.timedelta(milliseconds=500), 1],
        profiles=["heap", "heap"],
    )
    assert cfg.push_interval == 5.0
    assert cfg.retry_backoff == (0.5, 1)
    assert [p.value for p in cfg.profiles] == ["heap"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"push_interval": 0}, "push_interval"),
        ({"push_interval": float("inf")}, "push_interval"),
        ({"profile_interval": -1}, "profile_interval"),
        ({"retry_backoff": "1"}, "retry_backoff"),
        ({"retry_backoff": (float("nan"),)}, "retry_backoff"),
        ({"retry_backoff": (True,)}, "retry_backoff"),
        ({"profiles": "cpu"}, "profiles"),
        ({"profiles": ("nope",)}, "profile type"),
        ({"endpoint": ""}, "endpoint"),
        ({"traces_endpoint": ""}, "traces_endpoint"),
        ({"api_key": 42}, "api_key"),
    ],
)
def test_cloud_config_validation(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CloudConfig(**{"api_key": KEY, **kwargs})


# ------------------------------------------------------------------ RetryExporter


def test_retry_succeeds_after_transient_errors() -> None:
    inner = FakeExporter(ExportError("a"), ExportError("b"))
    RetryExporter(inner, backoff=(0, 0, 0)).export(SPANS)
    assert len(inner.calls) == 3


def test_retry_raises_last_error_when_exhausted() -> None:
    inner = FakeExporter(*(ExportError(str(i)) for i in range(5)))
    with pytest.raises(ExportError, match="2"):
        RetryExporter(inner, backoff=(0, 0)).export(SPANS)
    assert len(inner.calls) == 3


def test_permanent_errors_are_not_retried() -> None:
    inner = FakeExporter(PermanentExportError("bad request"))
    with pytest.raises(PermanentExportError):
        RetryExporter(inner, backoff=(0, 0, 0)).export(SPANS)
    assert len(inner.calls) == 1


def test_other_exceptions_are_not_retried() -> None:
    inner = FakeExporter(RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        RetryExporter(inner, backoff=(0,)).export(SPANS)
    assert len(inner.calls) == 1


def test_retry_is_not_attempted_past_the_export_timeout() -> None:
    inner = FakeExporter(ExportError("a"), ExportError("b"))
    started = time.monotonic()
    with pytest.raises(ExportError):
        RetryExporter(inner, backoff=(30.0,)).export(SPANS, timeout=0.5)
    assert time.monotonic() - started < 5
    assert len(inner.calls) == 1
    assert inner.calls[0] is not None
    assert inner.calls[0] <= 0.5


def test_shutdown_interrupts_backoff_and_forwards() -> None:
    inner = FakeExporter(ExportError("a"), ExportError("b"))
    retry = RetryExporter(inner, backoff=(30.0,))
    failed = threading.Event()

    def run() -> None:
        try:
            retry.export(SPANS)
        except ExportError:
            failed.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    retry.shutdown(timeout=1)
    worker.join(5)
    assert failed.is_set()
    assert inner.shutdowns == 1


def test_retry_forwards_fork_hook_and_keeps_stop_state() -> None:
    inner = FakeExporter(ExportError("a"), ExportError("b"))
    retry = RetryExporter(inner, backoff=(30.0,))
    retry.shutdown()
    retry._after_fork_in_child()
    assert inner.forks == 1
    with pytest.raises(ExportError):  # still stopped: no 30 s back-off
        retry.export(SPANS)


# ------------------------------------------------------------------ FanOutExporter


def test_fan_out_calls_all_and_raises_first_error() -> None:
    first, second, third = (
        FakeExporter(ExportError("one")),
        FakeExporter(),
        FakeExporter(ExportError("three")),
    )
    fan = FanOutExporter(first, second, third)
    with pytest.raises(ExportError, match="one"):
        fan.export(SPANS, timeout=5)
    assert [len(e.calls) for e in (first, second, third)] == [1, 1, 1]
    fan.export(SPANS)  # all succeed now


def test_fan_out_shutdown_and_fork_reach_every_exporter() -> None:
    class Failing(FakeExporter):
        def shutdown(self, *, timeout: float | None = None) -> None:
            super().shutdown(timeout=timeout)
            raise ExportError("close failed")

    exporters = (Failing(), FakeExporter())
    fan = FanOutExporter(*exporters)
    with pytest.raises(ExportError, match="close failed"):
        fan.shutdown(timeout=1)
    fan._after_fork_in_child()
    assert [e.shutdowns for e in exporters] == [1, 1]
    assert [e.forks for e in exporters] == [1, 1]


# ------------------------------------------------------------------ MetricsPusher


def _pusher(sink: MetricsPushSink, **kw: Any) -> MetricsPusher:
    registry = Registry("")
    registry.counter("unit_total", "Unit").labels({}).inc()
    options: dict[str, Any] = {"endpoint": f"{sink.endpoint}/v1/metrics", "interval": 60.0}
    options.update(kw)
    return MetricsPusher(registry, **options)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"endpoint": "ftp://host/x"},
        {"endpoint": "not a url"},
        {"endpoint": "http://h/v1/metrics", "interval": 0},
        {"endpoint": "http://h/v1/metrics", "timeout": float("nan")},
    ],
)
def test_pusher_rejects_bad_settings(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="metrics push"):
        MetricsPusher(Registry(""), **kwargs)


def test_push_once_counts_outcomes(push_sink: MetricsPushSink) -> None:
    pusher = _pusher(push_sink, headers={"X-Extra": "1"})
    push_sink.fail_next(1, status=500, path="/v1/metrics")
    assert pusher.push_once() is False
    assert pusher.push_once() is True
    assert pusher.stats() == PushStats(pushed=1, failed=1)
    assert push_sink.pushes()[-1].header("x-extra") == "1"


def test_push_once_fails_on_connection_error() -> None:
    pusher = MetricsPusher(Registry(""), endpoint="http://127.0.0.1:9/v1/metrics", timeout=2)
    assert pusher.push_once() is False
    assert pusher.stats().failed == 1


def test_redirects_are_not_followed(push_sink: MetricsPushSink) -> None:
    push_sink.fail_next(1, status=302, headers=[("Location", "http://127.0.0.1:9/")])
    pusher = _pusher(push_sink)
    assert pusher.push_once() is False
    assert len(push_sink.requests) == 1


def test_push_once_is_skipped_while_a_push_is_in_flight(push_sink: MetricsPushSink) -> None:
    push_sink.hang()
    pusher = _pusher(push_sink)
    worker = threading.Thread(target=pusher.push_once, daemon=True)
    worker.start()
    try:
        push_sink.wait_for_pushes(1)
        assert pusher.push_once() is False
        assert pusher.stats().skipped == 1
    finally:
        push_sink.release()
        worker.join(5)


def test_stop_without_start_still_flushes(push_sink: MetricsPushSink) -> None:
    pusher = _pusher(push_sink)
    pusher.stop(timeout=5)
    assert len(push_sink.pushes()) == 1
    with pytest.raises(RuntimeError):
        pusher.start()


def test_stop_without_final_flush_sends_nothing(push_sink: MetricsPushSink) -> None:
    for start in (False, True):
        pusher = _pusher(push_sink)
        if start:
            pusher.start()
            pusher.start()  # idempotent
        pusher.stop(timeout=5, final_flush=False)
    assert push_sink.pushes() == []


def test_concurrent_stops_share_one_final_push(push_sink: MetricsPushSink) -> None:
    pusher = _pusher(push_sink)
    pusher.start()
    stoppers = [threading.Thread(target=pusher.stop, kwargs={"timeout": 5}) for _ in range(4)]
    for t in stoppers:
        t.start()
    for t in stoppers:
        t.join(10)
    pusher.stop(timeout=5)
    assert len(push_sink.pushes()) == 1


def test_overrun_ticks_are_skipped_and_counted(push_sink: MetricsPushSink) -> None:
    push_sink.hang()
    pusher = _pusher(push_sink, interval=0.02)
    pusher.start()
    try:
        push_sink.wait_for_pushes(1)
        threading.Event().wait(0.2)  # ~10 ticks pass while the push hangs
    finally:
        push_sink.release()
    pusher.stop(timeout=5)
    assert pusher.stats().skipped >= 1


def test_worker_survives_unexpected_errors(push_sink: MetricsPushSink) -> None:
    pusher = _pusher(push_sink, interval=0.02)
    calls = 0
    original = pusher._send

    def flaky(timeout: float) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return original(timeout)

    pusher._send = flaky  # type: ignore[method-assign]
    pusher.start()
    push_sink.wait_for_pushes(1)
    pusher.stop(timeout=5)
    assert pusher.stats().failed >= 1


def test_after_fork_resets_and_restarts(push_sink: MetricsPushSink) -> None:
    pusher = _pusher(push_sink, interval=0.05)
    pusher.start()
    push_sink.wait_for_pushes(1)
    pusher._after_fork_in_child()  # simulate the child side in-process
    assert pusher.stats() == PushStats()
    before = len(push_sink.pushes())
    push_sink.wait_for_pushes(before + 1)
    pusher.stop(timeout=5)
    stopped = _pusher(push_sink)
    stopped.stop(timeout=5, final_flush=False)
    stopped._after_fork_in_child()
    assert stopped._thread is None
