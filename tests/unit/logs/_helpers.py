"""Shared helpers for the ``pyrock.logs`` unit tests."""

from __future__ import annotations

import contextvars
import io
import json
import logging
import sys
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from pyrock.trace import Span, SpanContext, use_span

T = TypeVar("T")

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
FIXED_CREATED = 1793880000.123456
"""``2026-11-05T12:00:00.123456Z``."""
FIXED_TIME = "2026-11-05T12:00:00.123Z"


def span_context() -> SpanContext:
    return SpanContext.from_hex(TRACE_ID, SPAN_ID)


def in_span(fn: Callable[[], T]) -> T:
    """Run ``fn`` in a copied context where a live span with TRACE_ID/SPAN_ID is current."""

    def run() -> T:
        use_span(Span("unit", span_context()))
        return fn()

    return contextvars.copy_context().run(run)


def detached_logger(name: str = "tests.unit.logs", *handlers: logging.Handler) -> logging.Logger:
    """A logger outside the global hierarchy (no propagation side effects)."""
    logger = logging.Logger(name, logging.DEBUG)
    logger.propagate = False
    for handler in handlers:
        logger.addHandler(handler)
    return logger


def make_record(
    msg: str = "hello",
    *args: object,
    level: int = logging.INFO,
    name: str = "myapp.users",
    extra: Mapping[str, Any] | None = None,
    exc_info: Any = None,
    created: float | None = FIXED_CREATED,
) -> logging.LogRecord:
    logger = detached_logger(name)
    record = logger.makeRecord(
        name,
        level,
        "/srv/app/myapp/users.py",
        42,
        msg,
        args,
        exc_info,
        func="load_user",
        extra=dict(extra) if extra else None,
    )
    if created is not None:
        record.created = created
        record.msecs = (created - int(created)) * 1000
    return record


def exc_info_of(exc: BaseException) -> Any:
    try:
        raise exc
    except BaseException:
        return sys.exc_info()


def as_json(formatter: logging.Formatter, record: logging.LogRecord) -> dict[str, Any]:
    line = formatter.format(record)
    assert "\n" not in line, line
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


class Capture:
    """A ``StringIO`` stream plus JSON-lines parsing."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]

    def one(self) -> dict[str, Any]:
        (record,) = self.records()
        assert isinstance(record, dict)
        return record
