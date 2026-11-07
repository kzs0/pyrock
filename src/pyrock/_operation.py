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

Duplicate-name numbering: a second ``operation("fetch")`` (or ``step``) under
the same parent *operation or step* is named ``fetch[1]``, then ``fetch[2]``.
Root operations and direct children of a source are never numbered, so span
names stay bounded in loops. Numbering applies to span names only; metric
families use the base name. Operations inside a source are prefixed with the
source name and inherit its attrs and metric labels.

Decorators always wrap (``functools.wraps``) and resolve the active instance
**at call time**, so functions decorated at import time, before ``init()``,
are instrumented once an instance is active. With no active instance the
wrapper costs one ``ContextVar.get()`` plus a direct call. Only an explicit
noop ``bedrock=`` makes the decorator return the function unchanged.
Generators and async generators are instrumented for their whole iteration;
their scope is made current only while their body runs, so the consumer's
context is never changed between items.

Lifecycle of a live object: ``_start`` (create, span started, not current),
``_activate``/``_deactivate`` (make it current in this context and undo that)
and ``_close`` (outcome from an escaping exception, then end). Attributes are
accumulated under the object's lock and written to the span in bulk at the end.

This module is private; its public names are re-exported from ``pyrock``.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Generator, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar, cast

from pyrock import logs
from pyrock._context import bedrock_var, no_trace_var, operation_var, source_var, step_var
from pyrock.attr import ERROR_KEY, AttrValue, Event
from pyrock.metric import Counter, Histogram
from pyrock.trace import (
    Span,
    SpanContext,
    SpanKind,
    StatusCode,
    current_span,
    use_span,
)

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock

__all__ = [
    "Activation",
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
L = TypeVar("L", "Operation", "Step", "Source")

RegisterItem = Mapping[str, AttrValue] | Event | BaseException
"""What ``register(*items)`` accepts besides keyword attributes."""

ExpectedExceptions = tuple[type[BaseException], ...]

_Tokens = tuple[contextvars.Token[Any], ...]


class Activation:
    """Opaque handle returned by :meth:`Operation.activate`; pass it to ``deactivate``."""

    __slots__ = ("_tokens",)

    def __init__(self, tokens: _Tokens, /) -> None:
        self._tokens = tokens


_NO_TOKENS: _Tokens = ()


def _restore(tokens: _Tokens) -> None:
    """Reset context variables in reverse order of setting."""
    for token in reversed(tokens):
        token.var.reset(token)


def _common_tokens(bedrock: Bedrock, span: Span | None, no_trace: bool) -> list[Any]:
    """Tokens every scope may need: its instance, its span and the no-trace flag."""
    tokens: list[Any] = []
    if bedrock_var.get() is not bedrock:
        tokens.append(bedrock_var.set(bedrock))
    if span is not None:
        tokens.append(use_span(span))
    if no_trace and not no_trace_var.get():
        tokens.append(no_trace_var.set(True))
    return tokens


def _is_failure(exc: BaseException) -> bool:
    """``GeneratorExit`` and a successful ``SystemExit`` are never failures."""
    if isinstance(exc, GeneratorExit):
        return False
    if isinstance(exc, SystemExit):
        return exc.code not in (0, None)
    return True


def _enclosing_context() -> SpanContext | None:
    """The innermost span's context, else the innermost operation's (remote or untraced)."""
    span = current_span()
    if span is not None:
        return span.context
    op = operation_var.get()
    return None if op is None else op._context


def _numbered(parent: Operation | Step, name: str) -> str:
    """``name`` for the first child of ``parent`` so named, then ``name[1]``, ``name[2]``..."""
    with parent._lock:
        children = parent._children
        if children is None:
            children = parent._children = {}
        seen = children.get(name, 0)
        children[name] = seen + 1
    return name if seen == 0 else f"{name}[{seen}]"


def _child_span_name(name: str, source: Source | None) -> str:
    """Number ``name`` under the innermost step or operation.

    Root operations and direct children of a source are never numbered.
    """
    parent: Operation | Step | None = step_var.get() or operation_var.get()
    if parent is None or (source is not None and parent is source._enclosing):
        return name
    return _numbered(parent, name)


def _hex(context: SpanContext | None, *, span_id: bool) -> str | None:
    if context is None:
        return None
    return context.span_id_hex if span_id else context.trace_id_hex


@dataclass(frozen=True, slots=True)
class _OperationMetrics:
    """The four metric families of one operation schema (cached per instance)."""

    labels: tuple[str, ...]
    """Declared label keys, without the ones that are static (constant) labels."""
    count: Counter
    successes: Counter
    failures: Counter
    duration: Histogram

    def record(self, attrs: Mapping[str, AttrValue], success: bool, duration_ms: float) -> None:
        values = {key: attrs[key] for key in self.labels if key in attrs}
        self.count.labels(values).inc()
        # Bind both outcomes so a label set always exposes its zero series too.
        successes, failures = self.successes.labels(values), self.failures.labels(values)
        (successes if success else failures).inc()
        self.duration.labels(values).observe(duration_ms)


# --------------------------------------------------------------------- operation


class Operation:
    """A live operation (the value bound by ``with pyrock.operation(...) as op``)."""

    __slots__ = (
        "_attrs",
        "_bedrock",
        "_children",
        "_context",
        "_ended",
        "_expected",
        "_failed",
        "_lock",
        "_metric_name",
        "_metrics",
        "_name",
        "_no_trace",
        "_scope",
        "_span",
        "_start_ns",
        "_tokens",
    )

    _attrs: dict[str, AttrValue]
    _children: dict[str, int] | None
    _context: SpanContext | None
    _lock: threading.Lock

    def __init__(
        self,
        bedrock: Bedrock,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None,
        metric_labels: tuple[str, ...],
        no_trace: bool,
        remote_parent: SpanContext | None,
        kind: SpanKind,
        expected: ExpectedExceptions,
    ) -> None:
        """Start an operation in the current context (not yet current; see :meth:`activate`)."""
        source = source_var.get()
        merged = dict(attrs) if attrs else {}
        if source is not None:
            name = f"{source._name}.{name}"
            if source._attrs:
                merged = {**source._attrs, **merged}
            if source._labels:
                metric_labels = (
                    *source._labels,
                    *(key for key in metric_labels if key not in source._labels),
                )
        self._bedrock = bedrock
        self._metric_name = name
        self._metrics = bedrock._operation_metrics(name, metric_labels)
        self._name = _child_span_name(name, source)
        self._attrs = merged
        self._expected = expected
        self._failed = False
        self._ended = False
        self._children = None
        self._lock = threading.Lock()
        self._scope: OperationScope | None = None
        self._tokens = _NO_TOKENS
        self._no_trace = no_trace or no_trace_var.get()
        if self._no_trace:
            self._span: Span | None = None
            self._context = remote_parent if remote_parent is not None else _enclosing_context()
        else:
            parent = remote_parent if remote_parent is not None else current_span()
            self._span = bedrock.tracer.start_span(self._name, parent=parent, kind=kind)
            self._context = self._span.context
        self._start_ns = time.perf_counter_ns()

    # -- accessors -----------------------------------------------------------

    @property
    def name(self) -> str:
        """The span name, including a source prefix and any ``[n]`` suffix."""
        return self._name

    @property
    def metric_name(self) -> str:
        """The base name used for metric families (no ``[n]`` suffix)."""
        return self._metric_name

    @property
    def span_context(self) -> SpanContext | None:
        """This operation's span context (the enclosing one under ``no_trace``)."""
        return self._context

    @property
    def trace_id(self) -> str | None:
        """Hex trace ID, or ``None`` when there is no trace."""
        return _hex(self._context, span_id=False)

    @property
    def span_id(self) -> str | None:
        """Hex span ID, or ``None`` when there is no span."""
        return _hex(self._context, span_id=True)

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def ended(self) -> bool:
        return self._ended

    # -- recording -------------------------------------------------------------

    def register(self, *items: RegisterItem, **attrs: AttrValue) -> None:
        """Accumulate attributes; an exception item marks failure, an Event is recorded."""
        for item in items:
            if isinstance(item, Event):
                self.event(item.name, item.attrs)
            elif isinstance(item, BaseException):
                self.fail(item)
            else:
                self._merge(item)
        if attrs:
            self._merge(attrs)

    def event(
        self, name: str, /, attrs: Mapping[str, AttrValue] | None = None, **kwargs: AttrValue
    ) -> None:
        """Record a span event immediately (not added to the operation attrs)."""
        if self._span is not None:
            self._span.add_event(name, {**attrs, **kwargs} if attrs and kwargs else attrs or kwargs)

    def fail(self, exc: BaseException | None = None, /) -> None:
        """Mark failure (with error attrs when ``exc`` is given) without raising."""
        details = None if exc is None else self._bedrock._error_attrs(exc)
        with self._lock:
            if self._ended:
                return
            self._failed = True
            if details is not None:
                self._attrs.update(details)
        if exc is not None and self._span is not None:
            self._span.record_exception(exc)

    def succeed(self) -> None:
        """Clear an earlier failure and its ``error.*`` attrs (retry loops that recover)."""
        with self._lock:
            if self._ended:
                return
            self._failed = False
            for key in [k for k in self._attrs if k == ERROR_KEY or k.startswith("error.")]:
                del self._attrs[key]

    def end(self, *, error: BaseException | None = None, success: bool | None = None) -> None:
        """End explicitly (idempotent): sync attrs, end the span, record metrics, log.

        ``error`` marks failure first; ``success`` then forces the outcome.
        """
        if self._ended:
            return
        if error is not None:
            self.fail(error)
        if success is True:
            self.succeed()
        elif success is False and error is None:
            self.fail()
        self._finish()

    def done(self) -> None:
        """Alias for :meth:`end` with no arguments (Go: ``Done``)."""
        self._finish()

    # -- activation -------------------------------------------------------------

    def activate(self) -> Activation:
        """Make this the current operation in this context (for :func:`start_operation`)."""
        return Activation(self._activate())

    def deactivate(self, activation: Activation, /) -> None:
        """Undo :meth:`activate` (restores what was current before)."""
        _restore(activation._tokens)

    # -- internals ---------------------------------------------------------------

    def _merge(self, attrs: Mapping[str, AttrValue]) -> None:
        with self._lock:
            if not self._ended:
                self._attrs.update(attrs)

    def _activate(self) -> _Tokens:
        tokens = _common_tokens(self._bedrock, self._span, self._no_trace)
        tokens.append(operation_var.set(self))
        if step_var.get() is not None:
            tokens.append(step_var.set(None))
        return tuple(tokens)

    def _close(self, exc: BaseException | None) -> None:
        if exc is not None:
            if isinstance(exc, self._expected):
                if self._span is not None:
                    self._span.record_exception(exc)
            elif _is_failure(exc):
                self.fail(exc)
        self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
            attrs = self._attrs
            failed = self._failed
        duration_ms = (time.perf_counter_ns() - self._start_ns) / 1e6
        span = self._span
        if span is not None:
            span.set_attrs(attrs)
            span.set_status(StatusCode.ERROR if failed else StatusCode.OK)
            span.end()
        self._metrics.record(attrs, not failed, duration_ms)
        bedrock = self._bedrock
        if bedrock._canonical_logs:
            logs.log_canonical(
                bedrock.logger,
                operation=self._metric_name,
                duration_ms=duration_ms,
                success=not failed,
                attributes=attrs,
                span_context=self._context,
            )

    def __repr__(self) -> str:
        return f"<pyrock.Operation {self._name!r} trace_id={self.trace_id}>"


# -------------------------------------------------------------------------- step


class Step:
    """A live step: a span whose attributes also propagate to the parent operation."""

    __slots__ = (
        "_attrs",
        "_bedrock",
        "_children",
        "_ended",
        "_expected",
        "_failed",
        "_lock",
        "_name",
        "_no_trace",
        "_operation",
        "_scope",
        "_span",
        "_tokens",
    )

    _children: dict[str, int] | None
    _lock: threading.Lock

    def __init__(
        self,
        bedrock: Bedrock,
        name: str,
        /,
        *,
        operation: Operation | None,
        attrs: Mapping[str, AttrValue] | None,
        no_trace: bool,
        expected: ExpectedExceptions,
    ) -> None:
        """Start a step under ``operation`` (and the current span, if tracing)."""
        self._bedrock = bedrock
        self._operation = operation
        self._name = _child_span_name(name, None)
        self._attrs: dict[str, AttrValue] = dict(attrs) if attrs else {}
        self._expected = expected
        self._failed = False
        self._ended = False
        self._children = None
        self._lock = threading.Lock()
        self._scope: StepScope | None = None
        self._tokens = _NO_TOKENS
        self._no_trace = no_trace
        parent = None if no_trace else current_span()
        self._span = (
            None if parent is None else bedrock.tracer.start_span(self._name, parent=parent)
        )
        if operation is not None and self._attrs:
            operation._merge(self._attrs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def span_context(self) -> SpanContext | None:
        """The step span's context, else the enclosing one."""
        if self._span is not None:
            return self._span.context
        return None if self._operation is None else self._operation._context

    @property
    def trace_id(self) -> str | None:
        return _hex(self.span_context, span_id=False)

    @property
    def span_id(self) -> str | None:
        return _hex(self.span_context, span_id=True)

    def register(self, *items: RegisterItem, **attrs: AttrValue) -> None:
        """Set attributes on the step span and propagate them to the parent operation.

        An exception item marks the step span failed; it does not fail the
        operation (an exception escaping the operation does).
        """
        for item in items:
            if isinstance(item, Event):
                self.event(item.name, item.attrs)
            elif isinstance(item, BaseException):
                self._fail(item)
            else:
                self._merge(item)
        if attrs:
            self._merge(attrs)

    def event(
        self, name: str, /, attrs: Mapping[str, AttrValue] | None = None, **kwargs: AttrValue
    ) -> None:
        if self._span is not None:
            self._span.add_event(name, {**attrs, **kwargs} if attrs and kwargs else attrs or kwargs)

    def done(self) -> None:
        """End the step span (idempotent)."""
        with self._lock:
            if self._ended:
                return
            self._ended = True
        span = self._span
        if span is not None:
            span.set_attrs(self._attrs)
            if self._failed:
                span.set_status(StatusCode.ERROR)
            span.end()

    def _merge(self, attrs: Mapping[str, AttrValue]) -> None:
        with self._lock:
            if self._ended:
                return
            self._attrs.update(attrs)
        if self._operation is not None:
            self._operation._merge(attrs)

    def _fail(self, exc: BaseException) -> None:
        details = self._bedrock._error_attrs(exc)
        with self._lock:
            if self._ended:
                return
            self._failed = True
            self._attrs.update(details)
        if self._span is not None:
            self._span.record_exception(exc)

    def _activate(self) -> _Tokens:
        tokens = _common_tokens(self._bedrock, self._span, self._no_trace)
        tokens.append(step_var.set(self))
        return tuple(tokens)

    def _close(self, exc: BaseException | None) -> None:
        if exc is not None:
            if isinstance(exc, self._expected):
                if self._span is not None:
                    self._span.record_exception(exc)
            elif _is_failure(exc):
                self._fail(exc)
        self.done()

    def __repr__(self) -> str:
        return f"<pyrock.Step {self._name!r} trace_id={self.trace_id}>"


# ------------------------------------------------------------------------ source


class Source:
    """A long-lived process that prefixes child operations and records aggregates."""

    __slots__ = (
        "_attrs",
        "_bedrock",
        "_enclosing",
        "_label_values",
        "_labels",
        "_metrics",
        "_name",
        "_scope",
        "_tokens",
    )

    def __init__(
        self,
        bedrock: Bedrock,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None,
        metric_labels: tuple[str, ...],
    ) -> None:
        self._bedrock = bedrock
        self._name = name
        self._attrs: dict[str, AttrValue] = dict(attrs) if attrs else {}
        self._labels = metric_labels
        aggregate_labels = bedrock._metric_labels(metric_labels)
        self._label_values = {k: self._attrs[k] for k in aggregate_labels if k in self._attrs}
        self._enclosing: Operation | Step | None = step_var.get() or operation_var.get()
        self._metrics: dict[tuple[str, str], Any] = {}
        self._scope: SourceScope | None = None
        self._tokens = _NO_TOKENS

    @property
    def name(self) -> str:
        return self._name

    @property
    def attrs(self) -> Mapping[str, AttrValue]:
        return dict(self._attrs)

    @property
    def metric_labels(self) -> tuple[str, ...]:
        return self._labels

    def sum(self, name: str, value: float = 1.0, /) -> None:
        """Add to the counter ``<source>_<name>`` (source labels + static labels)."""
        self._metric("counter", name).labels(self._label_values).inc(value)

    def gauge(self, name: str, value: float, /) -> None:
        """Set the gauge ``<source>_<name>``."""
        self._metric("gauge", name).labels(self._label_values).set(value)

    def histogram(self, name: str, value: float, /) -> None:
        """Observe ``value`` in the histogram ``<source>_<name>``."""
        self._metric("histogram", name).labels(self._label_values).observe(value)

    def done(self) -> None:
        """Kept for symmetry with Go; leaving the scope restores the previous context."""

    def _metric(self, kind: str, name: str) -> Any:
        """The source aggregate ``kind`` named ``name`` (registered once per source)."""
        key = (kind, name)
        metric = self._metrics.get(key)
        if metric is None:
            metric = self._metrics[key] = self._bedrock._source_metric(
                kind, f"{self._name}_{name}", f"Aggregated {name} for {self._name}", self._labels
            )
        return metric

    def _activate(self) -> _Tokens:
        tokens = _common_tokens(self._bedrock, None, False)
        tokens.append(source_var.set(self))
        return tuple(tokens)

    def _close(self, exc: BaseException | None) -> None:
        """Sources record no outcome."""

    def __repr__(self) -> str:
        return f"<pyrock.Source {self._name!r}>"


# ------------------------------------------------------------------------- noops


_EMPTY_ACTIVATION = Activation(_NO_TOKENS)
_NO_ATTRS: Mapping[str, AttrValue] = MappingProxyType({})


class NoopOperation(Operation):
    """Does nothing; ``failed`` is always ``False``, IDs are ``None``."""

    __slots__ = ()

    def __init__(self) -> None:
        """Build the singleton (no state)."""

    @property
    def name(self) -> str:
        return ""

    @property
    def metric_name(self) -> str:
        return ""

    @property
    def span_context(self) -> SpanContext | None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None

    @property
    def span_id(self) -> str | None:
        return None

    @property
    def failed(self) -> bool:
        return False

    @property
    def ended(self) -> bool:
        return False

    def register(self, *items: RegisterItem, **attrs: AttrValue) -> None:
        pass

    def event(
        self, name: str, /, attrs: Mapping[str, AttrValue] | None = None, **kwargs: AttrValue
    ) -> None:
        pass

    def fail(self, exc: BaseException | None = None, /) -> None:
        pass

    def succeed(self) -> None:
        pass

    def end(self, *, error: BaseException | None = None, success: bool | None = None) -> None:
        pass

    def done(self) -> None:
        pass

    def activate(self) -> Activation:
        return _EMPTY_ACTIVATION

    def deactivate(self, activation: Activation, /) -> None:
        pass

    def _merge(self, attrs: Mapping[str, AttrValue]) -> None:
        pass

    def _activate(self) -> _Tokens:
        return _NO_TOKENS

    def _close(self, exc: BaseException | None) -> None:
        pass

    def __repr__(self) -> str:
        return "<pyrock.Operation noop>"


class NoopStep(Step):
    """Does nothing."""

    __slots__ = ()

    def __init__(self) -> None:
        """Build the singleton (no state)."""

    @property
    def name(self) -> str:
        return ""

    @property
    def span_context(self) -> SpanContext | None:
        return None

    def register(self, *items: RegisterItem, **attrs: AttrValue) -> None:
        pass

    def event(
        self, name: str, /, attrs: Mapping[str, AttrValue] | None = None, **kwargs: AttrValue
    ) -> None:
        pass

    def done(self) -> None:
        pass

    def _activate(self) -> _Tokens:
        return _NO_TOKENS

    def _close(self, exc: BaseException | None) -> None:
        pass

    def __repr__(self) -> str:
        return "<pyrock.Step noop>"


class NoopSource(Source):
    """Does nothing."""

    __slots__ = ()

    def __init__(self) -> None:
        """Build the singleton (no state)."""

    @property
    def name(self) -> str:
        return ""

    @property
    def attrs(self) -> Mapping[str, AttrValue]:
        return _NO_ATTRS

    @property
    def metric_labels(self) -> tuple[str, ...]:
        return ()

    def sum(self, name: str, value: float = 1.0, /) -> None:
        pass

    def gauge(self, name: str, value: float, /) -> None:
        pass

    def histogram(self, name: str, value: float, /) -> None:
        pass

    def _activate(self) -> _Tokens:
        return _NO_TOKENS

    def __repr__(self) -> str:
        return "<pyrock.Source noop>"


NOOP_OPERATION = NoopOperation()
NOOP_STEP = NoopStep()
NOOP_SOURCE = NoopSource()
"""Preallocated singletons that scopes enter as when no instance is active."""


# ------------------------------------------------------------------------ scopes


class _Scope(ABC, Generic[L]):
    """Shared context-manager/decorator protocol for scopes.

    Each ``with`` entry (or decorated call) starts a new live object; a scope
    may be reused, nested and shared between threads and tasks. On exit the
    live object is found through its context variable (the innermost one in
    this context is the one this scope entered).

    Decorating always returns a ``functools.wraps`` wrapper that resolves the
    instance at call time (explicit ``bedrock=`` pinned, else the active one),
    so decoration before ``init()`` works. The function is returned unchanged
    only when an explicit noop ``bedrock=`` was given.
    """

    __slots__ = ("_bedrock", "_disabled")

    _var: ClassVar[contextvars.ContextVar[Any]]

    def __init__(self, bedrock: Bedrock | None) -> None:
        self._disabled = bedrock is not None and bedrock.is_noop
        self._bedrock = None if self._disabled else bedrock

    @abstractmethod
    def _noop(self) -> L:
        """The preallocated noop live object."""

    @abstractmethod
    def _start(self, bedrock: Bedrock) -> L:
        """Create the live object for one entry (not yet current)."""

    def _instance(self) -> Bedrock | None:
        if self._disabled:
            return None
        bedrock = self._bedrock
        return bedrock_var.get() if bedrock is None else bedrock

    def _enter(self) -> L:
        bedrock = self._instance()
        if bedrock is None:
            return self._noop()
        live = self._start(bedrock)
        if live is not self._noop():  # a step may still turn out to be a noop
            live._scope = self  # type: ignore[assignment]
            live._tokens = live._activate()
        return live

    def __enter__(self) -> L:
        if self._bedrock is None and not self._disabled and bedrock_var.get() is None:
            return self._noop()  # fast path: nothing active
        return self._enter()

    async def __aenter__(self) -> L:
        return self._enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        live = self._var.get()
        if live is not None and live._scope is self:
            try:
                live._close(exc)
            finally:
                _restore(live._tokens)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    # -- decorating ------------------------------------------------------------

    def __call__(self, fn: F, /) -> F:
        if self._disabled:
            return fn
        if inspect.isasyncgenfunction(fn):
            wrapper: Callable[..., Any] = self._wrap_async_generator(fn)
        elif inspect.isgeneratorfunction(fn):
            wrapper = self._wrap_generator(fn)
        elif inspect.iscoroutinefunction(fn):
            wrapper = self._wrap_coroutine(fn)
        else:
            wrapper = self._wrap_function(fn)
        return cast("F", functools.wraps(fn)(wrapper))

    def _wrap_function(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            bedrock = self._instance()
            if bedrock is None:
                return fn(*args, **kwargs)
            live = self._start(bedrock)
            tokens = live._activate()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                live._close(exc)
                raise
            else:
                live._close(None)
            finally:
                _restore(tokens)
            return result

        return call

    def _wrap_coroutine(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        async def call(*args: Any, **kwargs: Any) -> Any:
            bedrock = self._instance()
            if bedrock is None:
                return await fn(*args, **kwargs)
            live = self._start(bedrock)
            tokens = live._activate()
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                live._close(exc)
                raise
            else:
                live._close(None)
            finally:
                _restore(tokens)
            return result

        return call

    def _wrap_generator(
        self, fn: Callable[..., Generator[Any, Any, Any]]
    ) -> Callable[..., Generator[Any, Any, Any]]:
        def call(*args: Any, **kwargs: Any) -> Generator[Any, Any, Any]:
            bedrock = self._instance()
            if bedrock is None:
                return (yield from fn(*args, **kwargs))
            live = self._start(bedrock)
            try:
                result = yield from _ActivatedGenerator(live, fn(*args, **kwargs))
            except BaseException as exc:
                live._close(exc)
                raise
            live._close(None)
            return result

        return call

    def _wrap_async_generator(
        self, fn: Callable[..., AsyncIterator[Any]]
    ) -> Callable[..., AsyncIterator[Any]]:
        async def call(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            bedrock = self._instance()
            live = self._noop() if bedrock is None else self._start(bedrock)
            agen: Any = fn(*args, **kwargs)
            # Forward asend/athrow/aclose by hand: ``async for`` would drop them.
            resume: Callable[[Any], Any] = agen.asend
            value: Any = None
            try:
                while True:
                    tokens = live._activate()
                    try:
                        item = await resume(value)
                    except StopAsyncIteration:
                        break
                    finally:
                        _restore(tokens)
                    try:
                        value = yield item
                    except GeneratorExit:
                        tokens = live._activate()
                        try:
                            await agen.aclose()
                        finally:
                            _restore(tokens)
                        raise
                    except BaseException as exc:
                        resume, value = agen.athrow, exc
                    else:
                        resume = agen.asend
            except BaseException as exc:
                live._close(exc)
                raise
            live._close(None)

        return call


class _ActivatedGenerator:
    """Drives a generator with ``live`` current only while the generator body runs."""

    __slots__ = ("_gen", "_live")

    def __init__(self, live: Operation | Step | Source, gen: Generator[Any, Any, Any]) -> None:
        self._live = live
        self._gen = gen

    def __iter__(self) -> _ActivatedGenerator:
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def send(self, value: Any) -> Any:
        tokens = self._live._activate()
        try:
            return self._gen.send(value)
        finally:
            _restore(tokens)

    def throw(self, *exc_info: Any) -> Any:
        tokens = self._live._activate()
        try:
            return self._gen.throw(*exc_info)
        finally:
            _restore(tokens)

    def close(self) -> None:
        tokens = self._live._activate()
        try:
            self._gen.close()
        finally:
            _restore(tokens)


class OperationScope(_Scope[Operation]):
    """Returned by :func:`operation`."""

    __slots__ = ("_attrs", "_expected", "_kind", "_labels", "_name", "_no_trace", "_remote")

    _var = operation_var

    def __init__(
        self,
        name: str,
        attrs: Mapping[str, AttrValue] | None,
        metric_labels: Iterable[str],
        no_trace: bool,
        remote_parent: SpanContext | None,
        kind: SpanKind,
        expected: ExpectedExceptions,
        bedrock: Bedrock | None,
        /,
    ) -> None:
        """Positional only: built on every ``pyrock.operation(...)`` call, so kept lean."""
        if bedrock is None:  # the common case, inlined from _Scope.__init__
            self._bedrock = None
            self._disabled = False
        else:
            _Scope.__init__(self, bedrock)
        self._name = name
        self._attrs = attrs
        self._labels = metric_labels if type(metric_labels) is tuple else tuple(metric_labels)
        self._no_trace = no_trace
        self._remote = remote_parent
        self._kind = kind
        self._expected = expected

    def _noop(self) -> Operation:
        return NOOP_OPERATION

    def _start(self, bedrock: Bedrock) -> Operation:
        return Operation(
            bedrock,
            self._name,
            attrs=self._attrs,
            metric_labels=self._labels,
            no_trace=self._no_trace,
            remote_parent=self._remote,
            kind=self._kind,
            expected=self._expected,
        )


class StepScope(_Scope[Step]):
    """Returned by :func:`step`."""

    __slots__ = ("_attrs", "_expected", "_name", "_no_trace")

    _var = step_var

    def __init__(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None,
        no_trace: bool,
        expected: ExpectedExceptions,
        bedrock: Bedrock | None,
    ) -> None:
        super().__init__(bedrock)
        self._name = name
        self._attrs = attrs
        self._no_trace = no_trace
        self._expected = expected

    def _noop(self) -> Step:
        return NOOP_STEP

    def _start(self, bedrock: Bedrock) -> Step:
        operation = operation_var.get()
        no_trace = self._no_trace or no_trace_var.get()
        if operation is None and (no_trace or current_span() is None):
            return NOOP_STEP  # nothing to propagate to and nothing to trace
        return Step(
            bedrock,
            self._name,
            operation=operation,
            attrs=self._attrs,
            no_trace=no_trace,
            expected=self._expected,
        )


class SourceScope(_Scope[Source]):
    """Returned by :func:`source`."""

    __slots__ = ("_attrs", "_labels", "_name")

    _var = source_var

    def __init__(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None,
        metric_labels: Iterable[str],
        bedrock: Bedrock | None,
    ) -> None:
        super().__init__(bedrock)
        self._name = name
        self._attrs = attrs
        self._labels = tuple(metric_labels)

    def _noop(self) -> Source:
        return NOOP_SOURCE

    def _start(self, bedrock: Bedrock) -> Source:
        return Source(bedrock, self._name, attrs=self._attrs, metric_labels=self._labels)


# --------------------------------------------------------------------- functions


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
    resolved from context when the scope is entered or the decorated function
    is called (noop if none). Duplicate names under the same parent operation
    or step are numbered ``name[1]``, ``name[2]``, ...; root operations and
    direct children of a source are never numbered.
    """
    return OperationScope(
        name, attrs, metric_labels, no_trace, remote_parent, kind, expected, bedrock
    )


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
    if bedrock is None:
        bedrock = bedrock_var.get()
    if bedrock is None or bedrock.is_noop:
        return NOOP_OPERATION
    return Operation(
        bedrock,
        name,
        attrs=attrs,
        metric_labels=tuple(metric_labels),
        no_trace=no_trace,
        remote_parent=remote_parent,
        kind=kind,
        expected=expected,
    )


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
    return StepScope(name, attrs=attrs, no_trace=no_trace, expected=expected, bedrock=bedrock)


def source(
    name: str,
    /,
    *,
    attrs: Mapping[str, AttrValue] | None = None,
    metric_labels: Iterable[str] = (),
    bedrock: Bedrock | None = None,
) -> SourceScope:
    """Open a source: child operations become ``<source>.<name>`` and share attrs/labels."""
    return SourceScope(name, attrs=attrs, metric_labels=metric_labels, bedrock=bedrock)
