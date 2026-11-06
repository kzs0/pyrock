"""Tracing core: spans, span contexts, tracer, and the active-span context var.

Users rarely touch these directly; operations and steps are the interface.
IDs come from :func:`os.urandom` (fast and fork-safe).

Import rules: ``pyrock.trace`` imports only ``pyrock.attr`` and
``pyrock.privacy`` from pyrock.
"""

from __future__ import annotations

import contextvars
import enum
import os
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from pyrock.attr import AttrValue, error
from pyrock.privacy import Policy
from pyrock.trace.sampler import AlwaysOn, ParentBased, Sampler

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
INVALID_SPAN_ID = bytes(8)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_bytes_of(value: object, size: int) -> bool:
    return isinstance(value, bytes) and len(value) == size


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
    """Return a random, non-zero 16-byte trace ID (from :func:`os.urandom`, fork-safe)."""
    while (trace_id := os.urandom(16)) == INVALID_TRACE_ID:
        pass
    return trace_id


def new_span_id() -> bytes:
    """Return a random, non-zero 8-byte span ID (from :func:`os.urandom`, fork-safe)."""
    while (span_id := os.urandom(8)) == INVALID_SPAN_ID:
        pass
    return span_id


_SAMPLED = 0x01
_UNSET_STATUS: tuple[StatusCode, str] = (StatusCode.UNSET, "")


class Span:
    """A unit of traced work. Thread-safe; mutations after :meth:`end` are ignored.

    A span created by a :class:`Tracer` cleans every attribute, event and
    status message with the tracer's privacy policy as it is recorded. Its end
    time defaults to the start time plus the elapsed monotonic time, so a wall
    clock step never yields a negative duration.
    """

    __slots__ = (
        "_attrs",
        "_context",
        "_end_ns",
        "_ended",
        "_events",
        "_kind",
        "_live",
        "_lock",
        "_name",
        "_parent",
        "_policy",
        "_start_mono",
        "_start_ns",
        "_status",
        "_tracer",
    )

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
        # Wall clock first, then the monotonic anchor: derived times never run ahead.
        self._start_ns = time.time_ns() if start_time_ns is None else start_time_ns
        self._start_mono = time.perf_counter_ns()
        self._name = name
        self._context = context
        self._parent = parent
        self._kind = kind
        self._tracer = tracer
        self._policy = None if tracer is None else tracer._policy
        self._attrs: dict[str, AttrValue] = self._clean(attrs) if recording and attrs else {}
        self._events: list[SpanEvent] | None = None
        self._status = _UNSET_STATUS
        self._end_ns: int | None = None
        self._ended = False
        self._live = recording
        self._lock = threading.Lock()

    # -- read-only state ---------------------------------------------------

    @property
    def name(self) -> str:
        """The span name."""
        return self._name

    @property
    def context(self) -> SpanContext:
        """This span's identity (trace ID, span ID, flags, trace state)."""
        return self._context

    @property
    def parent(self) -> SpanContext | None:
        """The parent's context (local or remote), or ``None`` for a root span."""
        return self._parent

    @property
    def kind(self) -> SpanKind:
        """The span kind."""
        return self._kind

    @property
    def start_time_ns(self) -> int:
        """Start time in nanoseconds since the Unix epoch."""
        return self._start_ns

    @property
    def end_time_ns(self) -> int | None:
        """End time in nanoseconds since the Unix epoch, or ``None`` while running."""
        with self._lock:
            return self._end_ns

    @property
    def attrs(self) -> Mapping[str, AttrValue]:
        """A snapshot of the span attributes."""
        with self._lock:
            if self._ended:  # frozen from now on: share instead of copying
                return MappingProxyType(self._attrs)
            return dict(self._attrs)

    @property
    def events(self) -> Sequence[SpanEvent]:
        """A snapshot of the span events."""
        with self._lock:
            return tuple(self._events or ())

    @property
    def status(self) -> tuple[StatusCode, str]:
        """``(code, message)``."""
        with self._lock:
            return self._status

    @property
    def ended(self) -> bool:
        """Whether :meth:`end` has been called."""
        with self._lock:
            return self._ended

    @property
    def is_recording(self) -> bool:
        """False for spans that will not be exported (dropped by sampling, or ended)."""
        return self._live

    @property
    def duration(self) -> float | None:
        """Seconds between start and end, or ``None`` while running."""
        end = self.end_time_ns
        return None if end is None else (end - self._start_ns) / 1e9

    # -- mutation ----------------------------------------------------------

    def set_attr(self, key: str, value: AttrValue, /) -> None:
        """Set one attribute (privacy-filtered; a dropped value leaves the key unset)."""
        if not self._live:
            return
        policy = self._policy
        if policy is not None:
            value, keep = policy.value(key, value)
            if not keep:
                return
        with self._lock:
            if self._live:
                self._attrs[key] = value

    def set_attrs(self, attrs: Mapping[str, AttrValue], /) -> None:
        """Merge several attributes (privacy-filtered); later values win."""
        if not self._live or not attrs:
            return
        cleaned = self._clean(attrs)
        with self._lock:
            if self._live:
                self._attrs.update(cleaned)

    def add_event(
        self,
        name: str,
        /,
        attrs: Mapping[str, AttrValue] | None = None,
        *,
        timestamp_ns: int | None = None,
    ) -> None:
        """Record a named event, timestamped now unless ``timestamp_ns`` is given."""
        if not self._live:
            return
        event = SpanEvent(
            name,
            self._now_ns() if timestamp_ns is None else timestamp_ns,
            self._clean(attrs) if attrs else {},
        )
        with self._lock:
            if self._live:
                if self._events is None:
                    self._events = [event]
                else:
                    self._events.append(event)

    def record_exception(
        self, exc: BaseException, /, attrs: Mapping[str, AttrValue] | None = None
    ) -> None:
        """Add an ``exception`` event (privacy-filtered).

        The event carries :func:`pyrock.attr.error` attributes (``error.type``,
        ``error.fingerprint``, ...; ``error.message`` only when the policy
        allows error details) plus ``attrs``. The status is left unchanged.
        """
        if not self._live:
            return
        policy = self._policy
        event_attrs = error(
            exc,
            allow_details=policy is not None and policy.allow_error_details,
            allow_source_paths=policy is not None and policy.allow_source_paths,
        )
        if attrs:
            event_attrs.update(attrs)
        self.add_event("exception", event_attrs)

    def set_status(self, code: StatusCode, message: str = "", /) -> None:
        """Set the status; the message is cleaned by the privacy policy."""
        if not self._live:
            return
        if self._policy is not None:
            message = self._policy.message(message)
        with self._lock:
            if self._live:
                self._status = (code, message)

    def end(self, *, end_time_ns: int | None = None) -> None:
        """End the span (idempotent) and hand it to the tracer's processor if sampled."""
        with self._lock:
            if self._ended:
                return
            self._ended = True
            self._live = False
            self._end_ns = self._now_ns() if end_time_ns is None else end_time_ns
        tracer = self._tracer
        if tracer is not None and self._context.trace_flags & _SAMPLED:
            processor = tracer._processor
            if processor is not None:
                processor.on_end(self)

    # -- helpers -----------------------------------------------------------

    def _now_ns(self) -> int:
        return self._start_ns + (time.perf_counter_ns() - self._start_mono)

    def _clean(self, attrs: Mapping[str, AttrValue]) -> dict[str, AttrValue]:
        policy = self._policy
        return dict(attrs) if policy is None else policy.apply(attrs)

    def __repr__(self) -> str:
        ctx = self._context
        return f"Span({self._name!r}, trace_id={ctx.trace_id_hex}, span_id={ctx.span_id_hex})"


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
    """Creates spans, applies sampling and the data policy, and hands ended spans off.

    ``policy`` defaults to :class:`pyrock.privacy.Policy` defaults; the
    resource is filtered with :meth:`~pyrock.privacy.Policy.resource` once, at
    construction. ``sampler`` defaults to ``ParentBased(AlwaysOn())``. Only
    sampled spans reach ``processor``.
    """

    __slots__ = ("_policy", "_processor", "_resource", "_sampler", "_service")

    def __init__(
        self,
        *,
        service: str,
        resource: Mapping[str, AttrValue] | None = None,
        sampler: Sampler | None = None,
        processor: SpanProcessor | None = None,
        policy: Policy | None = None,
    ) -> None:
        self._service = service
        self._policy = Policy() if policy is None else policy
        self._resource: Mapping[str, AttrValue] = MappingProxyType(
            self._policy.resource(resource or {})
        )
        self._sampler: Sampler = ParentBased(AlwaysOn()) if sampler is None else sampler
        self._processor = processor

    @property
    def service(self) -> str:
        """The ``service.name`` resource attribute."""
        return self._service

    @property
    def resource(self) -> Mapping[str, AttrValue]:
        """Resource attributes, already filtered by the privacy policy."""
        return self._resource

    @property
    def processor(self) -> SpanProcessor | None:
        """The processor that receives sampled, ended spans."""
        return self._processor

    def set_processor(self, processor: SpanProcessor | None, /) -> None:
        """Replace the span processor (used by cloud wiring)."""
        self._processor = processor

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
        """Start a span. ``parent=None`` starts a new trace; a remote context continues one.

        An invalid parent context is treated as absent. The child keeps the
        parent's trace ID and trace state; the sampler sees the parent's
        *sampled* flag (``None`` for a root span).
        """
        parent_ctx = parent.context if isinstance(parent, Span) else parent
        parent_sampled: bool | None
        if parent_ctx is not None and parent_ctx.is_valid:
            trace_id = parent_ctx.trace_id
            trace_state = parent_ctx.trace_state
            parent_sampled = bool(parent_ctx.trace_flags & _SAMPLED)
        else:
            parent_ctx = None
            trace_id = new_trace_id()
            trace_state = _EMPTY_TRACE_STATE
            parent_sampled = None
        result = self._sampler.should_sample(trace_id, name, parent_sampled)
        if result.attrs:
            attrs = {**(attrs or {}), **result.attrs}
        context = SpanContext(trace_id, new_span_id(), int(result.sampled), trace_state)
        return Span(
            name,
            context,
            parent=parent_ctx,
            kind=kind,
            attrs=attrs,
            start_time_ns=start_time_ns,
            tracer=self,
            recording=result.recording,
        )

    def force_flush(self, *, timeout: float | None = None) -> bool:
        """Flush the processor; ``True`` when there is nothing left to export."""
        processor = self._processor
        return True if processor is None else processor.force_flush(timeout=timeout)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Shut the processor down (flushing what it holds)."""
        processor = self._processor
        if processor is not None:
            processor.shutdown(timeout=timeout)


_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "pyrock.trace.current_span", default=None
)


def current_span() -> Span | None:
    """Return the innermost live span in this context, or ``None``."""
    return _current_span.get()


def current_span_context() -> SpanContext | None:
    """Return the innermost span's context, or ``None``."""
    span = _current_span.get()
    return None if span is None else span._context


def use_span(span: Span | None, /) -> contextvars.Token[Span | None]:
    """Make ``span`` current; pass the returned token to ``ContextVar.reset``."""
    return _current_span.set(span)


# Re-exported last: pyrock.trace.propagator imports SpanContext from here.
from pyrock.trace.propagator import CompositePropagator, Propagator  # noqa: E402
