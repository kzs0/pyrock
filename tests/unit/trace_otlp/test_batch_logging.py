"""Review fixes: rate-limited export-failure warnings and fork-safe HTTP exporter state."""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence

import pytest

from pyrock.trace import Span, SpanContext
from pyrock.trace.otlp import ExportError
from pyrock.trace.otlp import batch as batch_module
from pyrock.trace.otlp.batch import BatchProcessor
from pyrock.trace.otlp.http import HTTPExporter

pytestmark = pytest.mark.unit


def _span(n: int) -> Span:
    span = Span("op", SpanContext(bytes(range(1, 17)), bytes([n]) * 8, 1))
    span.end()
    return span


class FailingExporter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        self.calls += 1
        raise self.error

    def shutdown(self, *, timeout: float | None = None) -> None:
        pass


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "pyrock" and r.levelno == logging.WARNING]


def _export_n(bp: BatchProcessor, n: int) -> None:
    for i in range(n):
        bp.on_end(_span(i + 1))
        assert bp.force_flush(timeout=5)


def test_first_failure_warns_then_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    exporter = FailingExporter(ExportError("OTLP/HTTP export rejected: HTTP 404 Not Found"))
    bp = BatchProcessor(exporter, schedule_delay=0.01)
    caplog.set_level(logging.WARNING, logger="pyrock")
    try:
        bp._export([_span(1)])
        bp._export([_span(2)])
        bp._export([_span(3)])
        assert len(_warnings(caplog)) == 1
        first = _warnings(caplog)[0].getMessage()
        assert "HTTP 404" in first
        assert "ExportError" in first
        assert bp._last_failure_log is not None
        bp._last_failure_log -= batch_module.FAILURE_LOG_INTERVAL + 1  # a minute passes
        bp._export([_span(4)])
        records = _warnings(caplog)
        assert len(records) == 2
        assert "3 failed export(s)" in records[1].getMessage()  # 2 suppressed + this one
    finally:
        bp.shutdown(timeout=5)
    assert bp.stats().failed == 4


def test_failure_warning_omits_messages_of_foreign_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = FailingExporter(RuntimeError("token=s3cr3t"))
    bp = BatchProcessor(exporter, schedule_delay=0.01)
    caplog.set_level(logging.WARNING, logger="pyrock")
    try:
        _export_n(bp, 1)
    finally:
        bp.shutdown(timeout=5)
    records = _warnings(caplog)
    assert len(records) == 1
    text = records[0].getMessage()
    assert "RuntimeError" in text
    assert "s3cr3t" not in text
    assert records[0].exc_info is None


def test_successful_exports_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    class Ok:
        def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
            pass

        def shutdown(self, *, timeout: float | None = None) -> None:
            pass

    bp = BatchProcessor(Ok(), schedule_delay=0.01)
    caplog.set_level(logging.WARNING, logger="pyrock")
    try:
        _export_n(bp, 2)
    finally:
        bp.shutdown(timeout=5)
    assert _warnings(caplog) == []


def test_http_exporter_errors_never_show_credentials() -> None:
    exporter = HTTPExporter("http://user:s3cr3t@127.0.0.1:9/v1/traces?k=s3cr3t", service="s")
    with pytest.raises(ExportError) as info:
        exporter.export([_span(1)], timeout=2)
    assert "s3cr3t" not in str(info.value)


def test_http_exporter_header_problems_are_all_listed() -> None:
    with pytest.raises(ValueError, match="bad name") as info:
        HTTPExporter("http://127.0.0.1:9", service="s", headers={"bad name": "x", "X-Ok": "a\r\nb"})
    assert "X-Ok" in str(info.value)
    assert "a\r\nb" not in str(info.value)


def test_http_exporter_fork_reset_replaces_lock() -> None:
    exporter = HTTPExporter("http://127.0.0.1:9", service="s")
    lock = exporter._lock
    lock.acquire()  # held "in the parent" at fork time
    try:
        exporter._after_fork_in_child()
        assert exporter._lock is not lock
        assert exporter._opener is None
        assert exporter._get_opener() is not None  # does not deadlock
    finally:
        lock.release()


def test_http_exporter_replace_resource() -> None:
    exporter = HTTPExporter("http://127.0.0.1:9", service="s", resource={"process.pid": 1})
    exporter._replace_resource({"process.pid": 2})
    assert exporter._resource == {"process.pid": 2}


def test_logging_failure_never_kills_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(batch_module._log, "warning", boom)
    exporter = FailingExporter(ExportError("nope"))
    bp = BatchProcessor(exporter, schedule_delay=0.01)
    try:
        _export_n(bp, 2)
        assert exporter.calls == 2
        assert bp._worker is not None
        assert bp._worker.is_alive()
    finally:
        bp.shutdown(timeout=5)
    assert isinstance(bp._work, threading.Condition)
