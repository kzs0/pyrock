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
All fields pass through the privacy policy. ``exc_info`` (or an exception
passed as a field value) becomes ``error.*`` fields built by
:func:`pyrock.attr.error` with the policy's flags; ``stack_info`` is never
written.

The top-level ``pyrock.log(level, msg, ...)`` helper lives in ``pyrock`` itself.

Import rules: ``pyrock.logs`` imports only ``pyrock.attr``, ``pyrock.trace``,
``pyrock.privacy`` and the ``pyrock._compat`` utilities from pyrock.
"""

from __future__ import annotations

import abc
import datetime
import functools
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, TextIO

from pyrock._compat import LOG_LEVEL_CHOICES, log_level_number
from pyrock.attr import (
    ERROR_FINGERPRINT,
    ERROR_KEY,
    ERROR_MESSAGE,
    ERROR_TYPE,
    AttrValue,
    _normalize_value,
    _text_value,
    error,
)
from pyrock.privacy import REDACTED_MARKER, Policy
from pyrock.trace import SpanContext, current_span_context

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

_STATIC_KEY: Final = "pyrock_static"
"""Record attribute where :class:`CorrelationFilter` leaves its static attrs."""

_CANONICAL_KEY: Final = "pyrock_canonical"
"""Record attribute carrying the :class:`_Canonical` fields."""

_TRACE_ID: Final = "trace_id"
_SPAN_ID: Final = "span_id"


_STANDARD_RECORD_KEYS: Final = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}
"""``LogRecord`` attributes that are never structured fields."""

_INTERNAL_KEYS: Final = frozenset({ATTRS_KEY, _STATIC_KEY, _CANONICAL_KEY, _TRACE_ID, _SPAN_ID})

_RESERVED: Final = frozenset(
    {"time", "level", "source", "msg", "service", _TRACE_ID, _SPAN_ID, "logger"}
)
"""Keys only the formatter writes; record fields never override them."""

_ERROR_NAMESPACES: Final = ("error", "exception")


# -- levels -----------------------------------------------------------------

_GO_LEVELS: Final = (
    (logging.CRITICAL, "CRITICAL"),
    (logging.ERROR, "ERROR"),
    (logging.WARNING, "WARN"),
    (logging.INFO, "INFO"),
    (logging.DEBUG, "DEBUG"),
)


def parse_level(level: str | int, /) -> int:
    """Map a level name (``pyrock._compat.LOG_LEVELS``, any case) or an int to a level.

    The names are ``debug``, ``info``, ``warn``, ``warning``, ``error`` and
    ``critical``, the same set ``Config.log_level`` accepts. Raises
    ``ValueError`` for anything else (``bool`` included).
    """
    number = log_level_number(level)
    if number is None:
        raise ValueError(f"unknown log level {level!r}: expected one of {LOG_LEVEL_CHOICES}")
    return number


@functools.lru_cache(maxsize=64)
def _level_name(levelno: int) -> str:
    """Go's level text: ``WARN`` for warnings, offsets such as ``INFO+5`` in between."""
    for number, name in _GO_LEVELS:
        if levelno >= number:
            return name if levelno == number else f"{name}+{levelno - number}"
    return f"DEBUG-{logging.DEBUG - levelno}"


# -- time -------------------------------------------------------------------

_second_cache: tuple[int, str] = (-1, "")
"""``(epoch second, "YYYY-MM-DDTHH:MM:SS")``; replaced atomically, so lock-free."""


def _format_time(record: logging.LogRecord) -> str:
    """RFC 3339 UTC with milliseconds (``2026-11-05T12:00:00.123Z``)."""
    global _second_cache
    second = int(record.created)
    cached = _second_cache
    if cached[0] == second:
        prefix = cached[1]
    else:
        prefix = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(second))
        _second_cache = (second, prefix)
    return f"{prefix}.{int(record.msecs):03d}Z"


# -- value encoding -----------------------------------------------------------


def _json_default(value: object) -> object:
    """Encode the non-JSON scalars the policy keeps; anything else becomes ``null``."""
    if isinstance(value, bytes | datetime.datetime | datetime.timedelta):
        return _text_value(value)
    return None


@functools.cache
def _encoder() -> Callable[[Any], str]:
    """The compact JSON encoder, built on first use (keeps :mod:`json` out of ``import pyrock``)."""
    import json

    return json.JSONEncoder(
        ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_json_default
    ).encode


def _finite(value: Any) -> Any:
    """Replace non-finite floats (invalid JSON) with ``"NaN"``/``"+Inf"``/``"-Inf"``."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("+Inf" if value > 0 else "-Inf")
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def _dumps(value: Any) -> str:
    """Compact JSON; the slower NaN-safe pass runs only when needed."""
    try:
        return _encoder()(value)
    except ValueError:
        return _encoder()(_finite(value))


def _is_error_namespace(key: str) -> bool:
    return key.split(".", 1)[0].lower() in _ERROR_NAMESPACES


def _exc_info_error(record: logging.LogRecord) -> BaseException | None:
    info = record.exc_info
    if info and isinstance(info[1], BaseException):
        return info[1]
    return None


# -- canonical record -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Canonical:
    """The structural fields of an ``operation.complete`` record."""

    operation: str
    duration_ms: float
    success: bool
    attributes: dict[str, Any]
    failure: dict[str, Any]


_CANONICAL_ERROR_KEYS: Final = (ERROR_KEY, ERROR_TYPE, ERROR_FINGERPRINT)


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
    if not logger.isEnabledFor(logging.INFO):
        return
    failure: dict[str, Any] = {}
    if not success:
        for key in (*_CANONICAL_ERROR_KEYS, ERROR_MESSAGE):
            if key in attributes:
                failure[key] = attributes[key]
    extra: dict[str, Any] = {
        _CANONICAL_KEY: _Canonical(operation, duration_ms, success, dict(attributes), failure)
    }
    if span_context is not None:
        extra[_TRACE_ID] = span_context.trace_id_hex
        extra[_SPAN_ID] = span_context.span_id_hex
    logger.info(CANONICAL_MESSAGE, extra=extra, stacklevel=2)


# -- formatters -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Statics:
    """A rendered static-attr fragment and the keys it claims (reserved ones included)."""

    source: object
    text: str
    keys: frozenset[str]


class _StructuredFormatter(logging.Formatter, abc.ABC):
    """Builds the ordered, policy-cleaned fields; subclasses only encode them.

    A line is: ``time, level, [source], msg``, then ``service`` and static
    attrs (rendered once), then canonical fields, record fields, error fields,
    ``trace_id``, ``span_id`` and ``logger``.
    """

    _separator: str

    def __init__(
        self,
        *,
        service: str | None = None,
        static_attrs: Mapping[str, AttrValue] | None = None,
        policy: Policy | None = None,
        add_source: bool = False,
    ) -> None:
        super().__init__()
        self._policy = policy if policy is not None else Policy()
        self._add_source = add_source
        self._error_flags = {
            "allow_details": self._policy.allow_error_details,
            "allow_source_paths": self._policy.allow_source_paths,
            "stack": self._policy.allow_error_details and self._policy.allow_source_paths,
        }
        head: dict[str, Any] = {} if service is None else {"service": self._policy.message(service)}
        self._statics = self._render_statics(head, static_attrs or {}, _RESERVED)
        self._record_statics: _Statics | None = None

    # -- encoding hooks ----------------------------------------------------

    @abc.abstractmethod
    def _join(self, fields: Mapping[str, Any], /) -> str:
        """Encode ``fields`` without any enclosing brackets (``""`` when empty)."""

    def _wrap(self, body: str, /) -> str:
        """Enclose a joined line (JSON adds braces)."""
        return body

    # -- record → fields -----------------------------------------------------

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as one line; never calls ``str()`` on field values."""
        record_statics = self._statics_of(record)
        parts = [self._join(self._head(record)), self._statics.text, record_statics.text]
        parts.append(self._join(self._tail(record, record_statics.keys or self._statics.keys)))
        return self._wrap(self._separator.join(part for part in parts if part))

    def _head(self, record: logging.LogRecord) -> dict[str, Any]:
        head: dict[str, Any] = {"time": _format_time(record), "level": _level_name(record.levelno)}
        if self._add_source:
            head["source"] = self._source(record)
        head["msg"] = self._policy.message(record.getMessage())
        return head

    def _source(self, record: logging.LogRecord) -> str:
        where = record.pathname if self._policy.allow_source_paths else record.module
        return f"{where}:{record.funcName or '?'}:{record.lineno}"

    def _tail(self, record: logging.LogRecord, claimed: frozenset[str]) -> dict[str, Any]:
        """Canonical, record and error fields, then the IDs and ``logger``.

        ``claimed`` holds the reserved and static keys, which record fields
        cannot override.
        """
        canonical = record.__dict__.get(_CANONICAL_KEY)
        tail = self._canonical(canonical) if isinstance(canonical, _Canonical) else {}
        for key, value in self._fields(record).items():
            if key not in claimed and key not in tail:
                tail[key] = value
        for key in (_TRACE_ID, _SPAN_ID):
            value = record.__dict__.get(key)
            if isinstance(value, str) and value:
                tail[key] = value
        tail["logger"] = record.name
        return tail

    def _fields(self, record: logging.LogRecord) -> dict[str, Any]:
        """``extra`` and :data:`ATTRS_KEY` fields after the policy, plus ``error.*``."""
        raw: dict[str, Any] = {}
        exc = _exc_info_error(record)
        for key, value in record.__dict__.items():
            if key == ATTRS_KEY:
                if isinstance(value, Mapping):
                    for name, item in value.items():
                        exc = _collect(raw, name, item, exc)
            elif key not in _STANDARD_RECORD_KEYS and key not in _INTERNAL_KEYS:
                exc = _collect(raw, key, value, exc)
        fields = self._clean(raw)
        if exc is not None:
            fields.update(self._error(exc))
        return fields

    def _clean(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Apply the policy; private ``error.*``/``exception.*`` fields are omitted."""
        if not raw:
            return {}
        return {
            key: value
            for key, value in self._policy.apply(raw).items()
            if not (value == REDACTED_MARKER and _is_error_namespace(key))
        }

    def _error(self, exc: BaseException) -> dict[str, Any]:
        fields: dict[str, Any] = dict(error(exc, **self._error_flags))
        message = fields.get(ERROR_MESSAGE)
        if isinstance(message, str):
            fields[ERROR_MESSAGE] = self._policy.message(message)
        return fields

    def _canonical(self, canonical: _Canonical) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "operation": self._policy.message(canonical.operation),
            "duration_ms": canonical.duration_ms,
            "success": canonical.success,
        }
        for key, value in canonical.failure.items():
            if key == ERROR_MESSAGE and not self._policy.allow_error_details:
                continue
            normalized = _normalize_value(value)
            if isinstance(normalized, str):
                normalized = self._policy.message(normalized)
            if normalized is not None:
                fields[key] = normalized
        fields["attributes"] = self._clean(canonical.attributes)
        return fields

    # -- static attrs ----------------------------------------------------------

    def _render_statics(
        self, head: Mapping[str, Any], attrs: Mapping[str, Any], taken: frozenset[str]
    ) -> _Statics:
        """Clean static attrs as resource attributes and render them once."""
        fields = dict(head)
        for key, value in self._policy.resource(attrs).items():
            if key not in taken and key not in fields:
                fields[key] = value
        return _Statics(attrs, self._join(fields), frozenset(fields).union(taken))

    def _statics_of(self, record: logging.LogRecord) -> _Statics:
        """The static attrs a :class:`CorrelationFilter` put on ``record`` (cached by identity)."""
        source = record.__dict__.get(_STATIC_KEY)
        if not isinstance(source, Mapping) or not source:
            return _NO_STATICS
        cached = self._record_statics
        if cached is not None and cached.source is source:
            return cached
        rendered = self._render_statics({}, source, self._statics.keys)
        self._record_statics = rendered
        return rendered


_NO_STATICS: Final = _Statics(None, "", frozenset())


def _collect(
    raw: dict[str, Any], key: Any, value: Any, exc: BaseException | None
) -> BaseException | None:
    """Add one field to ``raw``; an exception value is kept aside for ``error.*``."""
    if isinstance(value, BaseException):
        return exc if exc is not None else value
    raw[key] = value
    return exc


class JSONFormatter(_StructuredFormatter):
    """Formats records as one JSON object per line (keys identical to Go)."""

    _separator = ","

    def _join(self, fields: Mapping[str, Any], /) -> str:
        return _dumps(fields)[1:-1]

    def _wrap(self, body: str, /) -> str:
        return "{" + body + "}"


def _needs_quoting(text: str) -> bool:
    """Go slog's rule: empty, or containing a space, ``=``, ``"`` or a non-printable."""
    return not text or not text.isprintable() or any(c in ' ="' for c in text)


def _text(value: Any) -> str:
    """A field value as Go slog text (containers and ``None`` as compact JSON)."""
    if isinstance(value, str):
        text = value
    elif value is None or isinstance(value, dict | list):
        text = _dumps(value)
    else:
        text = _text_value(value)
    return _dumps(text) if _needs_quoting(text) else text


class TextFormatter(_StructuredFormatter):
    """Human-readable ``key=value`` lines with the same fields as :class:`JSONFormatter`."""

    _separator = " "

    def _join(self, fields: Mapping[str, Any], /) -> str:
        return " ".join(f"{_text(key)}={_text(value)}" for key, value in fields.items())


# -- correlation and handler ------------------------------------------------------


class CorrelationFilter(logging.Filter):
    """Adds ``trace_id``/``span_id`` (from the current span) and static attrs to records.

    Always returns ``True``; it never drops records. IDs already on a record
    (for example a canonical record's) are kept; outside a span they are
    ``""``. Static attrs become record attributes (usable as ``%(env)s``)
    unless they would shadow a standard ``LogRecord`` attribute.
    """

    def __init__(self, *, static_attrs: Mapping[str, AttrValue] | None = None) -> None:
        super().__init__()
        self._static: dict[str, AttrValue] = dict(static_attrs or {})

    def filter(self, record: logging.LogRecord) -> bool:
        """Correlate ``record`` with the active span; never drops it."""
        values = record.__dict__
        if _TRACE_ID not in values or _SPAN_ID not in values:
            context = current_span_context()
            values.setdefault(_TRACE_ID, "" if context is None else context.trace_id_hex)
            values.setdefault(_SPAN_ID, "" if context is None else context.span_id_hex)
        if self._static:
            values[_STATIC_KEY] = self._static
            for key, value in self._static.items():
                if key not in _STANDARD_RECORD_KEYS:
                    values.setdefault(key, value)
        return True


class BedrockHandler(logging.StreamHandler[TextIO]):
    """A stream handler (default ``sys.stderr``) with a pyrock formatter and correlation.

    Records are formatted outside the handler lock; only the write is
    serialized, so concurrent threads never interleave lines.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        level: int = logging.NOTSET,
        formatter: logging.Formatter | None = None,
        static_attrs: Mapping[str, AttrValue] | None = None,
    ) -> None:
        super().__init__(stream)
        self.setLevel(level)
        self.setFormatter(formatter if formatter is not None else JSONFormatter())
        self.addFilter(CorrelationFilter(static_attrs=static_attrs))

    def handle(self, record: logging.LogRecord) -> Any:
        """Filter and emit ``record``; :meth:`emit` takes the lock for the write only."""
        result: object = self.filter(record)  # a LogRecord on 3.12+
        if isinstance(result, logging.LogRecord):
            record = result
        if result:
            self.emit(record)
        return result

    def emit(self, record: logging.LogRecord) -> None:
        """Format ``record`` and write it as one line."""
        try:
            line = self.format(record) + self.terminator
            with self.lock:  # type: ignore[union-attr]  # always created by Handler.__init__
                self.stream.write(line)
                self.stream.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


# -- installation -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LoggerState:
    """A logger's handlers, level and propagation, as saved by an install."""

    handlers: tuple[logging.Handler, ...]
    level: int
    propagate: bool


class _Installation:
    """One install on one logger; ``restore`` undoes it, in any order relative to others."""

    __slots__ = ("_handler", "_logger", "_saved")

    def __init__(self, logger: logging.Logger, handler: logging.Handler) -> None:
        self._logger = logger
        self._handler = handler
        self._saved = _LoggerState(tuple(logger.handlers), logger.level, logger.propagate)

    def _activate(self, level: int) -> None:
        """Make the handler the logger's only one (call with ``_install_lock`` held)."""
        self._logger.handlers = [self._handler]
        self._logger.setLevel(level)
        if self._logger is not logging.root:
            self._logger.propagate = False
        _installed.setdefault(self._logger, []).append(self)

    def restore(self) -> None:
        """Put back what this install replaced and close its handler; idempotent."""
        with _install_lock:
            stack = _installed.get(self._logger, [])
            if self not in stack:
                return
            index = stack.index(self)
            del stack[index]
            if index < len(stack):
                stack[index]._inherit(self)
            else:
                self._apply()
            if not stack:
                del _installed[self._logger]
        self._handler.close()

    def _inherit(self, below: _Installation) -> None:
        """Take over the state a lower install saved, as if it had never happened."""
        handlers = tuple(
            restored
            for handler in self._saved.handlers
            for restored in (below._saved.handlers if handler is below._handler else (handler,))
        )
        self._saved = _LoggerState(handlers, below._saved.level, below._saved.propagate)

    def _apply(self) -> None:
        """Restore the saved state, keeping handlers others added since the install."""
        saved = self._saved
        added = [
            h for h in self._logger.handlers if h is not self._handler and h not in saved.handlers
        ]
        self._logger.handlers = [*saved.handlers, *added]
        self._logger.setLevel(saved.level)
        self._logger.propagate = saved.propagate


_install_lock = threading.Lock()
_installed: dict[logging.Logger, list[_Installation]] = {}
"""Live installs per logger, oldest first (guarded by ``_install_lock``)."""


def _install_lock_after_fork_in_child() -> None:
    """Replace the lock in a forked child: a holder thread at fork time no longer exists."""
    global _install_lock
    _install_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    # Held across fork() so the child never inherits a half-updated ``_installed``.
    os.register_at_fork(
        before=lambda: _install_lock.acquire(),
        after_in_parent=lambda: _install_lock.release(),
        after_in_child=_install_lock_after_fork_in_child,
    )


def _nothing() -> None:
    """The restore callback of ``log_install="none"``."""


def _install(
    target: str,
    *,
    level: int,
    log_format: str = "json",
    stream: TextIO | None = None,
    service: str | None = None,
    static_attrs: Mapping[str, AttrValue] | None = None,
    policy: Policy | None = None,
    add_source: bool = False,
) -> Callable[[], None]:
    """Install a :class:`BedrockHandler` per ``Config.log_install``; return its undo.

    ``"root"`` replaces the root logger's handlers and level; ``"pyrock"``
    does the same for the :data:`LOGGER_NAME` logger and stops it
    propagating; ``"none"`` installs nothing. The returned callable restores
    the previous handlers, level and propagation (idempotent, safe in any
    order with other installs) and closes the handler, but not its stream.
    """
    if target == "none":
        return _nothing
    if target not in ("root", "pyrock"):
        raise ValueError(f"unknown log install target {target!r}")
    formatter_type = TextFormatter if log_format == "text" else JSONFormatter
    formatter = formatter_type(
        service=service, static_attrs=static_attrs, policy=policy, add_source=add_source
    )
    handler = BedrockHandler(stream, formatter=formatter)
    logger = logging.getLogger() if target == "root" else logging.getLogger(LOGGER_NAME)
    with _install_lock:
        installation = _Installation(logger, handler)
        installation._activate(level)
    return installation.restore
