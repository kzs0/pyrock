"""Metrics: a thread-safe registry of counters, gauges and histograms.

Rules (same as Go):

* Names and label keys are sanitized to ``[a-zA-Z_:][a-zA-Z0-9_:]*``
  (``http.method`` -> ``http_method``); the registry prefix is prepended.
* Constant (static) labels are added to every series automatically.
* ``.labels(...)`` with an undeclared key raises ``ValueError``; a declared
  key that is not given renders as ``"_"``.
* Re-registering a name with a different type or label set raises
  :class:`MetricConflictError`; the same schema returns the existing metric.
* Past ``series_limit`` new series are dropped and counted
  (:meth:`Registry.dropped_series`); past ``family_limit`` registration raises
  :class:`MetricLimitError`.

Import rules: ``pyrock.metric`` imports only ``pyrock.attr`` and
``pyrock.privacy`` from pyrock.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol, TypeVar, runtime_checkable

from pyrock.attr import AttrValue

__all__ = [
    "DEFAULT_BUCKETS",
    "Bucket",
    "Collector",
    "Counter",
    "CounterChild",
    "Gauge",
    "GaugeChild",
    "Histogram",
    "HistogramChild",
    "MetricConflictError",
    "MetricFamily",
    "MetricLimitError",
    "MetricType",
    "Registry",
    "Sample",
    "Timer",
    "sanitize_name",
]

F = TypeVar("F", bound=Callable[..., object])

DEFAULT_BUCKETS: tuple[float, ...] = (0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000)
"""Default histogram buckets (milliseconds), identical to Go."""

LabelArg = Mapping[str, AttrValue] | None


class MetricConflictError(ValueError):
    """A metric name was re-registered with a different type, help or label set."""


class MetricLimitError(RuntimeError):
    """Registering another metric family would exceed ``family_limit``."""


class MetricType(enum.StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class Bucket:
    """A cumulative histogram bucket."""

    upper_bound: float
    count: int


@dataclass(frozen=True, slots=True)
class Sample:
    """One series in a snapshot. ``value`` for counters/gauges; buckets/count/sum for histograms."""

    labels: Mapping[str, str]
    value: float = 0.0
    buckets: tuple[Bucket, ...] = ()
    count: int = 0
    sum: float = 0.0


@dataclass(frozen=True, slots=True)
class MetricFamily:
    """A snapshot of all series of one metric."""

    name: str
    help: str
    type: MetricType
    samples: tuple[Sample, ...] = field(default_factory=tuple)


@runtime_checkable
class Collector(Protocol):
    """Called before each snapshot to refresh metrics it owns (Go: ``metric.Collector``)."""

    def collect(self) -> None: ...


def sanitize_name(name: str, /) -> str:
    """Replace every character outside ``[a-zA-Z0-9_:]`` with ``_``."""
    raise NotImplementedError


class Timer:
    """Times a block or function and observes the elapsed **milliseconds**.

    Works as a context manager and as a decorator (sync and async functions).
    """

    def __enter__(self) -> Timer:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    def __call__(self, fn: F, /) -> F:
        raise NotImplementedError


class CounterChild:
    """A counter bound to one label set (cached by the parent)."""

    def inc(self, amount: float = 1.0, /) -> None:
        """Add ``amount`` (must be ``>= 0``; ``ValueError`` otherwise)."""
        raise NotImplementedError

    def add(self, amount: float, /) -> None:
        """Alias of :meth:`inc` (Go: ``Add``)."""
        raise NotImplementedError


class GaugeChild:
    """A gauge bound to one label set."""

    def set(self, value: float, /) -> None:
        raise NotImplementedError

    def inc(self, amount: float = 1.0, /) -> None:
        raise NotImplementedError

    def dec(self, amount: float = 1.0, /) -> None:
        raise NotImplementedError

    def add(self, amount: float, /) -> None:
        raise NotImplementedError

    def sub(self, amount: float, /) -> None:
        raise NotImplementedError


class HistogramChild:
    """A histogram bound to one label set."""

    def observe(self, value: float, /) -> None:
        raise NotImplementedError

    def time(self) -> Timer:
        """Return a :class:`Timer` that observes elapsed milliseconds."""
        raise NotImplementedError


class _Metric:
    """Common accessors for registered metrics."""

    @property
    def name(self) -> str:
        """The full (prefixed, sanitized) metric name."""
        raise NotImplementedError

    @property
    def help(self) -> str:
        raise NotImplementedError

    @property
    def label_names(self) -> tuple[str, ...]:
        """Declared (sanitized) label names, excluding constant labels."""
        raise NotImplementedError


class Counter(_Metric, CounterChild):
    """A monotonically increasing counter. ``inc()`` without labels uses ``"_"`` for each."""

    def labels(self, mapping: LabelArg = None, /, **labels: AttrValue) -> CounterChild:
        """Bind label values (``.labels(method="GET")`` or ``.labels({"http.method": "GET"})``)."""
        raise NotImplementedError


class Gauge(_Metric, GaugeChild):
    """A value that can go up and down."""

    def labels(self, mapping: LabelArg = None, /, **labels: AttrValue) -> GaugeChild:
        raise NotImplementedError


class Histogram(_Metric, HistogramChild):
    """A bucketed distribution (bucket arrays + ``bisect``)."""

    @property
    def buckets(self) -> tuple[float, ...]:
        raise NotImplementedError

    def labels(self, mapping: LabelArg = None, /, **labels: AttrValue) -> HistogramChild:
        raise NotImplementedError


class Registry:
    """Thread-safe metric registry (Go: ``metric.Registry``)."""

    def __init__(
        self,
        prefix: str = "",
        *,
        buckets: Sequence[float] | None = None,
        const_labels: Mapping[str, AttrValue] | None = None,
        series_limit: int = 1000,
        family_limit: int = 1000,
        label_filter: Callable[[str, str], str] | None = None,
    ) -> None:
        """``label_filter(key, value) -> value`` applies the privacy policy to label values."""
        raise NotImplementedError

    @property
    def prefix(self) -> str:
        raise NotImplementedError

    @property
    def default_buckets(self) -> tuple[float, ...]:
        raise NotImplementedError

    @property
    def const_labels(self) -> Mapping[str, str]:
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
        """``buckets=None`` uses the registry default; they must be finite and increasing."""
        raise NotImplementedError

    def get(self, name: str, /) -> Counter | Gauge | Histogram | None:
        """Look up a registered metric by full or unprefixed name."""
        raise NotImplementedError

    def register_collector(self, collector: Collector, /) -> None:
        raise NotImplementedError

    def unregister_collector(self, collector: Collector, /) -> None:
        raise NotImplementedError

    def collect(self) -> list[MetricFamily]:
        """Run collectors, then return a consistent snapshot sorted by name (Go: ``Gather``)."""
        raise NotImplementedError

    def dropped_series(self) -> int:
        """Series dropped because ``series_limit`` was reached."""
        raise NotImplementedError

    def reset(self) -> None:
        """Zero every series, keeping registrations (used in a forked child)."""
        raise NotImplementedError
