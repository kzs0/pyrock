"""In-memory test harness: no server, no detection, no network.

.. code-block:: python

    from pyrock.testing import capture

    def test_process_user():
        with capture(service="test") as t:
            process_user("123")
        op = t.spans.one(name="process_user")
        assert op.attrs["user_id"] == "123"
        assert t.metrics.value("process_user_count", status="active") == 1

``capture()`` needs no pytest plugin. When pytest is running, the
``pyrock_capture`` fixture is also available (registered through the
``pytest11`` entry point; this module never imports pytest).
"""

from __future__ import annotations

import io
import json
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, overload

from pyrock._bedrock import Bedrock
from pyrock.attr import AttrValue
from pyrock.config import Config
from pyrock.metric import MetricType, Sample, sanitize_name
from pyrock.metric import prometheus as _prometheus
from pyrock.trace import Span, SpanEvent, SpanKind, SpanProcessor, StatusCode

__all__ = [
    "Capture",
    "CapturedLogs",
    "CapturedMetrics",
    "CapturedSpan",
    "CapturedSpans",
    "InMemorySpanExporter",
    "capture",
]

_CAPTURE_DEFAULTS: Mapping[str, Any] = {
    "server_enabled": False,
    "disable_environment_detection": True,
    "runtime_metrics": False,
    "log_install": "pyrock",
}


@dataclass(frozen=True, slots=True)
class CapturedSpan:
    """A finished span, flattened for assertions."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    kind: SpanKind
    status: StatusCode
    status_message: str
    attrs: Mapping[str, AttrValue]
    events: tuple[SpanEvent, ...]
    start_time_ns: int
    end_time_ns: int

    @property
    def failed(self) -> bool:
        """``True`` when the span status is ERROR."""
        return self.status is StatusCode.ERROR

    @classmethod
    def of(cls, span: Span, /) -> CapturedSpan:
        """Flatten an ended span."""
        status, message = span.status
        parent = span.parent
        return cls(
            name=span.name,
            trace_id=span.context.trace_id_hex,
            span_id=span.context.span_id_hex,
            parent_span_id=None if parent is None else parent.span_id_hex,
            kind=span.kind,
            status=status,
            status_message=message,
            attrs=span.attrs,
            events=tuple(span.events),
            start_time_ns=span.start_time_ns,
            end_time_ns=span.end_time_ns or span.start_time_ns,
        )


class InMemorySpanExporter:
    """A :class:`pyrock.trace.SpanExporter` that keeps spans in a list (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[CapturedSpan] = []

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        captured = [CapturedSpan.of(span) for span in spans]
        with self._lock:
            self._spans.extend(captured)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Nothing to release; spans stay readable."""

    def spans(self) -> list[CapturedSpan]:
        with self._lock:
            return list(self._spans)

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


class _SynchronousProcessor:
    """Exports every ended span immediately (tests need no batching)."""

    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    def on_end(self, span: Span, /) -> None:
        self._exporter.export([span])

    def force_flush(self, *, timeout: float | None = None) -> bool:
        return True

    def shutdown(self, *, timeout: float | None = None) -> None:
        self._exporter.shutdown(timeout=timeout)


def _matches(subject: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    missing = object()
    return all(subject.get(key, missing) == value for key, value in filters.items())


def _filters(positional: Mapping[str, Any] | None, keywords: Mapping[str, Any]) -> dict[str, Any]:
    return {**(positional or {}), **keywords}


class CapturedSpans(Sequence[CapturedSpan]):
    """The spans finished inside a capture, in end order (a live view)."""

    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    def names(self) -> list[str]:
        return [span.name for span in self._exporter.spans()]

    def find(self, filters: Mapping[str, Any] | None = None, /, **kw: Any) -> list[CapturedSpan]:
        """Spans whose fields (``name=``, ``kind=``, ``failed=`` ...) match all filters.

        ``filters`` is an optional positional mapping (merged with ``kw``,
        keywords win) for keys that are not identifiers.
        """
        wanted = _filters(filters, kw)
        missing = object()
        return [
            span
            for span in self._exporter.spans()
            if all(getattr(span, key, missing) == value for key, value in wanted.items())
        ]

    def one(self, filters: Mapping[str, Any] | None = None, /, **kw: Any) -> CapturedSpan:
        """Exactly one matching span (``AssertionError`` otherwise); filters as in :meth:`find`."""
        found = self.find(filters, **kw)
        if len(found) != 1:
            wanted = _filters(filters, kw)
            raise AssertionError(
                f"expected one span matching {wanted!r}, found {len(found)}: {self.names()!r}"
            )
        return found[0]

    @overload
    def __getitem__(self, index: int) -> CapturedSpan: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[CapturedSpan]: ...

    def __getitem__(self, index: int | slice) -> CapturedSpan | Sequence[CapturedSpan]:
        return self._exporter.spans()[index]

    def __len__(self) -> int:
        return len(self._exporter.spans())

    def __repr__(self) -> str:
        return f"CapturedSpans({self.names()!r})"


class CapturedMetrics:
    """Read access to the captured registry."""

    def __init__(self, bedrock: Bedrock) -> None:
        self._bedrock = bedrock

    def value(
        self, name: str, labels: Mapping[str, str] | None = None, /, **kw: str
    ) -> float | None:
        """Counter/gauge value (histogram: count) summed over the series matching the labels.

        ``labels`` is an optional positional mapping for dotted keys
        (``{"worker.type": "async"}``), merged with ``kw`` (keywords win).
        Dots in label keys are matched as underscores; unnamed labels match
        anything. ``name_sum``/``name_count`` read a histogram's sum/count.
        ``None`` when no series matches. Works inside the block.
        """
        wanted = {sanitize_name(k): v for k, v in _filters(labels, kw).items()}
        registry = self._bedrock.metrics
        prefixed = f"{sanitize_name(registry.prefix)}_" if registry.prefix else ""
        names = {sanitize_name(name), prefixed + sanitize_name(name)}
        found: list[float] = []
        for family in registry.collect():
            read = _reader(family.name, family.type, names)
            if read is not None:
                found.extend(read(s) for s in family.samples if _matches(s.labels, wanted))
        return sum(found) if found else None

    def text(self) -> str:
        """The Prometheus exposition text (also valid inside the ``with`` block)."""
        return _prometheus.encode(self._bedrock.metrics).decode("utf-8")


def _sample_value(sample: Sample) -> float:
    return sample.value


def _histogram_count(sample: Sample) -> float:
    return float(sample.count)


def _histogram_sum(sample: Sample) -> float:
    return sample.sum


def _reader(family: str, kind: MetricType, names: set[str]) -> Callable[[Sample], float] | None:
    """How to read a sample of ``family`` when it answers one of ``names`` (else ``None``)."""
    if kind is not MetricType.HISTOGRAM:
        return _sample_value if family in names else None
    if family in names or f"{family}_count" in names:
        return _histogram_count
    return _histogram_sum if f"{family}_sum" in names else None


class CapturedLogs(Sequence[dict[str, Any]]):
    """Parsed JSON log records emitted inside a capture (a live view)."""

    def __init__(self, stream: io.StringIO, lock: threading.Lock) -> None:
        self._stream = stream
        self._lock = lock

    def _records(self) -> list[dict[str, Any]]:
        with self._lock:
            text = self._stream.getvalue()
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:  # text format, or foreign output
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def find(self, filters: Mapping[str, Any] | None = None, /, **kw: Any) -> dict[str, Any]:
        """The first record whose keys match all filters (``AssertionError`` if none).

        ``filters`` is an optional positional mapping for dotted keys
        (``{"http.route": "/x"}``), merged with ``kw`` (keywords win).
        """
        found = self.find_all(filters, **kw)
        if not found:
            raise AssertionError(f"no log record matches {_filters(filters, kw)!r}")
        return found[0]

    def find_all(
        self, filters: Mapping[str, Any] | None = None, /, **kw: Any
    ) -> list[dict[str, Any]]:
        """Every record matching the filters (same rules as :meth:`find`)."""
        wanted = _filters(filters, kw)
        return [record for record in self._records() if _matches(record, wanted)]

    @overload
    def __getitem__(self, index: int) -> dict[str, Any]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[dict[str, Any]]: ...

    def __getitem__(self, index: int | slice) -> dict[str, Any] | Sequence[dict[str, Any]]:
        return self._records()[index]

    def __len__(self) -> int:
        return len(self._records())


class _LockedStream(io.StringIO):
    """The capture's log stream: writes and reads are serialized by one lock."""

    def __init__(self, lock: threading.Lock) -> None:
        super().__init__()
        self._lock = lock

    def write(self, text: str) -> int:
        with self._lock:
            return super().write(text)


@dataclass(slots=True)
class Capture:
    """What :func:`capture` yields; populated while and after the block runs."""

    bedrock: Bedrock
    spans: CapturedSpans
    metrics: CapturedMetrics
    logs: CapturedLogs
    _entered: bool = field(default=False, repr=False)

    def __enter__(self) -> Capture:
        if self._entered:
            raise RuntimeError("a capture can be entered only once")
        self._entered = True
        self.bedrock.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.bedrock.__exit__(exc_type, exc, tb)


def capture(
    config: Config | None = None,
    /,
    *,
    static_attrs: Mapping[str, AttrValue] | None = None,
    **overrides: Any,
) -> Capture:
    """Activate an in-memory ``Bedrock`` for the ``with`` block and collect its output.

    Defaults: ``server_enabled=False``, ``disable_environment_detection=True``,
    ``runtime_metrics=False``, ``log_install="pyrock"``, environment ignored
    (``config`` defaults to :meth:`Config.default`). Logs are collected unless
    ``log_output`` is given; spans never leave the process.

    Spans are complete once the block exits; ``metrics.text()`` and
    ``metrics.value()`` work inside it. On exit the instance is shut down and
    the previously active instance (if any) is restored, so nested captures
    and ``with pyrock.init()`` blocks compose.
    """
    lock = threading.Lock()
    stream = _LockedStream(lock)
    settings = {**_CAPTURE_DEFAULTS, "log_output": stream, **overrides}
    base = Config.default() if config is None else config
    bedrock = Bedrock(base, static_attrs=static_attrs, **settings)
    exporter = InMemorySpanExporter()
    processor: SpanProcessor = _SynchronousProcessor(exporter)
    replaced = bedrock.tracer.processor
    bedrock.tracer.set_processor(processor)
    if replaced is not None:
        replaced.shutdown(timeout=0)
    return Capture(
        bedrock, CapturedSpans(exporter), CapturedMetrics(bedrock), CapturedLogs(stream, lock)
    )


def _pyrock_capture() -> Iterator[Capture]:
    with capture() as t:
        yield t


# pytest plugin hook-up without importing pytest: only when pytest is the one
# loading this module (via the ``pytest11`` entry point) is it in sys.modules.
_pytest = sys.modules.get("pytest")
if _pytest is not None:  # pragma: no cover - exercised only under pytest
    pyrock_capture = _pytest.fixture(name="pyrock_capture")(_pyrock_capture)
