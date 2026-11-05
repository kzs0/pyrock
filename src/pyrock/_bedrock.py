"""The :class:`Bedrock` instance, :func:`init`, and the module-level helpers.

Every module-level helper resolves the active ``Bedrock`` from context (or the
noop singleton) and delegates to the method of the same name.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from pyrock.attr import AttrValue
from pyrock.config import Config
from pyrock.metric import Counter, Gauge, Histogram, Registry
from pyrock.operation import (
    Operation,
    OperationScope,
    RegisterItem,
    SourceScope,
    StepScope,
)
from pyrock.trace import Span, SpanContext, SpanKind, Tracer
from pyrock.trace.otlp.batch import BatchStats

if TYPE_CHECKING:
    import asyncio

    from pyrock.cloud import CloudConfig
    from pyrock.profile import ProfileExportConfig, ProfileExporter, ProfileExportStats

__all__ = [
    "Bedrock",
    "LogItem",
    "counter",
    "current",
    "current_operation",
    "current_span",
    "current_span_context",
    "debug",
    "error",
    "gauge",
    "histogram",
    "info",
    "init",
    "inject_headers",
    "log",
    "register",
    "track_loop",
    "warn",
    "warning",
]

H = TypeVar("H", bound=MutableMapping[str, str])

LogItem = Mapping[str, AttrValue] | BaseException
"""Positional extras accepted by the log helpers: attribute mappings or an exception."""


class Bedrock:
    """One observability instance: tracer, metrics registry, logging, server, exporters.

    ``Bedrock(config)`` builds an instance without activating it (Go:
    ``bedrock.New``); :func:`init` builds and activates one. It is a sync and
    async context manager that shuts down on exit.
    """

    def __init__(
        self,
        config: Config | None = None,
        /,
        *,
        static_attrs: Mapping[str, AttrValue] | None = None,
        cloud: CloudConfig | None = None,
        **overrides: Any,
    ) -> None:
        """``overrides`` are ``Config`` fields applied on top of ``config or Config.from_env()``.

        Unknown names raise ``TypeError``; invalid values raise ``ConfigError``.
        """
        raise NotImplementedError

    # -- accessors ---------------------------------------------------------

    @property
    def config(self) -> Config:
        raise NotImplementedError

    @property
    def static_attrs(self) -> Mapping[str, AttrValue]:
        """Detected resource attrs followed by user ``static_attrs`` (user values win)."""
        raise NotImplementedError

    @property
    def tracer(self) -> Tracer:
        raise NotImplementedError

    @property
    def metrics(self) -> Registry:
        raise NotImplementedError

    @property
    def logger(self) -> logging.Logger:
        """The ``pyrock`` logger used by the direct helpers."""
        raise NotImplementedError

    @property
    def is_noop(self) -> bool:
        raise NotImplementedError

    @property
    def server_address(self) -> tuple[str, int] | None:
        """The observability server's bound ``(host, port)``, or ``None`` if not running."""
        raise NotImplementedError

    @property
    def closed(self) -> bool:
        raise NotImplementedError

    def trace_export_stats(self) -> BatchStats:
        raise NotImplementedError

    def profile_stats(self) -> ProfileExportStats:
        """Exports, failures and skips summed over all profile exporters."""
        raise NotImplementedError

    def start_profile_export(self, config: ProfileExportConfig, /) -> ProfileExporter:
        """Start an extra profile exporter; it is also stopped by :meth:`shutdown`."""
        raise NotImplementedError

    # -- activation and lifecycle -----------------------------------------

    def activate(self) -> contextvars.Token[Bedrock | None]:
        """Make this the active instance in the current context."""
        raise NotImplementedError

    def deactivate(self, token: contextvars.Token[Bedrock | None], /) -> None:
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Flush spans, final metrics push, stop profiles and server, restore logging.

        Idempotent and thread-safe. ``timeout`` defaults to ``config.shutdown_timeout``.
        """
        raise NotImplementedError

    async def ashutdown(self, *, timeout: float | None = None) -> None:
        """:meth:`shutdown` run off the event loop (``asyncio.to_thread``)."""
        raise NotImplementedError

    def __enter__(self) -> Bedrock:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> Bedrock:
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    # -- method versions of the module API --------------------------------

    def operation(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None = None,
        metric_labels: Iterable[str] = (),
        no_trace: bool = False,
        remote_parent: SpanContext | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        expected: tuple[type[BaseException], ...] = (),
    ) -> OperationScope:
        raise NotImplementedError

    def start_operation(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None = None,
        metric_labels: Iterable[str] = (),
        no_trace: bool = False,
        remote_parent: SpanContext | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        expected: tuple[type[BaseException], ...] = (),
    ) -> Operation:
        raise NotImplementedError

    def step(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None = None,
        no_trace: bool = False,
        expected: tuple[type[BaseException], ...] = (),
    ) -> StepScope:
        raise NotImplementedError

    def source(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None = None,
        metric_labels: Iterable[str] = (),
    ) -> SourceScope:
        raise NotImplementedError

    def counter(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Counter:
        raise NotImplementedError

    def gauge(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Gauge:
        raise NotImplementedError

    def histogram(
        self,
        name: str,
        help: str = "",
        *,
        buckets: Sequence[float] | None = None,
        labels: Iterable[str] = (),
    ) -> Histogram:
        raise NotImplementedError

    def log(self, level: int | str, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        raise NotImplementedError

    def debug(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        raise NotImplementedError

    def info(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        raise NotImplementedError

    def warning(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        raise NotImplementedError

    def warn(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        """Alias of :meth:`warning`."""
        raise NotImplementedError

    def error(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        raise NotImplementedError

    def track_loop(self, loop: asyncio.AbstractEventLoop | None = None, /) -> None:
        """Include ``loop`` (default: the running loop) in asyncio task metrics/profiles."""
        raise NotImplementedError


def init(
    config: Config | None = None,
    /,
    *,
    static_attrs: Mapping[str, AttrValue] | None = None,
    cloud: CloudConfig | None = None,
    activate: bool = True,
    shutdown_atexit: bool = False,
    **overrides: Any,
) -> Bedrock:
    """Build, start and (by default) activate a :class:`Bedrock`.

    1. config = ``config or Config.from_env()`` with ``overrides`` applied and
       validated (unknown names: ``TypeError``; invalid: ``ConfigError``);
    2. environment detection (unless disabled), placed before ``static_attrs``;
    3. tracer, registry, logging, server, profile exporter and cloud wiring;
    4. with ``activate``, set as the active instance in the current context;
    5. register the fork hook.

    ``shutdown_atexit=True`` opts into an ``atexit`` shutdown (off by default).
    """
    raise NotImplementedError


def current() -> Bedrock:
    """Return the active ``Bedrock``, or the noop singleton."""
    raise NotImplementedError


def current_operation() -> Operation | None:
    """Return the innermost active operation, or ``None``."""
    raise NotImplementedError


def current_span() -> Span | None:
    """Return the innermost live span (operation or step), or ``None``."""
    raise NotImplementedError


def current_span_context() -> SpanContext | None:
    """Return the innermost span's context (including a remote parent), or ``None``."""
    raise NotImplementedError


def register(*items: RegisterItem, **attrs: AttrValue) -> None:
    """Register on the innermost step or operation (noop when there is none)."""
    raise NotImplementedError


def log(level: int | str, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    """Log through the ``pyrock`` logger with structured attrs (Go: ``bedrock.Log``)."""
    raise NotImplementedError


def debug(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    raise NotImplementedError


def info(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    raise NotImplementedError


def warning(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    raise NotImplementedError


def warn(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    """Alias of :func:`warning`."""
    raise NotImplementedError


def error(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    """Log at ERROR; a positional exception becomes ``error.*`` fields."""
    raise NotImplementedError


def counter(name: str, help: str = "", *, labels: Iterable[str] = ()) -> Counter:
    """Get or create a counter on the active registry (static labels added)."""
    raise NotImplementedError


def gauge(name: str, help: str = "", *, labels: Iterable[str] = ()) -> Gauge:
    raise NotImplementedError


def histogram(
    name: str,
    help: str = "",
    *,
    buckets: Sequence[float] | None = None,
    labels: Iterable[str] = (),
) -> Histogram:
    raise NotImplementedError


def inject_headers(headers: H, /, span_context: SpanContext | None = None) -> H:
    """Write ``traceparent``/``tracestate`` into ``headers`` and return it."""
    raise NotImplementedError


def track_loop(loop: asyncio.AbstractEventLoop | None = None, /) -> None:
    """Track ``loop`` (default: the running loop) on the active instance."""
    raise NotImplementedError
