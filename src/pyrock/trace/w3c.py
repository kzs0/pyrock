"""W3C Trace Context parsing and formatting (protocol-agnostic).

``traceparent``: ``00-{trace-id}-{parent-id}-{flags}``; lowercase hex only;
trace and parent IDs must be non-zero; version ``ff`` is invalid; future
versions are accepted when the first four fields are valid.

``tracestate``: ``key1=value1,key2=value2`` with multi-tenant
``{tenant}@{system}`` keys, at most :data:`MAX_TRACESTATE_ENTRIES` entries.
Optional whitespace around list members is ignored; empty members are skipped.
"""

from __future__ import annotations

from collections.abc import Iterable

from pyrock.trace import SpanContext, TraceState

__all__ = [
    "MAX_TRACESTATE_ENTRIES",
    "SAMPLED_FLAG",
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
    "InvalidTraceparent",
    "InvalidTracestate",
    "format_traceparent",
    "format_tracestate",
    "is_valid_tracestate_key",
    "is_valid_tracestate_value",
    "parse_traceparent",
    "parse_tracestate",
    "span_context_from_traceparent",
]

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"
MAX_TRACESTATE_ENTRIES = 32
SAMPLED_FLAG = 0x01


class InvalidTraceparent(ValueError):
    """The ``traceparent`` value is malformed."""


class InvalidTracestate(ValueError):
    """The ``tracestate`` value is malformed or has too many entries."""


def parse_traceparent(value: str, /) -> tuple[bytes, bytes, int]:
    """Parse ``traceparent`` into ``(trace_id, span_id, flags)``.

    Raises :class:`InvalidTraceparent`.
    """
    raise NotImplementedError


def format_traceparent(span_context: SpanContext, /) -> str:
    """Format a version-00 ``traceparent`` (the sampled flag comes from the context)."""
    raise NotImplementedError


def span_context_from_traceparent(
    traceparent: str, /, tracestate: str | None = None
) -> SpanContext:
    """Build a remote :class:`SpanContext`.

    Raises :class:`InvalidTraceparent`. An invalid ``tracestate`` is ignored
    (empty state), as the spec requires.
    """
    raise NotImplementedError


def parse_tracestate(value: str, /) -> TraceState:
    """Parse a (possibly comma-joined) ``tracestate``. Raises :class:`InvalidTracestate`."""
    raise NotImplementedError


def format_tracestate(state: TraceState | Iterable[tuple[str, str]], /) -> str:
    """Format entries as a ``tracestate`` header value (invalid entries are skipped)."""
    raise NotImplementedError


def is_valid_tracestate_key(key: str, /) -> bool:
    """Whether ``key`` is a valid simple or ``tenant@system`` key."""
    raise NotImplementedError


def is_valid_tracestate_value(value: str, /) -> bool:
    """Whether ``value`` is a valid tracestate value (printable ASCII, no ``,=``)."""
    raise NotImplementedError
