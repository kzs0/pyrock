"""Tracing core: spans, span contexts, tracer, and the active-span context var.

Users rarely touch these directly; operations and steps are the interface.
IDs come from :func:`os.urandom` (fast and fork-safe).

Import rules: ``pyrock.trace`` imports only ``pyrock.attr`` and
``pyrock.privacy`` from pyrock.
"""

from __future__ import annotations

import contextvars
import enum
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pyrock.attr import AttrValue

if TYPE_CHECKING:
    from pyrock.privacy import Policy
    from pyrock.trace.sampler import Sampler

__all__ = [
    "INVALID_SPAN_ID",
    "INVALID_TRACE_ID",
    "CompositePropagator",
    "Propagator",
    "Span",
    "SpanContext",
    "SpanEvent",
    "SpanExporter",
    "SpanKind",
    "SpanProcessor",
    "StatusCode",
    "TraceState",
    "Tracer",
    "current_span",
    "current_span_context",
    "new_span_id",
    "new_trace_id",
    "use_span",
]

INVALID_TRACE_ID = bytes(16)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_bytes_of(value: object, size: int) -> bool:
    return isinstance(value, bytes) and len(value) == size


INVALID_SPAN_ID = bytes(8)


class SpanKind(enum.IntEnum):
    """Span kind; values match OTLP ``Span.SpanKind``."""

    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class StatusCode(enum.IntEnum):
    """Span status code; values match OTLP ``Status.StatusCode``."""

    UNSET = 0
    OK = 1
    ERROR = 2


class TraceState(Mapping[str, str]):
    """Immutable, ordered W3C ``tracestate`` entries (passthrough propagation).

    Order is significant: the most recently updated vendor entry comes first.
    Parsing/validation lives in :mod:`pyrock.trace.w3c`; this type only stores
    entries. At most :data:`pyrock.trace.w3c.MAX_TRACESTATE_ENTRIES` entries are
    meaningful; constructing a larger state raises ``ValueError``.
    """

    __slots__ = ("_entries", "_index")

    MAX_ENTRIES = 32

    _entries: tuple[tuple[str, str], ...]
    _index: dict[str, str]

    def __init__(self, entries: Iterable[tuple[str, str]] = (), /) -> None:
        seen: dict[str, str] = {}
        ordered: list[tuple[str, str]] = []
        for key, value in entries:
            if not (_is_str(key) and _is_str(value)):
                raise TypeError("tracestate keys and values must be str")
            if key in seen:
                continue  # first occurrence wins, like W3C "most recent first"
            seen[key] = value
            ordered.append((key, value))
        if len(ordered) > self.MAX_ENTRIES:
            raise ValueError(f"tracestate has more than {self.MAX_ENTRIES} entries")
        object.__setattr__(self, "_entries", tuple(ordered))
        object.__setattr__(self, "_index", seen)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("TraceState is immutable")

    def __getitem__(self, key: str) -> str:
        return self._index[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TraceState):
            return self._entries == other._entries
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._entries)

    def __repr__(self) -> str:
        return f"TraceState({list(self._entries)!r})"

    def entries(self) -> tuple[tuple[str, str], ...]:
        """Return the ``(key, value)`` pairs in header order."""
        return self._entries

    def with_entry(self, key: str, value: str, /) -> TraceState:
        """Return a copy with ``key`` set and moved to the front (W3C update rule)."""
        rest = [(k, v) for k, v in self._entries if k != key]
        return TraceState([(key, value), *rest][: self.MAX_ENTRIES])

    def without(self, key: str, /) -> TraceState:
        """Return a copy without ``key``."""
        return TraceState((k, v) for k, v in self._entries if k != key)


_EMPTY_TRACE_STATE = TraceState()


@dataclass(frozen=True, slots=True)
class SpanContext:
    """The propagated identity of a span.

    ``trace_id`` is 16 bytes, ``span_id`` 8 bytes; ``trace_flags`` bit 0 is
    *sampled*. Hex forms are computed once and cached.
    """

    trace_id: bytes
    span_id: bytes
    trace_flags: int = 0
    trace_state: TraceState = field(default=_EMPTY_TRACE_STATE)
    is_remote: bool = False
    _trace_id_hex: str = field(default="", init=False, repr=False, compare=False)
    _span_id_hex: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _is_bytes_of(self.trace_id, 16):
            raise ValueError("trace_id must be 16 bytes")
        if not _is_bytes_of(self.span_id, 8):
            raise ValueError("span_id must be 8 bytes")
        if not 0 <= self.trace_flags <= 0xFF:
            raise ValueError("trace_flags must fit in one byte")
        object.__setattr__(self, "_trace_id_hex", self.trace_id.hex())
        object.__setattr__(self, "_span_id_hex", self.span_id.hex())

    @classmethod
    def from_hex(
        cls,
        trace_id: str,
        span_id: str,
        /,
        *,
        sampled: bool = True,
        trace_state: TraceState | None = None,
        is_remote: bool = False,
    ) -> SpanContext:
        """Build a context from lowercase hex IDs (32 and 16 characters)."""
        if len(trace_id) != 32 or len(span_id) != 16:
            raise ValueError("trace_id must be 32 and span_id 16 hex characters")
        return cls(
            bytes.fromhex(trace_id),
            bytes.fromhex(span_id),
            0x01 if sampled else 0x00,
            trace_state if trace_state is not None else _EMPTY_TRACE_STATE,
            is_remote,
        )

    @property
    def trace_id_hex(self) -> str:
        """32 lowercase hex characters."""
        return self._trace_id_hex

    @property
    def span_id_hex(self) -> str:
        """16 lowercase hex characters."""
        return self._span_id_hex

    @property
    def sampled(self) -> bool:
        """Whether the *sampled* flag (bit 0) is set."""
        return bool(self.trace_flags & 0x01)

    @property
    def is_valid(self) -> bool:
        """Both IDs are non-zero."""
        return self.trace_id != INVALID_TRACE_ID and self.span_id != INVALID_SPAN_ID


@dataclass(frozen=True, slots=True)
class SpanEvent:
    """An event recorded on a span."""

    name: str
    timestamp_ns: int
    attrs: Mapping[str, AttrValue] = field(default_factory=dict)


def new_trace_id() -> bytes:
    """Return a random, non-zero 16-byte trace ID."""
    raise NotImplementedError


def new_span_id() -> bytes:
    """Return a random, non-zero 8-byte span ID."""
    raise NotImplementedError


class Span:
    """A unit of traced work. Thread-safe; mutations after :meth:`end` are ignored."""

    def __init__(
        self,
        name: str,
        context: SpanContext,
        *,
        parent: SpanContext | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attrs: Mapping[str, AttrValue] | None = None,
        start_time_ns: int | None = None,
        tracer: Tracer | None = None,
        recording: bool = True,
    ) -> None:
        raise NotImplementedError

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def context(self) -> SpanContext:
        raise NotImplementedError

    @property
    def parent(self) -> SpanContext | None:
        raise NotImplementedError

    @property
    def kind(self) -> SpanKind:
        raise NotImplementedError

    @property
    def start_time_ns(self) -> int:
        raise NotImplementedError

    @property
    def end_time_ns(self) -> int | None:
        raise NotImplementedError

    @property
    def attrs(self) -> Mapping[str, AttrValue]:
        """A snapshot of the span attributes."""
        raise NotImplementedError

    @property
    def events(self) -> Sequence[SpanEvent]:
        """A snapshot of the span events."""
        raise NotImplementedError

    @property
    def status(self) -> tuple[StatusCode, str]:
        """``(code, message)``."""
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    @property
    def is_recording(self) -> bool:
        """False for spans that will not be exported (dropped by sampling, or ended)."""
        raise NotImplementedError

    @property
    def duration(self) -> float | None:
        """Seconds between start and end, or ``None`` while running."""
        raise NotImplementedError

    def set_attr(self, key: str, value: AttrValue, /) -> None:
        raise NotImplementedError

    def set_attrs(self, attrs: Mapping[str, AttrValue], /) -> None:
        raise NotImplementedError

    def add_event(
        self,
        name: str,
        /,
        attrs: Mapping[str, AttrValue] | None = None,
        *,
        timestamp_ns: int | None = None,
    ) -> None:
        raise NotImplementedError

    def record_exception(
        self, exc: BaseException, /, attrs: Mapping[str, AttrValue] | None = None
    ) -> None:
        """Add an ``exception`` event (privacy-filtered)."""
        raise NotImplementedError

    def set_status(self, code: StatusCode, message: str = "", /) -> None:
        raise NotImplementedError

    def end(self, *, end_time_ns: int | None = None) -> None:
        """End the span (idempotent) and hand it to the tracer's processor."""
        raise NotImplementedError


@runtime_checkable
class SpanExporter(Protocol):
    """Sends finished spans somewhere. Raise on failure (the caller counts it)."""

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None: ...

    def shutdown(self, *, timeout: float | None = None) -> None: ...


@runtime_checkable
class SpanProcessor(Protocol):
    """Receives ended spans (e.g. :class:`pyrock.trace.otlp.batch.BatchProcessor`)."""

    def on_end(self, span: Span, /) -> None: ...

    def force_flush(self, *, timeout: float | None = None) -> bool: ...

    def shutdown(self, *, timeout: float | None = None) -> None: ...


class Tracer:
    """Creates spans, applies sampling and the data policy, and hands ended spans off."""

    def __init__(
        self,
        *,
        service: str,
        resource: Mapping[str, AttrValue] | None = None,
        sampler: Sampler | None = None,
        processor: SpanProcessor | None = None,
        policy: Policy | None = None,
    ) -> None:
        raise NotImplementedError

    @property
    def service(self) -> str:
        raise NotImplementedError

    @property
    def resource(self) -> Mapping[str, AttrValue]:
        raise NotImplementedError

    @property
    def processor(self) -> SpanProcessor | None:
        raise NotImplementedError

    def set_processor(self, processor: SpanProcessor | None, /) -> None:
        """Replace the span processor (used by cloud wiring)."""
        raise NotImplementedError

    def start_span(
        self,
        name: str,
        /,
        *,
        parent: Span | SpanContext | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attrs: Mapping[str, AttrValue] | None = None,
        start_time_ns: int | None = None,
    ) -> Span:
        """Start a span. ``parent=None`` starts a new trace; a remote context continues one."""
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        raise NotImplementedError


_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "pyrock.trace.current_span", default=None
)


def current_span() -> Span | None:
    """Return the innermost live span in this context, or ``None``."""
    raise NotImplementedError


def current_span_context() -> SpanContext | None:
    """Return the innermost span's context, or ``None``."""
    raise NotImplementedError


def use_span(span: Span | None, /) -> contextvars.Token[Span | None]:
    """Make ``span`` current; pass the returned token to ``ContextVar.reset``."""
    raise NotImplementedError


# Re-exported last: pyrock.trace.propagator imports SpanContext from here.
from pyrock.trace.propagator import CompositePropagator, Propagator  # noqa: E402
