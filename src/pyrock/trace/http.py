"""HTTP header propagation of W3C Trace Context (Go: ``trace/http``).

Header names are matched case-insensitively. Repeated ``tracestate`` headers
are joined with commas. An invalid ``traceparent`` yields ``None`` (start a new
trace) and the ``tracestate`` is ignored.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from typing import Any, TypeAlias

from pyrock.trace import SpanContext

__all__ = ["HTTPPropagator", "HeaderCarrier", "WSGIEnvironHeaders"]

HeaderCarrier: TypeAlias = (
    Mapping[str, str] | Iterable[tuple[str, str]] | Iterable[tuple[bytes, bytes]]
)
"""A header ``Mapping``, or a list of ``(name, value)`` pairs (str or ASGI bytes)."""


class HTTPPropagator:
    """Extracts/injects ``traceparent`` and ``tracestate`` on HTTP headers."""

    def extract(self, carrier: Any) -> SpanContext | None:
        """Read trace context from a :data:`HeaderCarrier`; ``None`` if absent or invalid."""
        raise NotImplementedError

    def inject(self, carrier: Any, span_context: SpanContext | None = None) -> None:
        """Write trace context into a ``MutableMapping[str, str]`` or a list of pairs.

        Uses the current span's context when ``span_context`` is ``None``; does
        nothing when there is no valid context.
        """
        raise NotImplementedError


class WSGIEnvironHeaders(Mapping[str, str]):
    """A case-insensitive header view over a WSGI ``environ`` (``HTTP_*`` keys)."""

    def __init__(self, environ: MutableMapping[str, Any] | Mapping[str, Any], /) -> None:
        raise NotImplementedError

    def __getitem__(self, key: str) -> str:
        raise NotImplementedError

    def __iter__(self) -> Iterator[str]:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError
