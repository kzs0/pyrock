"""Metrics: a thread-safe registry of counters, gauges and histograms.

Rules (same as Go):

* Names and label keys are sanitized to ``[a-zA-Z_:][a-zA-Z0-9_:]*``
  (``http.method`` -> ``http_method``); the registry prefix is prepended.
* Constant (static) labels are added to every series automatically.
* ``.labels(...)`` with an undeclared key raises ``ValueError``; a declared
  key that is not given renders as ``"_"``.
* Re-registering a name with a different type or label set (or explicit,
  different histogram buckets) raises :class:`MetricConflictError`; the same
  schema returns the existing metric. A different ``help`` is not a conflict:
  the first registration wins.
* Past ``series_limit`` new series are dropped and counted
  (:meth:`Registry.dropped_series`); past ``family_limit`` registration raises
  :class:`MetricLimitError`.

Concurrency: every series owns a ``threading.Lock`` that guards only its own
numbers, so totals are exact on free-threaded builds. Bound children are
cached per metric, so ``.labels(...)`` on a known label set is one dict
lookup. No lock is held while user code (label filters, collectors) runs.

Import rules: ``pyrock.metric`` imports only ``pyrock.attr`` and
``pyrock.privacy`` from pyrock.
"""

from __future__ import annotations

import enum
import functools
import inspect
import itertools
import math
import re
import threading
import time
from abc import ABC, abstractmethod
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from operator import attrgetter
from types import MappingProxyType, TracebackType
from typing import Any, Generic, NoReturn, Protocol, TypeVar, cast, runtime_checkable

from pyrock.attr import MISSING_LABEL, AttrValue, _text_value

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

_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_:]")
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_RAW_CACHE_FACTOR = 4
"""Bound children cached per metric, as a multiple of ``series_limit``."""


class MetricConflictError(ValueError):
    """A metric name was re-registered with a different type, label set or buckets."""


class MetricLimitError(RuntimeError):
    """Registering another metric family would exceed ``family_limit``."""


class MetricType(enum.StrEnum):
    """The Prometheus type of a metric family."""

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
    return _INVALID_NAME_CHARS.sub("_", name)


@functools.lru_cache(maxsize=1024)
def _sanitize_key(key: str, /) -> str:
    """Cached :func:`sanitize_name` for label keys given per call."""
    return sanitize_name(key)


def _validate_buckets(buckets: Iterable[float], /) -> tuple[float, ...]:
    """Return ``buckets`` as floats, or raise ``ValueError`` unless finite and increasing."""
    bounds = tuple(float(b) for b in buckets)
    if not bounds:
        raise ValueError("histogram buckets must not be empty")
    if not all(math.isfinite(b) for b in bounds):
        raise ValueError(f"histogram buckets must be finite: {bounds!r}")
    if any(lo >= hi for lo, hi in itertools.pairwise(bounds)):
        raise ValueError(f"histogram buckets must be strictly increasing: {bounds!r}")
    return bounds


def _label_name(raw: str, /) -> str:
    """Sanitize a label name, raising ``ValueError`` if it is still not a valid label."""
    name = sanitize_name(raw)
    if not _LABEL_NAME.fullmatch(name):
        raise ValueError(f"invalid label name {raw!r}")
    if name.startswith("__"):
        raise ValueError(f"label name {raw!r} is reserved (starts with '__')")
    return name


def _reject_negative(amount: float, /) -> NoReturn:
    raise ValueError(f"counter increment must be a non-negative number, got {amount!r}")


# ------------------------------------------------------------------------ timer


class Timer:
    """Times a block or function and observes the elapsed **milliseconds**.

    Works as a context manager and as a decorator (sync and async functions).
    As a decorator each call is timed independently, so one decorated
    function may run concurrently in many threads or tasks.
    """

    __slots__ = ("_observe", "_start")

    def __init__(self, observe: Callable[[float], None], /) -> None:
        """``observe(milliseconds)`` is called once per timed block or call."""
        self._observe = observe
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._observe((time.perf_counter() - self._start) * 1000.0)

    def __call__(self, fn: F, /) -> F:
        """Wrap ``fn`` so that every call (or every ``await`` of it) is timed."""
        observe = self._observe
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def timed_coroutine(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    observe((time.perf_counter() - start) * 1000.0)

            return cast("F", timed_coroutine)

        @functools.wraps(fn)
        def timed(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                observe((time.perf_counter() - start) * 1000.0)

        return cast("F", timed)


# -------------------------------------------------------------- child interfaces


class CounterChild(ABC):
    """A counter bound to one label set (cached by the parent)."""

    __slots__ = ()

    @abstractmethod
    def inc(self, amount: float = 1.0, /) -> None:
        """Add ``amount`` (must be ``>= 0``; ``ValueError`` otherwise)."""

    def add(self, amount: float, /) -> None:
        """Alias of :meth:`inc` (Go: ``Add``)."""
        self.inc(amount)


class GaugeChild(ABC):
    """A gauge bound to one label set."""

    __slots__ = ()

    @abstractmethod
    def set(self, value: float, /) -> None:
        """Set the gauge to ``value``."""

    @abstractmethod
    def add(self, amount: float, /) -> None:
        """Add ``amount`` (may be negative)."""

    def inc(self, amount: float = 1.0, /) -> None:
        """Add ``amount`` (default 1)."""
        self.add(amount)

    def dec(self, amount: float = 1.0, /) -> None:
        """Subtract ``amount`` (default 1)."""
        self.add(-amount)

    def sub(self, amount: float, /) -> None:
        """Subtract ``amount``."""
        self.add(-amount)


class HistogramChild(ABC):
    """A histogram bound to one label set."""

    __slots__ = ()

    @abstractmethod
    def observe(self, value: float, /) -> None:
        """Record one observation."""

    def time(self) -> Timer:
        """Return a :class:`Timer` that observes elapsed milliseconds."""
        return Timer(self.observe)


# ----------------------------------------------------------------- live series


class _Series(Protocol):
    """What a family needs from each of its live series."""

    _labels: Mapping[str, str]
    _order: tuple[str, ...]

    def _sample(self) -> Sample: ...

    def _zero(self) -> None: ...


class _CounterSeries(CounterChild):
    __slots__ = ("_labels", "_lock", "_order", "_value")

    def __init__(self, labels: Mapping[str, str], /) -> None:
        self._labels = labels
        self._order = tuple(labels.values())
        self._lock = threading.Lock()
        self._value = 0.0

    def inc(self, amount: float = 1.0, /) -> None:
        if not amount >= 0:  # also rejects NaN
            _reject_negative(amount)
        with self._lock:
            self._value += amount

    def _set(self, value: float, /) -> None:
        with self._lock:
            self._value = value

    def _sample(self) -> Sample:
        with self._lock:
            value = self._value
        return Sample(self._labels, value=value)

    def _zero(self) -> None:
        self._lock = threading.Lock()
        self._value = 0.0


class _GaugeSeries(GaugeChild):
    __slots__ = ("_labels", "_lock", "_order", "_value")

    def __init__(self, labels: Mapping[str, str], /) -> None:
        self._labels = labels
        self._order = tuple(labels.values())
        self._lock = threading.Lock()
        self._value = 0.0

    def set(self, value: float, /) -> None:
        with self._lock:
            self._value = value

    def add(self, amount: float, /) -> None:
        with self._lock:
            self._value += amount

    def _sample(self) -> Sample:
        with self._lock:
            value = self._value
        return Sample(self._labels, value=value)

    def _zero(self) -> None:
        self._lock = threading.Lock()
        self._value = 0.0


class _HistogramSeries(HistogramChild):
    """Per-bucket (non-cumulative) counts; the last slot is the ``+Inf`` overflow."""

    __slots__ = ("_bounds", "_count", "_counts", "_labels", "_lock", "_order", "_sum")

    def __init__(self, labels: Mapping[str, str], bounds: tuple[float, ...], /) -> None:
        self._labels = labels
        self._order = tuple(labels.values())
        self._bounds = bounds
        self._lock = threading.Lock()
        self._counts = [0] * (len(bounds) + 1)
        self._count = 0
        self._sum = 0.0

    def observe(self, value: float, /) -> None:
        # ``le`` is inclusive, so the first bound >= value; NaN only reaches +Inf (as in Go).
        index = bisect_left(self._bounds, value) if value == value else len(self._bounds)
        with self._lock:
            self._counts[index] += 1
            self._count += 1
            self._sum += value

    def _sample(self) -> Sample:
        with self._lock:
            counts = self._counts.copy()
            count = self._count
            total = self._sum
        cumulative = 0
        buckets = []
        for bound, n in zip(self._bounds, counts, strict=False):
            cumulative += n
            buckets.append(Bucket(bound, cumulative))
        return Sample(self._labels, buckets=tuple(buckets), count=count, sum=total)

    def _zero(self) -> None:
        self._lock = threading.Lock()
        self._counts = [0] * (len(self._bounds) + 1)
        self._count = 0
        self._sum = 0.0


class _DroppedCounter(CounterChild):
    """Returned for series past the limit: validates input, records nothing."""

    __slots__ = ()

    def inc(self, amount: float = 1.0, /) -> None:
        if not amount >= 0:
            _reject_negative(amount)


class _DroppedGauge(GaugeChild):
    __slots__ = ()

    def set(self, value: float, /) -> None:
        pass

    def add(self, amount: float, /) -> None:
        pass


class _DroppedHistogram(HistogramChild):
    __slots__ = ()

    def observe(self, value: float, /) -> None:
        pass


_DROPPED_COUNTER = _DroppedCounter()
_DROPPED_GAUGE = _DroppedGauge()
_DROPPED_HISTOGRAM = _DroppedHistogram()

_S = TypeVar("_S", bound=_Series)


# --------------------------------------------------------------------- families


@dataclass(frozen=True, slots=True)
class _Schema:
    """Everything that identifies a family for re-registration checks."""

    name: str
    help: str
    type: MetricType
    labels: tuple[str, ...]
    """Sanitized declared label names, in declaration order."""
    raw_labels: tuple[str, ...]
    """Declared label names as given (the keys the label filter sees)."""
    buckets: tuple[float, ...] = ()


class _Metric(ABC, Generic[_S]):
    """Common machinery of registered metrics: label binding, series, snapshots."""

    __slots__ = (
        "_blank",
        "_cache",
        "_default",
        "_index",
        "_lock",
        "_registry",
        "_schema",
        "_series",
    )

    def __init__(self, registry: Registry, schema: _Schema, /) -> None:
        self._registry = registry
        self._schema = schema
        self._lock = threading.Lock()
        # Raw key -> position, for both the declared and the sanitized spelling.
        index = {name: i for i, name in enumerate(schema.labels)}
        index.update({raw: i for i, raw in enumerate(schema.raw_labels)})
        self._index = index
        self._blank = (MISSING_LABEL,) * len(schema.labels)
        """Label values with every declared label missing."""
        self._cache: dict[tuple[str, ...], _S] = {}
        """Label text as given -> series (before the label filter)."""
        self._series: dict[tuple[str, ...], _S] = {}
        """Filtered label values -> series: what is exported."""
        self._default: _S | None = None

    @property
    def name(self) -> str:
        """The full (prefixed, sanitized) metric name."""
        return self._schema.name

    @property
    def help(self) -> str:
        """The help text of the first registration."""
        return self._schema.help

    @property
    def label_names(self) -> tuple[str, ...]:
        """Declared (sanitized) label names, excluding constant labels."""
        return self._schema.labels

    @abstractmethod
    def _new_series(self, labels: Mapping[str, str], /) -> _S:
        """Create the live series for one label set."""

    # -- binding ----------------------------------------------------------------

    def _child(self, mapping: LabelArg, kwargs: Mapping[str, AttrValue], /) -> _S | None:
        """The series for these label values, or ``None`` if it was dropped.

        The hot path: on a cache hit this renders the values and does one
        dict lookup, without sanitizing keys or taking a lock.
        """
        if mapping:
            kwargs = {**mapping, **kwargs}
        elif not kwargs:
            return self._unlabelled()
        values = list(self._blank)
        index = self._index
        for name, value in kwargs.items():
            position = index.get(name)
            if position is None:
                position = self._position(name)
            values[position] = value if type(value) is str else _text_value(value)
        key = tuple(values)
        series = self._cache.get(key)
        return series if series is not None else self._bind(key)

    def _unlabelled(self) -> _S | None:
        """The series with every declared label missing (``"_"``)."""
        series = self._default
        if series is None:
            series = self._default = self._bind(self._blank)
        return series

    def _position(self, key: str, /) -> int:
        position = self._index.get(_sanitize_key(key))
        if position is not None:
            return position
        if _sanitize_key(key) in self._registry.const_labels:
            raise ValueError(f"label {key!r} of {self.name} is a constant label")
        raise ValueError(f"label {key!r} is not declared for {self.name}")

    def _bind(self, key: tuple[str, ...], /) -> _S | None:
        filtered = self._filter(key)
        limit = self._registry._series_limit
        with self._lock:
            series = self._series.get(filtered)
            if series is None and len(self._series) < limit:
                series = self._series[filtered] = self._new_series(self._label_map(filtered))
            if series is not None and len(self._cache) < limit * _RAW_CACHE_FACTOR:
                self._cache[key] = series
        if series is None:
            self._registry._count_dropped()
        return series

    def _filter(self, key: tuple[str, ...], /) -> tuple[str, ...]:
        label_filter = self._registry._label_filter
        if label_filter is None:
            return key
        return tuple(
            value if value == MISSING_LABEL else label_filter(raw, value)
            for raw, value in zip(self._schema.raw_labels, key, strict=True)
        )

    def _label_map(self, values: tuple[str, ...], /) -> Mapping[str, str]:
        labels = dict(self._registry.const_labels)
        labels.update(zip(self._schema.labels, values, strict=True))
        return MappingProxyType(dict(sorted(labels.items())))

    # -- collector seams --------------------------------------------------------

    def _clear(self) -> None:
        """Forget every series (collector seam for values that disappear)."""
        with self._lock:
            self._series.clear()
            self._cache.clear()
            self._default = None

    # -- snapshots and reset ---------------------------------------------------

    def _collect(self) -> MetricFamily:
        with self._lock:
            series = list(self._series.values())
        series.sort(key=attrgetter("_order"))
        schema = self._schema
        return MetricFamily(
            schema.name, schema.help, schema.type, tuple(s._sample() for s in series)
        )

    def _reset(self) -> None:
        self._lock = threading.Lock()
        for series in self._series.values():
            series._zero()


class Counter(_Metric[_CounterSeries], CounterChild):
    """A monotonically increasing counter. ``inc()`` without labels uses ``"_"`` for each."""

    __slots__ = ()

    def labels(self, mapping: LabelArg = None, /, **labels: AttrValue) -> CounterChild:
        """Bind label values (``.labels(method="GET")`` or ``.labels({"http.method": "GET"})``)."""
        series = self._child(mapping, labels)
        return _DROPPED_COUNTER if series is None else series

    def inc(self, amount: float = 1.0, /) -> None:
        """Add ``amount`` to the unlabelled series (must be ``>= 0``)."""
        series = self._default or self._unlabelled()
        (_DROPPED_COUNTER if series is None else series).inc(amount)

    def _set(self, value: float, mapping: LabelArg = None, /) -> None:
        """Collector seam: overwrite a series total read from elsewhere (gc, the OS)."""
        series = self._child(mapping, {})
        if series is not None:
            series._set(value)

    def _new_series(self, labels: Mapping[str, str], /) -> _CounterSeries:
        return _CounterSeries(labels)


class Gauge(_Metric[_GaugeSeries], GaugeChild):
    """A value that can go up and down."""

    __slots__ = ()

    def labels(self, mapping: LabelArg = None, /, **labels: AttrValue) -> GaugeChild:
        """Bind label values; see :meth:`Counter.labels`."""
        series = self._child(mapping, labels)
        return _DROPPED_GAUGE if series is None else series

    def set(self, value: float, /) -> None:
        """Set the unlabelled series."""
        series = self._default or self._unlabelled()
        (_DROPPED_GAUGE if series is None else series).set(value)

    def add(self, amount: float, /) -> None:
        """Add to the unlabelled series."""
        series = self._default or self._unlabelled()
        (_DROPPED_GAUGE if series is None else series).add(amount)

    def _new_series(self, labels: Mapping[str, str], /) -> _GaugeSeries:
        return _GaugeSeries(labels)


class Histogram(_Metric[_HistogramSeries], HistogramChild):
    """A bucketed distribution (bucket arrays + ``bisect``)."""

    __slots__ = ()

    @property
    def buckets(self) -> tuple[float, ...]:
        """Finite upper bounds (``+Inf`` is implied)."""
        return self._schema.buckets

    def labels(self, mapping: LabelArg = None, /, **labels: AttrValue) -> HistogramChild:
        """Bind label values; see :meth:`Counter.labels`."""
        series = self._child(mapping, labels)
        return _DROPPED_HISTOGRAM if series is None else series

    def observe(self, value: float, /) -> None:
        """Record one observation on the unlabelled series."""
        series = self._default or self._unlabelled()
        (_DROPPED_HISTOGRAM if series is None else series).observe(value)

    def _new_series(self, labels: Mapping[str, str], /) -> _HistogramSeries:
        return _HistogramSeries(labels, self._schema.buckets)


_AnyMetric = Counter | Gauge | Histogram
_M = TypeVar("_M", Counter, Gauge, Histogram)
_CLASSES: dict[MetricType, type[_AnyMetric]] = {
    MetricType.COUNTER: Counter,
    MetricType.GAUGE: Gauge,
    MetricType.HISTOGRAM: Histogram,
}


# --------------------------------------------------------------------- registry


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
        """Create an empty registry.

        ``prefix`` is prepended to every name with an underscore. ``buckets``
        are the default histogram bounds. ``const_labels`` are added to every
        series (values rendered like attributes; the label filter is not
        applied to them). ``series_limit`` bounds series per family and
        ``family_limit`` the number of families. ``label_filter(key, value) ->
        value`` applies the privacy policy to label values; ``key`` is the label
        name as declared (e.g. ``"http.method"``).
        """
        if series_limit <= 0 or family_limit <= 0:
            raise ValueError("series_limit and family_limit must be positive")
        self._prefix = prefix
        self._name_prefix = sanitize_name(prefix) + "_" if prefix else ""
        self._buckets = DEFAULT_BUCKETS if buckets is None else _validate_buckets(buckets)
        self._const_labels = MappingProxyType(
            {_label_name(k): _text_value(v) for k, v in (const_labels or {}).items()}
        )
        self._series_limit = series_limit
        self._family_limit = family_limit
        self._label_filter = label_filter
        self._lock = threading.Lock()
        self._families: dict[str, _AnyMetric] = {}
        self._collectors: list[Collector] = []
        self._dropped = 0

    @property
    def prefix(self) -> str:
        """The prefix as given."""
        return self._prefix

    @property
    def default_buckets(self) -> tuple[float, ...]:
        """Bounds used by histograms registered with ``buckets=None``."""
        return self._buckets

    @property
    def const_labels(self) -> Mapping[str, str]:
        """Sanitized constant labels added to every series."""
        return self._const_labels

    # -- registration ------------------------------------------------------------

    def counter(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Counter:
        """Get or create a counter."""
        return self._register(Counter, self._schema(name, help, MetricType.COUNTER, labels))

    def gauge(self, name: str, help: str = "", *, labels: Iterable[str] = ()) -> Gauge:
        """Get or create a gauge."""
        return self._register(Gauge, self._schema(name, help, MetricType.GAUGE, labels))

    def histogram(
        self,
        name: str,
        help: str = "",
        *,
        buckets: Sequence[float] | None = None,
        labels: Iterable[str] = (),
    ) -> Histogram:
        """``buckets=None`` uses the registry default; they must be finite and increasing.

        Re-registering with ``buckets=None`` returns the existing histogram
        whatever its buckets; explicit, different buckets are a conflict.
        """
        bounds = self._buckets if buckets is None else _validate_buckets(buckets)
        schema = self._schema(name, help, MetricType.HISTOGRAM, labels, bounds)
        return self._register(Histogram, schema, explicit_buckets=buckets is not None)

    def _schema(
        self,
        name: str,
        help: str,
        kind: MetricType,
        labels: Iterable[str],
        buckets: tuple[float, ...] = (),
    ) -> _Schema:
        full = self._name_prefix + sanitize_name(name)
        if not _METRIC_NAME.fullmatch(full):
            raise ValueError(f"invalid metric name {name!r}")
        raw = tuple(labels)
        sanitized = tuple(_label_name(label) for label in raw)
        if len(set(sanitized)) != len(sanitized):
            raise ValueError(f"duplicate label names for {full}: {raw!r}")
        clashes = set(sanitized) & self._const_labels.keys()
        if clashes:
            raise ValueError(f"labels {sorted(clashes)} of {full} are constant labels")
        if kind is MetricType.HISTOGRAM and "le" in {*sanitized, *self._const_labels}:
            raise ValueError(f"histogram {full} may not use the reserved label 'le'")
        return _Schema(full, help, kind, sanitized, raw, buckets)

    def _register(self, cls: type[_M], schema: _Schema, *, explicit_buckets: bool = True) -> _M:
        with self._lock:
            existing = self._families.get(schema.name)
            if existing is None:
                if len(self._families) >= self._family_limit:
                    raise MetricLimitError(
                        f"cannot register {schema.name}: family limit {self._family_limit} reached"
                    )
                metric = self._families[schema.name] = cls(self, schema)
                return metric
        _check_same_schema(existing._schema, schema, explicit_buckets=explicit_buckets)
        return cast("_M", existing)

    def get(self, name: str, /) -> Counter | Gauge | Histogram | None:
        """Look up a registered metric by full or unprefixed name."""
        sanitized = sanitize_name(name)
        families = self._families
        return families.get(sanitized) or families.get(self._name_prefix + sanitized)

    # -- collectors and snapshots --------------------------------------------------

    def register_collector(self, collector: Collector, /) -> None:
        """Run ``collector.collect()`` before every snapshot."""
        with self._lock:
            self._collectors.append(collector)

    def unregister_collector(self, collector: Collector, /) -> None:
        """Stop running ``collector`` (no error if it is not registered)."""
        with self._lock:
            if collector in self._collectors:
                self._collectors.remove(collector)

    def collect(self) -> list[MetricFamily]:
        """Run collectors, then return a consistent snapshot sorted by name (Go: ``Gather``).

        Each series is read under its own lock, so every sample is internally
        consistent (a histogram's buckets never exceed its count). Series are
        sorted by label values and labels by name.
        """
        with self._lock:
            collectors = list(self._collectors)
        for collector in collectors:
            collector.collect()
        with self._lock:
            families = sorted(self._families.items())
        return [metric._collect() for _, metric in families]

    def dropped_series(self) -> int:
        """Series dropped because ``series_limit`` was reached."""
        return self._dropped

    def _count_dropped(self) -> None:
        with self._lock:
            self._dropped += 1

    def reset(self) -> None:
        """Zero every series, keeping registrations (used in a forked child).

        Every lock is replaced rather than acquired, because a lock held by a
        thread at ``fork()`` time is never released in the child. Call it only
        while no other thread uses the registry, e.g. ``after_in_child``.
        """
        self._lock = threading.Lock()
        self._dropped = 0
        for metric in self._families.values():
            metric._reset()


def _check_same_schema(existing: _Schema, new: _Schema, /, *, explicit_buckets: bool) -> None:
    """Raise :class:`MetricConflictError` unless ``new`` matches ``existing``."""
    if existing.type is not new.type:
        raise MetricConflictError(
            f"{new.name} is already registered as a {existing.type}, not a {new.type}"
        )
    if set(existing.labels) != set(new.labels):
        raise MetricConflictError(
            f"{new.name} is already registered with labels {list(existing.labels)}, "
            f"not {list(new.labels)}"
        )
    if explicit_buckets and existing.buckets != new.buckets:
        raise MetricConflictError(
            f"{new.name} is already registered with buckets {list(existing.buckets)}"
        )
