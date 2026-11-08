"""The :class:`Bedrock` instance, :func:`init`, and the module-level helpers.

Every module-level helper resolves the active ``Bedrock`` from context (or the
noop singleton) and delegates to the method of the same name.

Integration surface for pyrock's own modules (WSGI/ASGI middleware, the HTTP
client, cloud wiring); private to the package:

* ``Bedrock.tracer`` / ``.metrics`` / ``.policy`` / ``.logger`` / ``.static_attrs``;
* ``tracer.processor`` / ``tracer.set_processor()`` to swap the export
  pipeline (the processor installed by ``init`` is a
  :class:`~pyrock.trace.otlp.batch.BatchProcessor` whose ``.exporter`` is the
  local ``trace_url`` exporter, or ``None`` without one);
* ``Bedrock.operation(..., kind=, remote_parent=)`` opens an operation that
  also makes the instance active for its duration;
* ``cloud=`` wiring is built by ``pyrock._cloud.wire(bedrock, config)``; the
  returned object's ``stop(timeout=)`` runs on shutdown and its
  ``_after_fork_in_child()`` (when defined) in a forked child, as does the
  processor's.

Fork safety (DESIGN §15): one ``os.register_at_fork`` hook serves every live
instance. In the child each instance resets its metric series, rebuilds its
own export pipeline (fresh queue, worker started lazily, ``process.pid``
refreshed in the resource), forwards ``_after_fork_in_child`` to any other
processor, restarts its profile exporters and never starts the observability
server (the parent owns the port).
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from pyrock import logs
from pyrock._compat import ConfigError
from pyrock._context import bedrock_var, operation_var, step_var
from pyrock._noop import NOOP_COUNTER, NOOP_GAUGE, NOOP_HISTOGRAM
from pyrock._operation import (
    NOOP_OPERATION_METRICS,
    Operation,
    OperationScope,
    RegisterItem,
    SourceScope,
    StepScope,
    _enclosing_context,
    _OperationMetrics,
    start_operation,
)
from pyrock.attr import AttrValue
from pyrock.attr import error as error_attrs
from pyrock.config import Config
from pyrock.metric import (
    Counter,
    Gauge,
    Histogram,
    MetricConflictError,
    MetricLimitError,
    MetricType,
    Registry,
    sanitize_name,
)
from pyrock.privacy import Policy
from pyrock.trace import (
    Span,
    SpanContext,
    SpanKind,
    Tracer,
)
from pyrock.trace import current_span as _current_span
from pyrock.trace.sampler import AlwaysOn, ParentBased, Ratio, Sampler

if TYPE_CHECKING:
    import asyncio

    from pyrock._cloud import CloudConfig, CloudWiring
    from pyrock.metric.runtime import RuntimeCollector
    from pyrock.profile import ProfileExportConfig, ProfileExporter, ProfileExportStats
    from pyrock.server import Server
    from pyrock.trace.http import HTTPPropagator
    from pyrock.trace.otlp.batch import BatchProcessor, BatchStats

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

_UNLABELLED_PREFIXES = ("host.", "process.", "container.", "k8s.", "cloud.")
"""Static keys that identify infrastructure: resource attributes, never metric labels (Go)."""

_HELPER_STACKLEVEL = 3
"""``stacklevel`` that attributes helper log records to the helper's caller."""

_propagator: HTTPPropagator | None = None
"""Built on first :func:`inject_headers` (keeps ``trace.http`` out of ``import pyrock``)."""

_METRIC_ERRORS = (MetricConflictError, MetricLimitError)
"""Registration errors that operations and sources log instead of raising."""

_CONFIG_FIELDS = frozenset(f.name for f in dataclasses.fields(Config))


def _resolve_config(config: Config | None, overrides: Mapping[str, Any]) -> Config:
    """``config or Config.from_env()`` with ``overrides`` applied and validated."""
    unknown = sorted(set(overrides) - _CONFIG_FIELDS)
    if unknown:
        raise TypeError(f"unknown pyrock config option(s): {', '.join(unknown)}")
    base = Config.from_env() if config is None else config
    return base.replace(**overrides) if overrides else base


def _sampler(config: Config) -> Sampler:
    if config.trace_sampler is not None:
        return config.trace_sampler
    if config.trace_sample_rate < 1.0:
        return ParentBased(Ratio(config.trace_sample_rate))
    return ParentBased(AlwaysOn())


def _uses_grpc(config: Config) -> bool:
    """Explicit protocol, else Go's rule: a scheme or a path means HTTP, ``host:port`` gRPC."""
    if config.trace_protocol is not None:
        return config.trace_protocol == "grpc"
    url = config.trace_url or ""
    return "://" not in url and "/" not in url


def _nothing() -> None:
    """The logging restore callback before logging is installed."""


def _metric_static_labels(static: Mapping[str, AttrValue], policy: Policy) -> dict[str, AttrValue]:
    """The static attrs that become constant metric labels (after the policy)."""
    return {
        key: value
        for key, value in policy.resource(static).items()
        if not key.startswith(_UNLABELLED_PREFIXES)
    }


def _static_problems(static: Mapping[str, AttrValue], labels: Iterable[str]) -> list[str]:
    """Problems with static attr keys, as ``ConfigError`` lines (never echoing values)."""
    keys: Iterable[object] = static  # callers may not respect the static type
    problems = [
        f"static_attrs keys must be non-empty strings, got {key!r}"
        for key in keys
        if not isinstance(key, str) or not key
    ]
    if problems:
        return problems
    for key in labels:
        label = sanitize_name(key)
        if not label or label[0].isdigit() or ":" in label:
            problems.append(f"static attr {key!r} is not a valid metric label name")
        elif label.startswith("__"):
            problems.append(f"static attr {key!r} is a reserved metric label name (leading '__')")
        elif label == "le":
            problems.append(f"static attr {key!r} is reserved: histograms use the 'le' label")
    return problems


def _after_fork_hook(target: object) -> None:
    """Call ``target._after_fork_in_child()`` when it defines one."""
    reset = getattr(target, "_after_fork_in_child", None)
    if callable(reset):
        reset()


class Bedrock:
    """One observability instance: tracer, metrics registry, logging, server, exporters.

    ``Bedrock(config)`` builds a **fully live** instance (Go: ``bedrock.New``):
    the constructor runs environment detection and starts exporters, the
    observability server and log installation according to config, but does
    not activate it; the caller must shut it down. :func:`init` builds and
    activates one.

    It is a sync and async context manager. Entering activates it (unless
    :func:`init` already did); exiting shuts it down and restores the context
    value that was active before, which deactivates it. Nested
    ``with pyrock.init()`` / ``testing.capture()`` blocks therefore restore the
    outer active instance on exit.
    """

    def __init__(
        self,
        config: Config | None = None,
        /,
        *,
        static_attrs: Mapping[str, AttrValue] | None = None,
        cloud: CloudConfig | None = None,
        _defer_logging: bool = False,
        **overrides: Any,
    ) -> None:
        """``overrides`` are ``Config`` fields applied on top of ``config or Config.from_env()``.

        Unknown names raise ``TypeError``; invalid values raise ``ConfigError``.
        ``_defer_logging`` (private, for ``testing.capture``) leaves the log
        installation to a later :meth:`_install_logging` call.
        """
        config = _resolve_config(config, overrides)
        self._config = config
        self._policy = config.data_policy if config.data_policy is not None else Policy()
        self._canonical_logs = config.log_canonical
        self._lock = threading.Lock()
        """Guards the lifecycle state (``_closed``, profile exporters); never held for I/O."""
        self._closed = False
        self._shutdown_done = threading.Event()
        """Set once the first :meth:`shutdown` has finished; concurrent callers wait on it."""
        self._activation: contextvars.Token[Bedrock | None] | None = None
        self._op_metrics: dict[tuple[str, tuple[str, ...]], _OperationMetrics] = {}
        self._metric_warned: set[str] = set()
        """Names whose metric registration failure was already logged."""
        self._loops: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()
        self._server: Server | None = None
        self._cloud: CloudWiring | None = None
        self._profilers: list[tuple[ProfileExportConfig, ProfileExporter]] = []
        self._restart_in_child: list[ProfileExportConfig] = []
        self._runtime: RuntimeCollector | None = None
        self._batch: BatchProcessor | None = None
        self._restore_logging: Callable[[], None] = _nothing
        self._logger = logging.getLogger(logs.LOGGER_NAME)

        detected: Mapping[str, AttrValue] = {}
        if not config.disable_environment_detection:
            from pyrock import _envdetect

            detected = _envdetect.detect().attrs
        self._static: dict[str, AttrValue] = {**detected, **(static_attrs or {})}

        # Everything up to the tracer holds no resources: validate and fail fast.
        const_labels = _metric_static_labels(self._static, self._policy)
        problems = _static_problems(self._static, const_labels)
        if problems:
            raise ConfigError(problems)
        try:
            self._registry = Registry(
                config.metric_prefix,
                buckets=config.metric_buckets,
                const_labels=const_labels,
                series_limit=config.metric_series_limit,
                family_limit=config.metric_family_limit,
                label_filter=self._policy.label_value,
            )
        except ValueError as exc:
            raise ConfigError([f"static_attrs: {exc}"]) from None
        self._sampler = _sampler(config)
        self._tracer = self._new_tracer()

        try:
            if config.runtime_metrics:
                from pyrock.metric.runtime import RuntimeCollector

                self._runtime = RuntimeCollector(self._registry)
            self._batch = self._new_batch_processor()
            self._tracer.set_processor(self._batch)
            if not _defer_logging:
                self._install_logging()
            if config.server_enabled:
                self._start_server()
            if config.profiles is not None:
                self.start_profile_export(config.profiles)
            if cloud is not None:
                from pyrock import _cloud

                self._cloud = _cloud.wire(self, cloud)
        except BaseException:
            self.shutdown(timeout=config.shutdown_timeout)
            raise
        with _instances_lock:
            _instances.add(self)

    # -- construction helpers ------------------------------------------------

    def _install_logging(self) -> None:
        """Install logging per ``config.log_install`` (once; undone by :meth:`shutdown`)."""
        config = self._config
        with self._lock:
            if self._closed or self._restore_logging is not _nothing:
                return
            self._restore_logging = logs._install(
                config.log_install,
                level=logs.parse_level(config.log_level),
                log_format=config.log_format,
                stream=config.log_output,
                service=config.service,
                static_attrs=self._static,
                policy=self._policy,
                add_source=config.log_add_source,
            )

    def _new_tracer(self) -> Tracer:
        return Tracer(
            service=self._config.service,
            resource=self._static,
            sampler=self._sampler,
            policy=self._policy,
        )

    def _new_batch_processor(self) -> BatchProcessor | None:
        """The local ``trace_url`` pipeline (``None`` without a URL)."""
        config = self._config
        url = config.trace_url
        if not url:
            return None
        from pyrock.trace.otlp.batch import BatchProcessor

        resource = self._tracer.resource
        if _uses_grpc(config):
            from pyrock.trace.otlp import grpc

            try:
                # One parser for every gRPC target: ``http://`` (or trace_insecure) is h2c.
                _, _, plaintext = grpc.parse_target(url, insecure=config.trace_insecure)
            except ValueError as exc:
                raise ConfigError([f"trace_url: {exc}"]) from None
            return BatchProcessor(
                grpc.GRPCExporter(
                    url, service=config.service, resource=resource, insecure=plaintext
                )
            )
        from pyrock.trace.otlp.http import HTTPExporter

        return BatchProcessor(HTTPExporter(url, service=config.service, resource=resource))

    def _start_server(self) -> None:
        from pyrock import server

        config = self._config
        if config.server_pprof and config.server_profile_handler is None:
            self._logger.warning(
                "server_pprof is set without server_profile_handler: no pprof endpoints served"
            )
        srv = server.Server(
            self._registry,
            server.Config(
                addr=config.server_addr,
                metrics=config.server_metrics,
                pprof=config.server_pprof,
                profile_handler=config.server_profile_handler,
                read_timeout=config.server_read_timeout,
                read_header_timeout=config.server_read_header_timeout,
                write_timeout=config.server_write_timeout,
                idle_timeout=config.server_idle_timeout,
                max_header_bytes=config.server_max_header_bytes,
                shutdown_timeout=config.shutdown_timeout,
                max_connections=config.server_max_connections,
            ),
        )
        try:
            srv.start()
        except OSError as exc:
            # As in Go: telemetry must not take the application down.
            self._logger.warning(
                "observability server could not listen on %s", config.server_addr, exc_info=exc
            )
            return
        self._server = srv

    # -- accessors ---------------------------------------------------------

    @property
    def config(self) -> Config:
        return self._config

    @property
    def static_attrs(self) -> Mapping[str, AttrValue]:
        """Detected resource attrs followed by user ``static_attrs`` (user values win).

        As in Go, these are *resource* attributes on exported spans, top-level
        fields in log records, and labels on metrics (except the keys that
        identify infrastructure: ``host.*``, ``process.*``, ``container.*``,
        ``k8s.*`` and ``cloud.*``). The privacy policy filters each surface.
        """
        return MappingProxyType(self._static)

    @property
    def policy(self) -> Policy:
        """The data policy every surface applies (``Config.data_policy`` or the defaults)."""
        return self._policy

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def metrics(self) -> Registry:
        return self._registry

    @property
    def logger(self) -> logging.Logger:
        """The ``pyrock`` logger used by the direct helpers."""
        return self._logger

    @property
    def is_noop(self) -> bool:
        return False

    @property
    def server_address(self) -> tuple[str, int] | None:
        """The observability server's bound ``(host, port)``, or ``None`` if not running."""
        srv = self._server
        return None if srv is None else srv.address

    @property
    def closed(self) -> bool:
        return self._closed

    def trace_export_stats(self) -> BatchStats:
        """Counters of the active batch processor (all zero without one)."""
        from pyrock.trace.otlp.batch import BatchProcessor, BatchStats

        processor = self._tracer.processor
        return processor.stats() if isinstance(processor, BatchProcessor) else BatchStats()

    def profile_stats(self) -> ProfileExportStats:
        """Exports, failures and skips summed over all profile exporters."""
        from pyrock.profile import ProfileExportStats

        with self._lock:
            exporters = [exporter for _, exporter in self._profilers]
        return sum((e.stats() for e in exporters), ProfileExportStats())

    def start_profile_export(self, config: ProfileExportConfig, /) -> ProfileExporter:
        """Start an extra profile exporter; it is also stopped by :meth:`shutdown`."""
        exporter = self._start_profiler(config)
        with self._lock:
            if not self._closed:
                self._profilers.append((config, exporter))
                return exporter
        exporter.stop()
        raise RuntimeError("this pyrock instance is shut down")

    def _start_profiler(self, config: ProfileExportConfig) -> ProfileExporter:
        from pyrock import profile

        return profile.start_export(
            config,
            service=self._config.service,
            resource=self._static,
            policy=self._policy,
            loops=lambda: list(self._loops),
        )

    # -- private API used by operations, steps and sources ----------------------

    def _metric_labels(self, labels: Iterable[str]) -> tuple[str, ...]:
        """``labels`` without the ones that are already static (constant) labels."""
        static = self._registry.const_labels
        return tuple(key for key in labels if sanitize_name(key) not in static)

    def _operation_metrics(self, name: str, labels: tuple[str, ...]) -> _OperationMetrics:
        """The four families of operation ``name`` (registered once per schema).

        Registration never fails the operation: a schema conflict (the same
        name with other ``metric_labels``) or the family limit is logged once
        per name, and the operation records into shared no-op metrics.
        """
        key = (name, labels)
        cached = self._op_metrics.get(key)
        if cached is not None:
            return cached
        declared = self._metric_labels(labels)
        reg = self._registry
        try:
            metrics = _OperationMetrics(
                declared,
                reg.counter(f"{name}_count", f"Total count of {name} operations", labels=declared),
                reg.counter(f"{name}_successes", f"Successful {name} operations", labels=declared),
                reg.counter(f"{name}_failures", f"Failed {name} operations", labels=declared),
                reg.histogram(
                    f"{name}_duration_ms",
                    f"Duration of {name} operations in milliseconds",
                    labels=declared,
                ),
            )
        except _METRIC_ERRORS as exc:
            self._metric_failed(name, exc)
            metrics = NOOP_OPERATION_METRICS
        # A concurrent first use may register twice; the registry returns the same families.
        return self._op_metrics.setdefault(key, metrics)

    def _source_counter(self, name: str, help: str, labels: Iterable[str]) -> Counter:
        metric = self._source_metric(MetricType.COUNTER, name, help, labels)
        return metric if isinstance(metric, Counter) else NOOP_COUNTER

    def _source_gauge(self, name: str, help: str, labels: Iterable[str]) -> Gauge:
        metric = self._source_metric(MetricType.GAUGE, name, help, labels)
        return metric if isinstance(metric, Gauge) else NOOP_GAUGE

    def _source_histogram(self, name: str, help: str, labels: Iterable[str]) -> Histogram:
        metric = self._source_metric(MetricType.HISTOGRAM, name, help, labels)
        return metric if isinstance(metric, Histogram) else NOOP_HISTOGRAM

    def _source_metric(
        self, kind: MetricType, name: str, help: str, labels: Iterable[str]
    ) -> Counter | Gauge | Histogram | None:
        """Register a source aggregate; ``None`` (logged once) if registration fails."""
        declared = self._metric_labels(labels)
        reg = self._registry
        try:
            if kind is MetricType.COUNTER:
                return reg.counter(name, help, labels=declared)
            if kind is MetricType.GAUGE:
                return reg.gauge(name, help, labels=declared)
            return reg.histogram(name, help, labels=declared)
        except _METRIC_ERRORS as exc:
            self._metric_failed(name, exc)
            return None

    def _metric_failed(self, name: str, exc: Exception) -> None:
        """Warn once per ``name`` that its metrics are not recorded."""
        with self._lock:
            if name in self._metric_warned:
                return
            self._metric_warned.add(name)
        self._logger.warning("pyrock: metrics for %r are not recorded: %s", name, exc, stacklevel=2)

    def _error_attrs(self, exc: BaseException) -> dict[str, AttrValue]:
        """``error.*`` attrs for ``exc`` with the policy's detail flags."""
        policy = self._policy
        return error_attrs(
            exc,
            allow_details=policy.allow_error_details,
            allow_source_paths=policy.allow_source_paths,
        )

    def _log(
        self, level: int | str, msg: str, items: Sequence[LogItem], attrs: Mapping[str, AttrValue]
    ) -> None:
        """Emit through the ``pyrock`` logger, attributed to the helper's caller."""
        levelno = logs.parse_level(level)
        logger = self._logger
        if not logger.isEnabledFor(levelno):
            return
        fields: dict[str, AttrValue] = {}
        exc: BaseException | None = None
        for item in items:
            if isinstance(item, BaseException):
                if exc is None:
                    exc = item
            else:
                fields.update(item)
        fields.update(attrs)
        logger.log(
            levelno,
            msg,
            exc_info=exc,
            extra={logs.ATTRS_KEY: fields},
            stacklevel=_HELPER_STACKLEVEL,
        )

    # -- activation and lifecycle -----------------------------------------

    def activate(self) -> contextvars.Token[Bedrock | None]:
        """Make this the active instance in the current context."""
        return bedrock_var.set(self)

    def deactivate(self, token: contextvars.Token[Bedrock | None], /) -> None:
        """Restore what was active before the matching :meth:`activate`."""
        bedrock_var.reset(token)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Flush spans, final metrics push, stop profiles and server, restore logging.

        Idempotent and thread-safe: concurrent callers wait (within their own
        ``timeout``) for the first to finish. ``timeout`` (default
        ``config.shutdown_timeout``) bounds the whole shutdown. The lifecycle
        lock is held only to take a snapshot, never while stopping components,
        so :meth:`profile_stats` and friends stay responsive meanwhile.
        """
        budget = self._config.shutdown_timeout if timeout is None else timeout
        deadline = time.monotonic() + budget

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        with self._lock:
            first = not self._closed
            self._closed = True
            done = self._shutdown_done
            server, self._server = self._server, None
            cloud = self._cloud
            profilers = list(self._profilers)
        if not first:
            done.wait(remaining())
            return
        try:
            # Each stopper takes ``timeout=``; together they share one deadline.
            stoppers: list[tuple[str, Callable[..., object]]] = []
            if server is not None:
                stoppers.append(("server", server.shutdown))
            if cloud is not None:
                stoppers.append(("cloud", cloud.stop))
            stoppers.extend(("profiles", exporter.stop) for _, exporter in profilers)
            stoppers.append(("traces", self._tracer.shutdown))
            for name, stop in stoppers:
                try:
                    stop(timeout=remaining())
                except Exception:  # one failing component must not block the others
                    self._logger.warning("pyrock shutdown: stopping %s failed", name, exc_info=True)
            if self._runtime is not None:
                self._runtime.close()
            self._restore_logging()
        finally:
            _atexit_instances.discard(self)
            with _instances_lock:
                _instances.discard(self)
            done.set()

    async def ashutdown(self, *, timeout: float | None = None) -> None:
        """:meth:`shutdown` run off the event loop (``asyncio.to_thread``)."""
        import asyncio

        await asyncio.to_thread(self.shutdown, timeout=timeout)

    def _enter(self) -> Bedrock:
        if self._activation is None:
            self._activation = self.activate()
        return self

    def _leave(self) -> None:
        """Restore the context value from before activation (best effort across contexts)."""
        token, self._activation = self._activation, None
        if token is None:
            return
        try:
            bedrock_var.reset(token)
        except ValueError:  # activated in another context: at least stop pointing at us
            if bedrock_var.get() is self:
                bedrock_var.set(None)

    def __enter__(self) -> Bedrock:
        return self._enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.shutdown()
        finally:
            self._leave()

    async def __aenter__(self) -> Bedrock:
        return self._enter()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self.ashutdown()
        finally:
            self._leave()

    # -- fork --------------------------------------------------------------

    def _before_fork(self) -> None:
        """Remember which profile exporters run, while their threads still exist."""
        self._restart_in_child = [cfg for cfg, e in self._profilers if e.running]

    def _after_fork_in_child(self) -> None:
        self._lock = threading.Lock()
        if not self._shutdown_done.is_set():  # its waiters' lock may be held
            self._shutdown_done = threading.Event()
        if self._closed:
            return
        self._server = None  # the parent owns the port; never serve from the child
        if "process.pid" in self._static:
            self._static["process.pid"] = os.getpid()
        processor = self._tracer.processor
        self._tracer = self._new_tracer()
        if processor is self._batch:
            self._batch = self._new_batch_processor()
            processor = self._batch
        else:
            _after_fork_hook(processor)
        self._tracer.set_processor(processor)
        self._registry.reset()
        self._metric_warned = set()
        if self._cloud is not None:
            _after_fork_hook(self._cloud)
        self._profilers = [(cfg, self._start_profiler(cfg)) for cfg in self._restart_in_child]

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
        """Open an operation recorded by this instance (active for the scope's duration)."""
        return OperationScope(
            name, attrs, metric_labels, no_trace, remote_parent, kind, expected, self
        )

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
        """Start an operation now without activating it (manual lifecycle; call ``end()``)."""
        return start_operation(
            name,
            attrs=attrs,
            metric_labels=metric_labels,
            no_trace=no_trace,
            remote_parent=remote_parent,
            kind=kind,
            expected=expected,
            bedrock=self,
        )

    def step(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None = None,
        no_trace: bool = False,
        expected: tuple[type[BaseException], ...] = (),
    ) -> StepScope:
        return StepScope(name, attrs=attrs, no_trace=no_trace, expected=expected, bedrock=self)

    def source(
        self,
        name: str,
        /,
        *,
        attrs: Mapping[str, AttrValue] | None = None,
        metric_labels: Iterable[str] = (),
    ) -> SourceScope:
        return SourceScope(name, attrs=attrs, metric_labels=metric_labels, bedrock=self)

    def counter(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Counter:
        """Get or create a counter (static labels are added to every series)."""
        return self._registry.counter(name, help, labels=labels)

    def gauge(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Gauge:
        return self._registry.gauge(name, help, labels=labels)

    def histogram(
        self,
        name: str,
        help: str = "",
        *,
        buckets: Sequence[float] | None = None,
        labels: Iterable[str] = (),
    ) -> Histogram:
        return self._registry.histogram(name, help, buckets=buckets, labels=labels)

    def log(self, level: int | str, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        self._log(level, msg, items, attrs)

    def debug(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        self._log(logging.DEBUG, msg, items, attrs)

    def info(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        self._log(logging.INFO, msg, items, attrs)

    def warning(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        self._log(logging.WARNING, msg, items, attrs)

    def warn(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        """Alias of :meth:`warning`."""
        self._log(logging.WARNING, msg, items, attrs)

    def error(self, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
        self._log(logging.ERROR, msg, items, attrs)

    def track_loop(self, loop: asyncio.AbstractEventLoop | None = None, /) -> None:
        """Include ``loop`` (default: the running loop) in asyncio task metrics/profiles."""
        if loop is None:
            import asyncio

            loop = asyncio.get_running_loop()
        self._loops.add(loop)
        if self._runtime is not None:
            self._runtime.track_loop(loop)

    def __repr__(self) -> str:
        state = "closed" if self._closed else "live"
        return f"<pyrock.Bedrock service={self._config.service!r} {state}>"


class _NoopBedrock(Bedrock):
    """What :func:`current` returns when nothing is active: records and starts nothing.

    Its scopes enter as the noop singletons and, used as decorators, return
    the function unchanged.
    """

    def __init__(self) -> None:
        """Build the singleton: only the read-only parts, nothing that runs."""
        self._config = Config.default()
        self._policy = Policy()
        self._static = {}
        self._closed = False
        self._shutdown_done = threading.Event()
        self._activation = None
        self._registry = Registry()
        self._tracer = Tracer(service=self._config.service, policy=self._policy)
        self._logger = logging.getLogger(logs.LOGGER_NAME)

    @property
    def is_noop(self) -> bool:
        return True

    @property
    def server_address(self) -> tuple[str, int] | None:
        return None

    def trace_export_stats(self) -> BatchStats:
        from pyrock.trace.otlp.batch import BatchStats

        return BatchStats()

    def profile_stats(self) -> ProfileExportStats:
        from pyrock.profile import ProfileExportStats

        return ProfileExportStats()

    def start_profile_export(self, config: ProfileExportConfig, /) -> ProfileExporter:
        raise RuntimeError("no active pyrock instance: call pyrock.init() first")

    def counter(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Counter:
        return NOOP_COUNTER

    def gauge(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Gauge:
        return NOOP_GAUGE

    def histogram(
        self,
        name: str,
        help: str = "",
        *,
        buckets: Sequence[float] | None = None,
        labels: Iterable[str] = (),
    ) -> Histogram:
        return NOOP_HISTOGRAM

    def _log(
        self, level: int | str, msg: str, items: Sequence[LogItem], attrs: Mapping[str, AttrValue]
    ) -> None:
        pass

    def activate(self) -> contextvars.Token[Bedrock | None]:
        """Activating the noop deactivates whatever was active."""
        return bedrock_var.set(None)

    def shutdown(self, *, timeout: float | None = None) -> None:
        pass

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._leave()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._leave()

    def track_loop(self, loop: asyncio.AbstractEventLoop | None = None, /) -> None:
        pass

    def __repr__(self) -> str:
        return "<pyrock.Bedrock noop>"


_NOOP = _NoopBedrock()


# --------------------------------------------------------------------------- fork

_instances: weakref.WeakSet[Bedrock] = weakref.WeakSet()
"""Live (not shut down) instances, for the fork hook. Guarded by ``_instances_lock``."""
_instances_lock = threading.Lock()


def _fork_before() -> None:
    _instances_lock.acquire()
    for bedrock in list(_instances):
        bedrock._before_fork()


def _fork_after_in_parent() -> None:
    _instances_lock.release()


def _fork_after_in_child() -> None:
    try:
        # Profiling state first: restarted profile exporters must see fresh locks.
        profile = sys.modules.get("pyrock.profile")  # never import profiling for a fork
        reset = getattr(profile, "_after_fork_in_child", None)
        if callable(reset):
            reset()
        _atexit_instances._after_fork_in_child()
        for bedrock in list(_instances):
            bedrock._after_fork_in_child()
    finally:
        _instances_lock.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_fork_before,
        after_in_parent=_fork_after_in_parent,
        after_in_child=_fork_after_in_child,
    )


# ----------------------------------------------------------------------- atexit


class _AtExit:
    """Instances built with ``shutdown_atexit=True`` that are not shut down yet.

    One process-wide ``atexit`` hook serves them all. Shutting an instance
    down removes it, so nothing stays registered for it (or keeps it alive).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._instances: dict[int, Bedrock] = {}
        self._registered = False

    def add(self, bedrock: Bedrock) -> None:
        with self._lock:
            if not self._registered:
                import atexit

                atexit.register(self._run)
                self._registered = True
            self._instances[id(bedrock)] = bedrock

    def discard(self, bedrock: Bedrock) -> None:
        with self._lock:
            self._instances.pop(id(bedrock), None)

    def _after_fork_in_child(self) -> None:
        self._lock = threading.Lock()

    def _run(self) -> None:
        with self._lock:
            pending = list(self._instances.values())
        for bedrock in pending:
            bedrock.shutdown()


_atexit_instances = _AtExit()


# ----------------------------------------------------------------- module API


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
    5. register with the fork hook.

    ``shutdown_atexit=True`` opts into an ``atexit`` shutdown (off by default).

    Exiting ``with pyrock.init(...)`` shuts the instance down and restores the
    previously active context value (the outer instance, or none), which
    deactivates this one. There is no process-wide default: threads started
    with plain ``threading.Thread`` see the noop unless context is copied (or
    the interpreter inherits it, as free-threaded 3.14 does).
    """
    bedrock = Bedrock(config, static_attrs=static_attrs, cloud=cloud, **overrides)
    if activate:
        bedrock._activation = bedrock.activate()
    if shutdown_atexit:
        _atexit_instances.add(bedrock)
    return bedrock


def current() -> Bedrock:
    """Return the active ``Bedrock``, or the noop singleton.

    An active instance that has been shut down counts as inactive: after
    ``b.shutdown()`` without ``with``, everything behaves as before ``init()``.
    """
    bedrock = bedrock_var.get()
    return _NOOP if bedrock is None or bedrock._closed else bedrock


def current_operation() -> Operation | None:
    """Return the innermost active operation, or ``None``."""
    return operation_var.get()


def current_span() -> Span | None:
    """Return the innermost live span (operation or step), or ``None``."""
    return _current_span()


def current_span_context() -> SpanContext | None:
    """Return the innermost span's context (including a remote parent), or ``None``."""
    return _enclosing_context()


def register(*items: RegisterItem, **attrs: AttrValue) -> None:
    """Register on the innermost step or operation (noop when there is none)."""
    target = step_var.get() or operation_var.get()
    if target is not None:
        target.register(*items, **attrs)


def log(level: int | str, msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    """Log through the ``pyrock`` logger with structured attrs (Go: ``bedrock.Log``)."""
    current()._log(level, msg, items, attrs)


def debug(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    current()._log(logging.DEBUG, msg, items, attrs)


def info(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    current()._log(logging.INFO, msg, items, attrs)


def warning(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    current()._log(logging.WARNING, msg, items, attrs)


def warn(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    """Alias of :func:`warning`."""
    current()._log(logging.WARNING, msg, items, attrs)


def error(msg: str, /, *items: LogItem, **attrs: AttrValue) -> None:
    """Log at ERROR; a positional exception becomes ``error.*`` fields."""
    current()._log(logging.ERROR, msg, items, attrs)


def counter(name: str, help: str = "", *, labels: Iterable[str] = ()) -> Counter:
    """Get or create a counter on the active registry (static labels added)."""
    return current().counter(name, help, labels=labels)


def gauge(name: str, help: str = "", *, labels: Iterable[str] = ()) -> Gauge:
    """Get or create a gauge on the active registry (static labels added)."""
    return current().gauge(name, help, labels=labels)


def histogram(
    name: str,
    help: str = "",
    *,
    buckets: Sequence[float] | None = None,
    labels: Iterable[str] = (),
) -> Histogram:
    """Get or create a histogram on the active registry (static labels added)."""
    return current().histogram(name, help, buckets=buckets, labels=labels)


def inject_headers(headers: H, /, span_context: SpanContext | None = None) -> H:
    """Write ``traceparent``/``tracestate`` into ``headers`` and return it.

    Uses :func:`current_span_context` unless ``span_context`` is given; with
    neither, ``headers`` is returned unchanged.
    """
    global _propagator
    context = current_span_context() if span_context is None else span_context
    if context is not None:
        propagator = _propagator
        if propagator is None:
            from pyrock.trace.http import HTTPPropagator

            propagator = _propagator = HTTPPropagator()
        propagator.inject(headers, context)
    return headers


def track_loop(loop: asyncio.AbstractEventLoop | None = None, /) -> None:
    """Track ``loop`` (default: the running loop) on the active instance."""
    current().track_loop(loop)
