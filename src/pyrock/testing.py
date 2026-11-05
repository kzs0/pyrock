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

import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from pyrock._bedrock import Bedrock
from pyrock.attr import AttrValue
from pyrock.config import Config
from pyrock.trace import Span, SpanEvent, SpanKind, StatusCode

__all__ = [
    "Capture",
    "CapturedLogs",
    "CapturedMetrics",
    "CapturedSpan",
    "CapturedSpans",
    "InMemorySpanExporter",
    "capture",
]


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


class InMemorySpanExporter:
    """A :class:`pyrock.trace.SpanExporter` that keeps spans in a list (thread-safe)."""

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    def spans(self) -> list[CapturedSpan]:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class CapturedSpans(Sequence[CapturedSpan]):
    """The spans finished inside a capture, in end order."""

    def names(self) -> list[str]:
        raise NotImplementedError

    def find(self, filters: Mapping[str, Any] | None = None, /, **kw: Any) -> list[CapturedSpan]:
        """Spans whose fields (``name=``, ``kind=`` ...) match all filters.

        ``filters`` is an optional positional mapping (merged with ``kw``,
        keywords win) for keys that are not identifiers.
        """
        raise NotImplementedError

    def one(self, filters: Mapping[str, Any] | None = None, /, **kw: Any) -> CapturedSpan:
        """Exactly one matching span (``AssertionError`` otherwise); filters as in :meth:`find`."""
        raise NotImplementedError

    def __getitem__(self, index: Any) -> Any:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError


class CapturedMetrics:
    """Read access to the captured registry."""

    def value(
        self, name: str, labels: Mapping[str, str] | None = None, /, **kw: str
    ) -> float | None:
        """Counter/gauge value (histogram: count) of the series matching the labels.

        ``labels`` is an optional positional mapping for dotted keys
        (``{"worker.type": "async"}``), merged with ``kw`` (keywords win).
        Dots in label keys are matched as underscores. Works inside the block.
        """
        raise NotImplementedError

    def text(self) -> str:
        """The Prometheus exposition text (also valid inside the ``with`` block)."""
        raise NotImplementedError


class CapturedLogs(Sequence[dict[str, Any]]):
    """Parsed JSON log records emitted inside a capture."""

    def find(self, filters: Mapping[str, Any] | None = None, /, **kw: Any) -> dict[str, Any]:
        """The first record whose keys match all filters (``AssertionError`` if none).

        ``filters`` is an optional positional mapping for dotted keys
        (``{"http.route": "/x"}``), merged with ``kw`` (keywords win).
        """
        raise NotImplementedError

    def find_all(
        self, filters: Mapping[str, Any] | None = None, /, **kw: Any
    ) -> list[dict[str, Any]]:
        """Every record matching the filters (same rules as :meth:`find`)."""
        raise NotImplementedError

    def __getitem__(self, index: Any) -> Any:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError


@dataclass(slots=True)
class Capture:
    """What :func:`capture` yields; populated while and after the block runs."""

    bedrock: Bedrock
    spans: CapturedSpans
    metrics: CapturedMetrics
    logs: CapturedLogs
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def __enter__(self) -> Capture:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError


def capture(
    config: Config | None = None,
    /,
    *,
    static_attrs: Mapping[str, AttrValue] | None = None,
    **overrides: Any,
) -> Capture:
    """Activate an in-memory ``Bedrock`` for the ``with`` block and collect its output.

    Defaults: ``server_enabled=False``, ``disable_environment_detection=True``,
    ``runtime_metrics=False``, ``log_install="pyrock"``, environment ignored.

    Spans are complete once the block exits; ``metrics.text()`` and
    ``metrics.value()`` work inside it. On exit the instance is shut down and
    the previously active instance (if any) is restored, so nested captures
    and ``with pyrock.init()`` blocks compose.
    """
    raise NotImplementedError


def _pyrock_capture() -> Iterator[Capture]:
    with capture() as t:
        yield t


# pytest plugin hook-up without importing pytest: only when pytest is the one
# loading this module (via the ``pytest11`` entry point) is it in sys.modules.
_pytest = sys.modules.get("pytest")
if _pytest is not None:  # pragma: no cover - exercised only under pytest
    pyrock_capture = _pytest.fixture(name="pyrock_capture")(_pyrock_capture)
