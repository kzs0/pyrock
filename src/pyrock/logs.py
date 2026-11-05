"""Structured, trace-correlated logging on top of stdlib :mod:`logging`.

* :class:`BedrockHandler`: a stream handler installed by ``init()`` (on the
  root logger by default, see ``Config.log_install``).
* :class:`JSONFormatter` / :class:`TextFormatter`: Go-compatible record shape::

    {"time":"2026-11-05T12:00:00.123Z","level":"INFO","msg":"...",
     "service":"users-api","env":"production","user_id":"123",
     "trace_id":"...","span_id":"...","logger":"myapp.users"}

  ``level`` uses Go's names: ``DEBUG``, ``INFO``, ``WARN`` (Python's
  ``WARNING``), ``ERROR``; ``CRITICAL`` stays ``CRITICAL``. Static attrs are
  top-level fields.
* :class:`CorrelationFilter`: adds ``trace_id``, ``span_id`` and static attrs
  to records for apps that own their logging config.
* :func:`log_canonical`: the ``operation.complete`` record.

Attributes passed through the pyrock helpers travel in ``record.__dict__``
under :data:`ATTRS_KEY`; other ``extra`` keys also become structured fields.
All fields pass through the privacy policy. ``exc_info`` becomes ``error.*``
fields.

The top-level ``pyrock.log(level, msg, ...)`` helper lives in ``pyrock`` itself.

Import rules: ``pyrock.logs`` imports only ``pyrock.attr``, ``pyrock.trace`` and
``pyrock.privacy`` from pyrock.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, TextIO

from pyrock.attr import AttrValue
from pyrock.privacy import Policy
from pyrock.trace import SpanContext

__all__ = [
    "ATTRS_KEY",
    "CANONICAL_MESSAGE",
    "LOGGER_NAME",
    "BedrockHandler",
    "CorrelationFilter",
    "JSONFormatter",
    "TextFormatter",
    "log_canonical",
    "parse_level",
]

LOGGER_NAME = "pyrock"
"""The logger used by the direct helpers (``pyrock.info`` etc.)."""

ATTRS_KEY = "pyrock_attrs"
"""``LogRecord`` attribute holding a ``Mapping[str, AttrValue]`` of structured fields."""

CANONICAL_MESSAGE = "operation.complete"


def parse_level(level: str | int, /) -> int:
    """Map ``debug``/``info``/``warn``/``warning``/``error`` (any case) or an int to a level.

    Raises ``ValueError`` for unknown names.
    """
    raise NotImplementedError


class JSONFormatter(logging.Formatter):
    """Formats records as one JSON object per line (keys identical to Go)."""

    def __init__(
        self,
        *,
        service: str | None = None,
        static_attrs: Mapping[str, AttrValue] | None = None,
        policy: Policy | None = None,
        add_source: bool = False,
    ) -> None:
        raise NotImplementedError

    def format(self, record: logging.LogRecord) -> str:
        raise NotImplementedError


class TextFormatter(logging.Formatter):
    """Human-readable ``key=value`` lines with the same fields as :class:`JSONFormatter`."""

    def __init__(
        self,
        *,
        service: str | None = None,
        static_attrs: Mapping[str, AttrValue] | None = None,
        policy: Policy | None = None,
        add_source: bool = False,
    ) -> None:
        raise NotImplementedError

    def format(self, record: logging.LogRecord) -> str:
        raise NotImplementedError


class CorrelationFilter(logging.Filter):
    """Adds ``trace_id``/``span_id`` (from the current span) and static attrs to records.

    Always returns ``True``; it never drops records.
    """

    def __init__(self, *, static_attrs: Mapping[str, AttrValue] | None = None) -> None:
        raise NotImplementedError

    def filter(self, record: logging.LogRecord) -> bool:
        raise NotImplementedError


class BedrockHandler(logging.StreamHandler[TextIO]):
    """A stream handler (default ``sys.stderr``) with a pyrock formatter and correlation."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        level: int = logging.NOTSET,
        formatter: logging.Formatter | None = None,
        static_attrs: Mapping[str, AttrValue] | None = None,
    ) -> None:
        raise NotImplementedError

    def emit(self, record: logging.LogRecord) -> None:
        raise NotImplementedError


def log_canonical(
    logger: logging.Logger,
    /,
    *,
    operation: str,
    duration_ms: float,
    success: bool,
    attributes: Mapping[str, Any],
    span_context: SpanContext | None = None,
) -> None:
    """Emit the ``operation.complete`` INFO record (same fields as Go's canonical log).

    Fields: ``operation``, ``duration_ms``, ``success``, ``attributes``,
    ``trace_id``, ``span_id``. When ``success`` is ``False`` the record also
    carries top-level ``error``, ``error.type`` and ``error.fingerprint``
    (taken from ``attributes``); ``error.message`` is added only when the
    privacy policy has ``allow_error_details``.
    """
    raise NotImplementedError
