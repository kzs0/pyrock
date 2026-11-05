"""Operations, steps and sources: context managers that are also decorators.

Every operation creates a span and records four metrics on exit
(``<name>_count``, ``_successes``, ``_failures``, ``_duration_ms``) labelled
with the declared ``metric_labels`` (missing -> ``"_"``) plus static labels.

How a scope ends:

* no exception: success, unless ``fail()`` was called;
* an ``Exception`` escapes: failure, error attrs registered, re-raised (never
  swallowed); exceptions in ``expected`` are recorded as an ``exception`` span
  event and leave the outcome a success;
* ``asyncio.CancelledError`` / ``KeyboardInterrupt``: failure with
  ``error.type``; ``GeneratorExit`` and ``SystemExit(0)`` are never failures;
* ``ExceptionGroup``: failure summarized in ``error.group.count`` /
  ``error.types``.

A second ``operation("fetch")`` under the same parent is named ``fetch[1]``,
then ``fetch[2]``; numbering applies to span names only. Operations inside a
source are prefixed with the source name.

Note: ``pyrock.operation`` (the attribute) is the :func:`operation` function;
the classes in this module are re-exported from ``pyrock``.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable, Mapping
from types import TracebackType
from typing import TYPE_CHECKING, TypeVar

from pyrock.attr import AttrValue, Event
from pyrock.trace import SpanContext, SpanKind

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock

__all__ = [
    "Operation",
    "OperationScope",
    "RegisterItem",
    "Source",
    "SourceScope",
    "Step",
    "StepScope",
    "operation",
    "source",
    "start_operation",
    "step",
]

F = TypeVar("F", bound=Callable[..., object])

RegisterItem = Mapping[str, AttrValue] | Event | BaseException
"""What ``register(*items)`` accepts besides keyword attributes."""

ExpectedExceptions = tuple[type[BaseException], ...]


class Operation:
    """A live operation (the value bound by ``with pyrock.operation(...) as op``)."""

    @property
    def name(self) -> str:
        """The span name, including a source prefix and any ``[n]`` suffix."""
        raise NotImplementedError

    @property
    def metric_name(self) -> str:
        """The base name used for metric families (no ``[n]`` suffix)."""
        raise NotImplementedError

    @property
    def span_context(self) -> SpanContext | None:
        """This operation's span context (the enclosing one under ``no_trace``)."""
        raise NotImplementedError

    @property
    def trace_id(self) -> str | None:
        """Hex trace ID, or ``None`` when there is no trace."""
        raise NotImplementedError

    @property
    def span_id(self) -> str | None:
        """Hex span ID, or ``None`` when there is no span."""
        raise NotImplementedError

    @property
    def failed(self) -> bool:
        raise NotImplementedError

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    def register(self, *items: RegisterItem, **attrs: AttrValue) -> None:
        """Accumulate attributes; an exception item marks failure, an Event is recorded."""
        raise NotImplementedError

    def event(
        self, name: str, /, attrs: Mapping[str, AttrValue] | None = None, **kwargs: AttrValue
    ) -> None:
        """Record a span event immediately (not added to the operation attrs)."""
        raise NotImplementedError

    def fail(self, exc: BaseException | None = None, /) -> None:
        """Mark failure (with error attrs when ``exc`` is given) without raising."""
        raise NotImplementedError

    def succeed(self) -> None:
        """Clear an earlier failure (retry loops that recover)."""
        raise NotImplementedError

    def end(self, *, error: BaseException | None = None, success: bool | None = None) -> None:
        """End explicitly (idempotent): sync attrs, end the span, record metrics, log."""
        raise NotImplementedError

    def done(self) -> None:
        """Alias for :meth:`end` with no arguments (Go: ``Done``)."""
        raise NotImplementedError

    def activate(self) -> contextvars.Token[Operation | None]:
        """Make this the current operation in this context (for :func:`start_operation`)."""
        raise NotImplementedError

    def deactivate(self, token: contextvars.Token[Operation | None], /) -> None:
        raise NotImplementedError


class Step:
    """A live step: a span whose attributes also propagate to the parent operation."""

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def span_context(self) -> SpanContext | None:
        raise NotImplementedError

    @property
    def trace_id(self) -> str | None:
        raise NotImplementedError

    @property
    def span_id(self) -> str | None:
        raise NotImplementedError

    def register(self, *items: RegisterItem, **attrs: AttrValue) -> None:
        """Set attributes on the step span and propagate them to the parent operation."""
        raise NotImplementedError

    def event(
        self, name: str, /, attrs: Mapping[str, AttrValue] | None = None, **kwargs: AttrValue
    ) -> None:
        raise NotImplementedError

    def done(self) -> None:
        """End the step span (idempotent)."""
        raise NotImplementedError


class Source:
    """A long-lived process that prefixes child operations and records aggregates."""

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def attrs(self) -> Mapping[str, AttrValue]:
        raise NotImplementedError

    @property
    def metric_labels(self) -> tuple[str, ...]:
        raise NotImplementedError

    def sum(self, name: str, value: float = 1.0, /) -> None:
        """Add to the counter ``<source>_<name>`` (source labels + static labels)."""
        raise NotImplementedError

    def gauge(self, name: str, value: float, /) -> None:
        """Set the gauge ``<source>_<name>``."""
        raise NotImplementedError

    def histogram(self, name: str, value: float, /) -> None:
        """Observe ``value`` in the histogram ``<source>_<name>``."""
        raise NotImplementedError

    def done(self) -> None:
        """Kept for symmetry with Go; leaving the scope restores the previous context."""
        raise NotImplementedError


class _Scope:
    """Shared context-manager/decorator protocol for scopes.

    Each ``with`` entry (or decorated call) starts a new live object; a scope
    may be reused and nested. Decorators support sync functions, coroutines,
    generators and async generators (the scope spans the whole iteration).
    """

    def __call__(self, fn: F, /) -> F:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class OperationScope(_Scope):
    """Returned by :func:`operation`."""

    def __enter__(self) -> Operation:
        raise NotImplementedError

    async def __aenter__(self) -> Operation:
        raise NotImplementedError


class StepScope(_Scope):
    """Returned by :func:`step`."""

    def __enter__(self) -> Step:
        raise NotImplementedError

    async def __aenter__(self) -> Step:
        raise NotImplementedError


class SourceScope(_Scope):
    """Returned by :func:`source`."""

    def __enter__(self) -> Source:
        raise NotImplementedError

    async def __aenter__(self) -> Source:
        raise NotImplementedError


def operation(
    name: str,
    /,
    *,
    attrs: Mapping[str, AttrValue] | None = None,
    metric_labels: Iterable[str] = (),
    no_trace: bool = False,
    remote_parent: SpanContext | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    expected: ExpectedExceptions = (),
    bedrock: Bedrock | None = None,
) -> OperationScope:
    """Open an operation (context manager or decorator).

    ``bedrock`` pins an explicit instance; by default the active one is
    resolved from context when the scope is entered (noop if none).
    """
    raise NotImplementedError


def start_operation(
    name: str,
    /,
    *,
    attrs: Mapping[str, AttrValue] | None = None,
    metric_labels: Iterable[str] = (),
    no_trace: bool = False,
    remote_parent: SpanContext | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    expected: ExpectedExceptions = (),
    bedrock: Bedrock | None = None,
) -> Operation:
    """Start an operation now without activating it (manual lifecycle; call ``end()``)."""
    raise NotImplementedError


def step(
    name: str,
    /,
    *,
    attrs: Mapping[str, AttrValue] | None = None,
    no_trace: bool = False,
    expected: ExpectedExceptions = (),
    bedrock: Bedrock | None = None,
) -> StepScope:
    """Open a step. Without an active operation it still spans if a trace is active."""
    raise NotImplementedError


def source(
    name: str,
    /,
    *,
    attrs: Mapping[str, AttrValue] | None = None,
    metric_labels: Iterable[str] = (),
    bedrock: Bedrock | None = None,
) -> SourceScope:
    """Open a source: child operations become ``<source>.<name>`` and share attrs/labels."""
    raise NotImplementedError
