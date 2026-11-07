"""W3C Trace Context parsing and formatting (protocol-agnostic).

``traceparent``: ``00-{trace-id}-{parent-id}-{flags}``; lowercase hex only;
trace and parent IDs must be non-zero; version ``ff`` is invalid; future
versions are accepted when the first four fields are valid.

``tracestate``: ``key1=value1,key2=value2`` with multi-tenant
``{tenant}@{system}`` keys, at most :data:`MAX_TRACESTATE_ENTRIES` entries.
Optional whitespace around list members is ignored; empty members are skipped.
"""

from __future__ import annotations

import contextlib
import re
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
MAX_TRACESTATE_ENTRIES = TraceState.MAX_ENTRIES
SAMPLED_FLAG = 0x01


class InvalidTraceparent(ValueError):
    """The ``traceparent`` value is malformed."""


class InvalidTracestate(ValueError):
    """The ``tracestate`` value is malformed or has too many entries."""


_TRACEPARENT = re.compile(r"([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?", re.S)
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16
_KEY_CHARS = r"[a-z0-9_\-*/]"
_TRACESTATE_KEY = re.compile(
    rf"[a-z]{_KEY_CHARS}{{0,255}}|[a-z0-9]{_KEY_CHARS}{{0,240}}@[a-z]{_KEY_CHARS}{{0,13}}"
)
# Printable ASCII except "," and "="; the last character must not be a space.
_TRACESTATE_VALUE = re.compile(r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]")
_OWS = " \t"


def parse_traceparent(value: str, /) -> tuple[bytes, bytes, int]:
    """Parse ``traceparent`` into ``(trace_id, span_id, flags)``.

    Raises :class:`InvalidTraceparent`.
    """
    match = _TRACEPARENT.fullmatch(value)
    if match is None:
        raise InvalidTraceparent("malformed traceparent")
    version, trace_id, span_id, flags, rest = match.groups()
    if version == "ff":
        raise InvalidTraceparent("traceparent version ff is forbidden")
    if version == "00" and rest is not None:
        raise InvalidTraceparent("version 00 traceparent has extra fields")
    if trace_id == _ZERO_TRACE_ID or span_id == _ZERO_SPAN_ID:
        raise InvalidTraceparent("traceparent IDs must be non-zero")
    return bytes.fromhex(trace_id), bytes.fromhex(span_id), int(flags, 16)


def format_traceparent(span_context: SpanContext, /) -> str:
    """Format a version-00 ``traceparent`` (the sampled flag comes from the context)."""
    flags = "01" if span_context.sampled else "00"
    return f"00-{span_context.trace_id_hex}-{span_context.span_id_hex}-{flags}"


def span_context_from_traceparent(
    traceparent: str, /, tracestate: str | None = None
) -> SpanContext:
    """Build a remote :class:`SpanContext`.

    Raises :class:`InvalidTraceparent`. An invalid ``tracestate`` is ignored
    (empty state), as the spec requires.
    """
    trace_id, span_id, flags = parse_traceparent(traceparent)
    state = TraceState()
    if tracestate:
        with contextlib.suppress(InvalidTracestate):
            state = parse_tracestate(tracestate)
    return SpanContext(trace_id, span_id, flags, state, is_remote=True)


def parse_tracestate(value: str, /) -> TraceState:
    """Parse a (possibly comma-joined) ``tracestate``. Raises :class:`InvalidTracestate`.

    Duplicate keys and more than :data:`MAX_TRACESTATE_ENTRIES` members are
    errors.
    """
    entries: dict[str, str] = {}
    for raw in value.split(","):
        member = raw.strip(_OWS)
        if not member:
            continue
        key, sep, val = member.partition("=")
        if not (sep and is_valid_tracestate_key(key) and is_valid_tracestate_value(val)):
            raise InvalidTracestate("malformed tracestate member")
        if key in entries:
            raise InvalidTracestate("duplicate tracestate key")
        if len(entries) == MAX_TRACESTATE_ENTRIES:
            raise InvalidTracestate(f"tracestate has more than {MAX_TRACESTATE_ENTRIES} entries")
        entries[key] = val
    return TraceState(entries.items())


def format_tracestate(state: TraceState | Iterable[tuple[str, str]], /) -> str:
    """Format entries as a ``tracestate`` header value.

    Invalid entries are skipped, the first of duplicate keys wins and at most
    :data:`MAX_TRACESTATE_ENTRIES` members are written.
    """
    pairs = state.entries() if isinstance(state, TraceState) else state
    members: dict[str, str] = {}
    for key, value in pairs:
        if len(members) == MAX_TRACESTATE_ENTRIES:
            break
        if key not in members and is_valid_tracestate_key(key) and is_valid_tracestate_value(value):
            members[key] = value
    return ",".join(f"{key}={value}" for key, value in members.items())


def is_valid_tracestate_key(key: str, /) -> bool:
    """Whether ``key`` is a valid simple or ``tenant@system`` key."""
    return _TRACESTATE_KEY.fullmatch(key) is not None


def is_valid_tracestate_value(value: str, /) -> bool:
    """Whether ``value`` is a valid tracestate value (printable ASCII, no ``,=``)."""
    return _TRACESTATE_VALUE.fullmatch(value) is not None
