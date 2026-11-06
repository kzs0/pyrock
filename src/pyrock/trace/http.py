"""HTTP header propagation of W3C Trace Context (Go: ``trace/http``).

Header names are matched case-insensitively. Repeated ``tracestate`` headers
are joined with commas. An invalid ``traceparent`` yields ``None`` (start a new
trace) and the ``tracestate`` is ignored.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from typing import Any, TypeAlias

from pyrock.trace import SpanContext, current_span_context
from pyrock.trace.w3c import (
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    InvalidTraceparent,
    format_traceparent,
    format_tracestate,
    span_context_from_traceparent,
)

__all__ = ["HTTPPropagator", "HeaderCarrier", "WSGIEnvironHeaders"]

HeaderCarrier: TypeAlias = (
    Mapping[str, str] | Iterable[tuple[str, str]] | Iterable[tuple[bytes, bytes]]
)
"""A header ``Mapping``, or a list of ``(name, value)`` pairs (str or ASGI bytes)."""


class HTTPPropagator:
    """Extracts/injects ``traceparent`` and ``tracestate`` on HTTP headers."""

    __slots__ = ()

    def extract(self, carrier: Any) -> SpanContext | None:
        """Read trace context from a :data:`HeaderCarrier`; ``None`` if absent or invalid."""
        if isinstance(carrier, Mapping):
            traceparent, tracestate = _from_mapping(carrier)
        else:
            traceparent, tracestate = _from_pairs(carrier)
        if traceparent is None:
            return None
        try:
            return span_context_from_traceparent(traceparent, tracestate)
        except InvalidTraceparent:
            return None

    def inject(self, carrier: Any, span_context: SpanContext | None = None) -> None:
        """Write trace context into a ``MutableMapping[str, str]`` or a list of pairs.

        Uses the current span's context when ``span_context`` is ``None``; does
        nothing when there is no valid context. A mapping gets lowercase keys
        (a stale ``tracestate`` is removed when there is none to send); a list
        gets pairs appended, as ``bytes`` when the list already holds ``bytes``
        names (ASGI style).
        """
        context = current_span_context() if span_context is None else span_context
        if context is None or not context.is_valid:
            return
        traceparent = format_traceparent(context)
        tracestate = format_tracestate(context.trace_state)
        if isinstance(carrier, MutableMapping):
            carrier[TRACEPARENT_HEADER] = traceparent
            if tracestate:
                carrier[TRACESTATE_HEADER] = tracestate
            else:
                carrier.pop(TRACESTATE_HEADER, None)
            return
        headers = [(TRACEPARENT_HEADER, traceparent)]
        if tracestate:
            headers.append((TRACESTATE_HEADER, tracestate))
        if any(isinstance(name, bytes) for name, _ in carrier):
            carrier.extend((n.encode("latin-1"), v.encode("latin-1")) for n, v in headers)
        else:
            carrier.extend(headers)


def _text(value: str | bytes) -> str:
    return value.decode("latin-1") if isinstance(value, bytes) else value


def _from_mapping(headers: Mapping[str, str]) -> tuple[str | None, str | None]:
    """``(traceparent, tracestate)``: exact lowercase keys first, then a case-insensitive scan."""
    traceparent = headers.get(TRACEPARENT_HEADER)
    tracestate = headers.get(TRACESTATE_HEADER)
    if traceparent is not None:
        if tracestate is None:
            tracestate = _from_pairs(headers.items())[1]
        return traceparent, tracestate
    return _from_pairs(headers.items())


def _from_pairs(
    pairs: Iterable[tuple[str, str]] | Iterable[tuple[bytes, bytes]],
) -> tuple[str | None, str | None]:
    """``(traceparent, tracestate)`` from header pairs; repeated tracestates are joined."""
    traceparent: str | None = None
    tracestates: list[str] = []
    for raw_name, raw_value in pairs:
        name = _text(raw_name).lower()
        if name == TRACEPARENT_HEADER:
            if traceparent is None:
                traceparent = _text(raw_value)
        elif name == TRACESTATE_HEADER:
            tracestates.append(_text(raw_value))
    return traceparent, ",".join(tracestates) if tracestates else None


class WSGIEnvironHeaders(Mapping[str, str]):
    """A case-insensitive header view over a WSGI ``environ`` (``HTTP_*`` keys).

    ``CONTENT_TYPE`` and ``CONTENT_LENGTH`` appear as ``content-type`` and
    ``content-length``. The view is live: it reads ``environ`` on every access.
    """

    __slots__ = ("_environ",)

    def __init__(self, environ: MutableMapping[str, Any] | Mapping[str, Any], /) -> None:
        self._environ = environ

    def __getitem__(self, key: str) -> str:
        value = self._environ.get(_environ_key(key))
        if not isinstance(value, str):
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        for key, value in self._environ.items():
            name = _header_name(key)
            if name is not None and isinstance(value, str):
                yield name

    def __len__(self) -> int:
        return sum(1 for _ in self)


_UNPREFIXED = {"CONTENT_TYPE": "content-type", "CONTENT_LENGTH": "content-length"}


def _environ_key(header: str) -> str:
    key = header.upper().replace("-", "_")
    return key if key in _UNPREFIXED else "HTTP_" + key


def _header_name(environ_key: object) -> str | None:
    if not isinstance(environ_key, str):
        return None
    if environ_key.startswith("HTTP_"):
        return environ_key[5:].lower().replace("_", "-")
    return _UNPREFIXED.get(environ_key)
