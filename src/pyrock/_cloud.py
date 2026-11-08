"""The managed cloud backend (Go: ``WithCloud`` / ``wireCloud``).

.. code-block:: python

    pyrock.init(cloud=pyrock.cloud("brk_live_abc123", push_interval=15.0))

* Traces: OTLP/gRPC with ``x-bedrock-key`` metadata; a larger batch processor.
  The target is ``traces_endpoint`` when set, otherwise derived from
  ``endpoint``; either may be ``host:port`` or a URL, parsed by
  :func:`pyrock.trace.otlp.grpc.parse_target` (see :func:`grpc_endpoint`).
  The connection uses h2c when ``insecure`` is set or the target is an
  ``http://`` URL, TLS otherwise. Because the API key travels with every
  request, an ``http://`` ``endpoint`` or ``traces_endpoint`` is rejected
  unless ``insecure=True``.
* :class:`RetryExporter`: one initial attempt plus one retry per
  ``retry_backoff`` entry (default ``(1, 2, 4)`` s: 4 calls in all, as in Go),
  waiting on an event so shutdown interrupts the back-off at once.
* :class:`FanOutExporter`: with a local ``trace_url`` too, every batch goes to
  both; every exporter is always called, the first error is raised.
* Metrics push (:mod:`pyrock._push`): Prometheus text to
  ``{endpoint}/v1/metrics`` with ``Authorization: Bearer``; overlapping pushes
  are skipped; final flush on shutdown, bounded by the caller's timeout.
* Profile push: OTLP profiles to ``{endpoint}/v1/profiles`` through
  ``Bedrock.start_profile_export`` (so shutdown and fork handle it like any
  other profile exporter); ``profiles=()`` disables it.

``pyrock.cloud`` and ``pyrock.CloudConfig`` are re-exported (lazily) from this
private module. It is never imported by ``import pyrock``; the gRPC stack loads
only when wiring starts.
"""

from __future__ import annotations

import datetime
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pyrock._compat import ConfigError, is_nonempty_str, is_positive_number, seconds
from pyrock._transport import forward_fork, remaining
from pyrock.privacy import sanitize_url
from pyrock.profile import DEFAULT_PROFILE_TYPES, ProfileExportConfig, ProfileType
from pyrock.trace import Span, SpanExporter
from pyrock.trace.otlp import ExportError, PermanentExportError
from pyrock.trace.otlp.batch import BatchProcessor

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock
    from pyrock._push import MetricsPusher

__all__ = [
    "DEFAULT_ENDPOINT",
    "CloudConfig",
    "CloudWiring",
    "FanOutExporter",
    "RetryExporter",
    "cloud",
    "grpc_endpoint",
    "wire",
]

DEFAULT_ENDPOINT = "https://ingest.bedrock.dev"
API_KEY_HEADER = "x-bedrock-key"
DEFAULT_RETRY_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0)
MAX_RETRY_BACKOFF_ENTRIES = 10

CLOUD_MAX_QUEUE_SIZE = 10_000
"""Queue of the cloud batch processor (Go: 10000, against 2048 locally)."""
CLOUD_MAX_BATCH_SIZE = 512
CLOUD_SCHEDULE_DELAY = 5.0

_log = logging.getLogger("pyrock.cloud")


# -- configuration ------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CloudConfig:
    """Cloud settings. ``api_key`` is excluded from ``repr`` and equality.

    Durations are seconds (a :class:`datetime.timedelta` is converted).
    Construction validates every field and raises
    :class:`~pyrock.ConfigError` (a ``ValueError``) listing every problem.
    """

    api_key: str = field(repr=False, compare=False)
    endpoint: str = DEFAULT_ENDPOINT
    """Base ``http(s)`` URL for metrics and profile pushes (and traces, unless
    ``traces_endpoint``). ``http://`` requires ``insecure=True``."""
    insecure: bool = False
    """Plaintext h2c for traces and allow ``http://`` endpoints (local development only:
    the API key is then sent unencrypted)."""
    push_interval: float = 15.0
    profile_interval: float = 60.0
    profile_cpu_sample_duration: float = 10.0
    """Must be ``> 0`` and ``< profile_interval``."""
    profiles: tuple[ProfileType, ...] = DEFAULT_PROFILE_TYPES
    """``()`` disables cloud profiling."""
    retry_backoff: tuple[float, ...] = DEFAULT_RETRY_BACKOFF
    """Waits (seconds) before each trace-export retry; its length is the retry count.

    Each entry must be finite and ``>= 0``; at most 10 entries. ``()`` disables
    retries. Lists are accepted and stored as a tuple.
    """
    traces_endpoint: str | None = None
    """Optional gRPC trace target (``host:port`` or a URL, normalized like Go).

    ``None`` derives the target from :attr:`endpoint`. An ``http://`` URL
    requires ``insecure=True``.
    """

    def __post_init__(self) -> None:
        for name in ("push_interval", "profile_interval", "profile_cpu_sample_duration"):
            object.__setattr__(self, name, seconds(getattr(self, name)))
        problems = [
            *self._normalize_profiles(),
            *self._normalize_backoff(),
            *self._key_problems(),
            *self._endpoint_problems(),
            *self._interval_problems(),
        ]
        if problems:
            raise ConfigError(problems)

    def _normalize_profiles(self) -> list[str]:
        raw: object = self.profiles
        if isinstance(raw, str) or not isinstance(raw, Iterable):
            return ["profiles must be a sequence of ProfileType"]
        try:
            profiles = tuple(dict.fromkeys(ProfileType(p) for p in raw))
        except ValueError as exc:
            return [f"unknown profile type: {exc}"]
        object.__setattr__(self, "profiles", profiles)
        return []

    def _normalize_backoff(self) -> list[str]:
        raw: object = self.retry_backoff
        if isinstance(raw, str | bytes) or not isinstance(raw, Iterable):
            return ["retry_backoff must be a sequence of seconds"]
        values = tuple(seconds(b) for b in raw)
        object.__setattr__(self, "retry_backoff", values)
        problems: list[str] = []
        if len(values) > MAX_RETRY_BACKOFF_ENTRIES:
            problems.append(f"retry_backoff must have at most {MAX_RETRY_BACKOFF_ENTRIES} entries")
        if not all(_is_backoff(b) for b in values):
            problems.append("retry_backoff entries must be finite and >= 0")
        return problems

    def _key_problems(self) -> list[str]:
        if not is_nonempty_str(self.api_key):
            return ["api_key must be a non-empty string"]
        if any(c in self.api_key for c in "\r\n\0"):
            return ["api_key contains control characters"]
        return []

    def _endpoint_problems(self) -> list[str]:
        problems: list[str] = []
        if not is_nonempty_str(self.endpoint):
            problems.append("endpoint must be a non-empty string")
        else:
            problems += _url_problems("endpoint", self.endpoint, insecure=self.insecure)
        traces: object = self.traces_endpoint
        if traces is not None and not is_nonempty_str(traces):
            problems.append("traces_endpoint must be a non-empty string or None")
        elif isinstance(traces, str):
            problems += _target_problems("traces_endpoint", traces, insecure=self.insecure)
        return problems

    def _interval_problems(self) -> list[str]:
        problems: list[str] = []
        if not is_positive_number(self.push_interval):
            problems.append("push_interval must be positive and finite")
        if not is_positive_number(self.profile_interval):
            problems.append("profile_interval must be positive and finite")
        elif not (
            is_positive_number(self.profile_cpu_sample_duration)
            and self.profile_cpu_sample_duration < self.profile_interval
        ):
            problems.append("profile_cpu_sample_duration must be > 0 and < profile_interval")
        return problems


def _url_problems(name: str, url: str, *, insecure: bool) -> list[str]:
    """``endpoint`` is the base of the push URLs: an absolute ``http(s)`` URL."""
    shown = sanitize_url(url)
    try:
        parts = urlsplit(url)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        return [f"{name} must be an http(s) URL: {shown!r}"]
    if parts.username is not None or parts.password is not None:
        return [f"{name} must not contain credentials: {shown!r}"]
    if parts.scheme == "http" and not insecure:
        return [_plaintext_problem(name, shown)]
    return []


def _target_problems(name: str, target: str, *, insecure: bool) -> list[str]:
    """``traces_endpoint`` is a gRPC target: ``host[:port]`` or an ``http(s)`` URL."""
    from pyrock.trace.otlp.grpc import parse_target

    try:
        _, _, plaintext = parse_target(target)
    except ValueError as exc:
        return [f"{name}: {exc}"]
    if plaintext and not insecure:
        return [_plaintext_problem(name, sanitize_url(target))]
    return []


def _plaintext_problem(name: str, shown: str) -> str:
    return (
        f"{name} {shown!r} is plaintext http, which would send the API key unencrypted; "
        "use https or set insecure=True"
    )


def _is_backoff(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value >= 0


def cloud(
    api_key: str,
    /,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    insecure: bool = False,
    push_interval: float | datetime.timedelta = 15.0,
    profile_interval: float | datetime.timedelta = 60.0,
    profile_cpu_sample_duration: float | datetime.timedelta = 10.0,
    profiles: Iterable[ProfileType] = DEFAULT_PROFILE_TYPES,
    retry_backoff: Iterable[float | datetime.timedelta] = DEFAULT_RETRY_BACKOFF,
    traces_endpoint: str | None = None,
) -> CloudConfig:
    """Build a :class:`CloudConfig` for ``init(cloud=...)``; takes every field as a keyword."""
    return CloudConfig(
        api_key=api_key,
        endpoint=endpoint,
        insecure=insecure,
        push_interval=seconds(push_interval),
        profile_interval=seconds(profile_interval),
        profile_cpu_sample_duration=seconds(profile_cpu_sample_duration),
        profiles=tuple(profiles),
        retry_backoff=tuple(seconds(b) for b in retry_backoff),
        traces_endpoint=traces_endpoint,
    )


def grpc_endpoint(endpoint: str, /) -> str:
    """Normalize a gRPC target to ``host:port`` like Go's ``cloudGRPCEndpoint``.

    An ``http://`` or ``https://`` scheme (any case) is stripped along with
    any path, query or fragment, and port 4317 is added when none is given:
    ``https://ingest.bedrock.dev`` becomes ``ingest.bedrock.dev:4317``. A bare
    ``host:port`` is returned unchanged. IPv6 literals are written in brackets.
    Parsing is :func:`pyrock.trace.otlp.grpc.parse_target`'s, so a malformed
    target raises ``ValueError``.
    """
    from pyrock.trace.otlp.grpc import format_target, parse_target

    host, port, _ = parse_target(endpoint)
    return format_target(host, port)


# -- exporters ----------------------------------------------------------------


class RetryExporter:
    """Retries a failing exporter: at most ``1 + len(backoff)`` calls per batch.

    ``backoff[i]`` is the wait before retry ``i + 1``; the default
    ``(1, 2, 4)`` gives 4 calls in all, as in Go. ``()`` means one call and
    no retries. Only retryable :class:`~pyrock.trace.otlp.ExportError` s are
    retried; a :class:`~pyrock.trace.otlp.PermanentExportError` or any other
    exception is raised at once. Waits are on an event that :meth:`shutdown`
    sets, so shutdown interrupts a back-off immediately; a retry that would
    start after the export's ``timeout`` is not attempted.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        /,
        *,
        backoff: Sequence[float] = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        self._exporter = exporter
        self._backoff = tuple(backoff)
        self._stopped = threading.Event()

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        """Export ``spans``, retrying; raises the last error once retries are exhausted."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for attempt, delay in enumerate((*self._backoff, None), start=1):
            try:
                self._exporter.export(spans, timeout=remaining(deadline))
            except PermanentExportError:
                raise
            except ExportError as exc:
                if delay is None or not self._wait(delay, deadline):
                    _log.error("cloud trace export failed after %d attempt(s): %s", attempt, exc)
                    raise
                _log.warning(
                    "cloud trace export failed, retrying: attempt=%d backoff=%gs error=%s",
                    attempt,
                    delay,
                    exc,
                )
            else:
                return

    def _wait(self, delay: float, deadline: float | None) -> bool:
        """Back off for ``delay``; ``False`` if shut down or the deadline comes first."""
        left = remaining(deadline)
        if left is not None and left < delay:
            return False
        return not self._stopped.wait(delay)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Interrupt any back-off wait, then shut the wrapped exporter down."""
        self._stopped.set()
        self._exporter.shutdown(timeout=timeout)

    def _after_fork_in_child(self) -> None:
        """Fresh stop event (same state) and forward the hook to the wrapped exporter."""
        stopped = threading.Event()
        if self._stopped.is_set():
            stopped.set()
        self._stopped = stopped
        forward_fork(self._exporter)


class FanOutExporter:
    """Sends each batch to every exporter; raises the first error after calling all.

    The exporters run in order and share one ``timeout`` deadline, as Go's
    exporters share one context.
    """

    def __init__(self, *exporters: SpanExporter) -> None:
        self._exporters = exporters

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        """Export to every exporter; then raise the first error, if any."""
        _call_all(self._exporters, timeout, lambda e, t: e.export(spans, timeout=t))

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Shut every exporter down; then raise the first error, if any."""
        _call_all(self._exporters, timeout, lambda e, t: e.shutdown(timeout=t))

    def _after_fork_in_child(self) -> None:
        """Forward the fork hook to every exporter."""
        for exporter in self._exporters:
            forward_fork(exporter)


def _call_all(
    exporters: Iterable[SpanExporter],
    timeout: float | None,
    call: Callable[[SpanExporter, float | None], None],
) -> None:
    """``call(exporter, remaining)`` for each exporter, then raise the first error."""
    deadline = None if timeout is None else time.monotonic() + timeout
    first: Exception | None = None
    for exporter in exporters:
        try:
            call(exporter, remaining(deadline))
        except Exception as exc:  # every exporter must be called, whatever the others do
            if first is None:
                first = exc
    if first is not None:
        raise first


# -- wiring -------------------------------------------------------------------


class CloudWiring:
    """Handles to the cloud workers started by :func:`wire`.

    Only the metrics pusher lives here: the trace pipeline belongs to the
    tracer and the profile exporter to the :class:`~pyrock.Bedrock`, which
    stop and fork-reset them. The wiring also remembers the trace exporters
    so a forked child can give them the child's refreshed resource.
    """

    def __init__(
        self,
        pusher: MetricsPusher,
        /,
        *,
        bedrock: Bedrock | None = None,
        exporters: Sequence[object] = (),
    ) -> None:
        self._pusher = pusher
        self._bedrock = bedrock
        self._exporters = tuple(exporters)

    def stop(self, *, timeout: float | None = None) -> None:
        """Final metrics push and stop all cloud workers, bounded by ``timeout``."""
        self._pusher.stop(timeout=timeout)

    def _after_fork_in_child(self) -> None:
        """Restart the metrics push in a forked child (called by the core fork hook).

        The core rebuilds its tracer (and so the resource, with the child's
        ``process.pid``) before calling this; the trace exporters, which
        copied the parent's resource, are handed the new one.
        """
        self._pusher._after_fork_in_child()
        if self._bedrock is None:
            return
        resource = self._bedrock.tracer.resource
        for exporter in self._exporters:
            replace = getattr(exporter, "_replace_resource", None)
            if callable(replace):
                replace(resource)


def wire(bedrock: Bedrock, config: CloudConfig, /) -> CloudWiring:
    """Replace the batch processor and start metrics and profile push for ``bedrock``.

    Everything that can fail validation is built before anything starts, so a
    ``ValueError`` (for example a malformed endpoint) leaves ``bedrock``
    unchanged. A profile configuration the profiler rejects is logged and
    skipped, as in Go.
    """
    from pyrock._push import MetricsPusher

    exporter, leaves = _trace_exporter(bedrock, config)
    base = config.endpoint.rstrip("/")
    auth = {"Authorization": f"Bearer {config.api_key}"}
    pusher = MetricsPusher(
        bedrock.metrics,
        endpoint=f"{base}/v1/metrics",
        headers=auth,
        interval=config.push_interval,
        allow_http=config.insecure,
    )
    profiles = _profile_config(config, base, auth)
    bedrock.tracer.set_processor(
        BatchProcessor(
            exporter,
            max_queue_size=CLOUD_MAX_QUEUE_SIZE,
            max_batch_size=CLOUD_MAX_BATCH_SIZE,
            schedule_delay=CLOUD_SCHEDULE_DELAY,
        )
    )
    if profiles is not None:
        bedrock.start_profile_export(profiles)
    pusher.start()
    return CloudWiring(pusher, bedrock=bedrock, exporters=leaves)


def _trace_exporter(bedrock: Bedrock, config: CloudConfig) -> tuple[SpanExporter, list[object]]:
    """The retrying gRPC exporter, fanned out with the local one when there is one.

    Also returns the unwrapped exporters, whose resource a fork child refreshes.
    """
    from pyrock.trace.otlp.grpc import GRPCExporter

    grpc = GRPCExporter(
        config.traces_endpoint or config.endpoint,
        service=bedrock.config.service,
        resource=bedrock.tracer.resource,
        headers={API_KEY_HEADER: config.api_key},
        insecure=config.insecure,
    )
    cloud_exporter = RetryExporter(grpc, backoff=config.retry_backoff)
    processor = bedrock.tracer.processor
    if isinstance(processor, BatchProcessor):  # a local trace_url pipeline
        local = processor.exporter
        return FanOutExporter(local, cloud_exporter), [local, grpc]
    return cloud_exporter, [grpc]


def _profile_config(
    config: CloudConfig, base: str, headers: dict[str, str]
) -> ProfileExportConfig | None:
    """The cloud profile export settings; ``None`` when disabled or rejected."""
    if not config.profiles:
        return None
    try:
        return ProfileExportConfig(
            endpoint=f"{base}/v1/profiles",
            headers=headers,
            types=config.profiles,
            interval=config.profile_interval,
            cpu_sample_duration=config.profile_cpu_sample_duration,
            allow_http=config.insecure,
        )
    except ValueError as exc:  # its messages name settings, never header values
        _log.warning("cloud profile export disabled: %s", exc)
        return None
