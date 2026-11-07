"""A minimal hand-written protobuf encoder (varint, fixed64, length-delimited).

Covers the fixed OTLP ``ExportTraceServiceRequest`` schema; the profile
encoders reuse :class:`Writer`. There is no reflection and no ``.proto``
compilation. Field numbers follow ``opentelemetry/proto/trace/v1/trace.proto``
and are written in ascending order; fields holding their schema default are
omitted (no parent for roots, no empty trace state, no status when UNSET).
"""

from __future__ import annotations

import functools
import struct
from collections.abc import Iterable, Mapping, Sequence

from pyrock.attr import AttrValue, _normalize_value
from pyrock.trace import Span, SpanEvent, StatusCode, TraceState

__all__ = [
    "WIRE_FIXED32",
    "WIRE_FIXED64",
    "WIRE_LEN",
    "WIRE_VARINT",
    "Writer",
    "encode_any_value",
    "encode_export_trace_request",
    "encode_key_value",
    "encode_varint",
]

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

_UINT64_LIMIT = 1 << 64
_INT64_MIN = -(1 << 63)
_FIXED64 = struct.Struct("<Q")
_FIXED32 = struct.Struct("<I")
_DOUBLE = struct.Struct("<d")
_SMALL_VARINTS = tuple(bytes((i,)) for i in range(128))


def encode_varint(value: int, /) -> bytes:
    """Encode a non-negative int (negative int64 values use two's complement, 10 bytes)."""
    if 0 <= value < 128:
        return _SMALL_VARINTS[value]
    if not _INT64_MIN <= value < _UINT64_LIMIT:
        raise ValueError(f"varint out of range: {value}")
    if value < 0:
        value += _UINT64_LIMIT
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


@functools.lru_cache(maxsize=512)
def _tag(field: int, wire: int) -> bytes:
    return encode_varint((field << 3) | wire)


class Writer:
    """Appends protobuf fields to an internal buffer. Default values are still written."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def varint(self, field: int, value: int, /) -> None:
        """Write a varint field (``int32``/``int64``/``uint64``/enums)."""
        self._buf += _tag(field, WIRE_VARINT)
        self._buf += encode_varint(value)

    def sint(self, field: int, value: int, /) -> None:
        """Zig-zag encoded signed varint."""
        self.varint(field, (value << 1) ^ (value >> 63))

    def bool_(self, field: int, value: bool, /) -> None:
        """Write a ``bool`` field."""
        self.varint(field, 1 if value else 0)

    def fixed64(self, field: int, value: int, /) -> None:
        """Write a little-endian ``fixed64`` field."""
        self._buf += _tag(field, WIRE_FIXED64)
        self._buf += _FIXED64.pack(value)

    def fixed32(self, field: int, value: int, /) -> None:
        """Write a little-endian ``fixed32`` field."""
        self._buf += _tag(field, WIRE_FIXED32)
        self._buf += _FIXED32.pack(value)

    def double(self, field: int, value: float, /) -> None:
        """Write a ``double`` field."""
        self._buf += _tag(field, WIRE_FIXED64)
        self._buf += _DOUBLE.pack(value)

    def bytes_(self, field: int, value: bytes | bytearray, /) -> None:
        """Write a length-delimited ``bytes`` field."""
        self._buf += _tag(field, WIRE_LEN)
        self._buf += encode_varint(len(value))
        self._buf += value

    def string(self, field: int, value: str, /) -> None:
        """Write a UTF-8 ``string`` field."""
        self.bytes_(field, value.encode())

    def message(self, field: int, value: bytes | Writer, /) -> None:
        """Write an embedded message (length-delimited)."""
        self.bytes_(field, value._buf if isinstance(value, Writer) else value)

    def packed_varints(self, field: int, values: Sequence[int], /) -> None:
        """Write a packed repeated varint field."""
        self.bytes_(field, b"".join(encode_varint(v) for v in values))

    def getvalue(self) -> bytes:
        """The bytes written so far."""
        return bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)


# -- OTLP common ----------------------------------------------------------


def _write_any(w: Writer, value: AttrValue) -> None:
    """Write the fields of an already-normalized value as an ``AnyValue``."""
    tp = type(value)
    if tp is str:
        w.string(1, value)  # type: ignore[arg-type]
    elif tp is bool:
        w.bool_(2, value)  # type: ignore[arg-type]
    elif tp is int:
        w.varint(3, value)  # type: ignore[arg-type]
    elif tp is float:
        w.double(4, value)  # type: ignore[arg-type]
    elif tp is bytes:
        w.bytes_(7, value)  # type: ignore[arg-type]
    else:  # a homogeneous list (ArrayValue.values = 1)
        array = Writer()
        for item in value:  # type: ignore[union-attr]
            element = Writer()
            _write_any(element, item)
            array.message(1, element)
        w.message(5, array)


def encode_any_value(value: AttrValue, /) -> bytes | None:
    """Encode an OTLP ``AnyValue``; ``None`` for unsupported values (dropped)."""
    normalized = _normalize_value(value)
    if normalized is None:
        return None
    w = Writer()
    _write_any(w, normalized)
    return w.getvalue()


def _key_value(key: str, normalized: AttrValue) -> Writer:
    w = Writer()
    w.string(1, key)
    value = Writer()
    _write_any(value, normalized)
    w.message(2, value)
    return w


def encode_key_value(key: str, value: AttrValue, /) -> bytes | None:
    """Encode an OTLP ``KeyValue``; ``None`` for unsupported values (dropped)."""
    normalized = _normalize_value(value)
    return None if normalized is None else _key_value(key, normalized).getvalue()


def normalized_items(attrs: Mapping[str, AttrValue]) -> tuple[list[tuple[str, AttrValue]], int]:
    """Export-ready ``(key, value)`` pairs and the number of dropped values.

    Shared by the protobuf and JSON encoders: every value goes through
    :func:`pyrock.attr._normalize_value` (privacy has already been applied).
    """
    items: list[tuple[str, AttrValue]] = []
    dropped = 0
    for key, value in attrs.items():
        normalized = _normalize_value(value)
        if normalized is None:
            dropped += 1
        else:
            items.append((key, normalized))
    return items, dropped


def _write_attributes(
    w: Writer, field: int, dropped_field: int, attrs: Mapping[str, AttrValue]
) -> None:
    items, dropped = normalized_items(attrs)
    for key, value in items:
        w.message(field, _key_value(key, value))
    if dropped:
        w.varint(dropped_field, dropped)


def resource_attributes(
    service: str, resource: Mapping[str, AttrValue] | None
) -> dict[str, AttrValue]:
    """``service.name`` first, then the other resource attributes."""
    out: dict[str, AttrValue] = {"service.name": service}
    if resource:
        out.update((k, v) for k, v in resource.items() if k != "service.name")
    return out


def tracestate_text(state: TraceState) -> str:
    """The ``tracestate`` header form of ``state`` (``""`` when empty)."""
    return ",".join(f"{key}={value}" for key, value in state.entries())


def end_time_ns(span: Span) -> int:
    """The span's end time; its start time if it was never ended."""
    end = span.end_time_ns
    return span.start_time_ns if end is None else end


# -- trace schema ---------------------------------------------------------


def _event(event: SpanEvent) -> Writer:
    w = Writer()
    w.fixed64(1, event.timestamp_ns)
    w.string(2, event.name)
    _write_attributes(w, 3, 4, event.attrs)
    return w


def _status(code: StatusCode, message: str) -> Writer:
    w = Writer()
    if message:
        w.string(2, message)
    w.varint(3, int(code))
    return w


def _span(span: Span) -> Writer:
    ctx = span.context
    w = Writer()
    w.bytes_(1, ctx.trace_id)
    w.bytes_(2, ctx.span_id)
    if ctx.trace_state:
        w.string(3, tracestate_text(ctx.trace_state))
    if span.parent is not None:
        w.bytes_(4, span.parent.span_id)
    w.string(5, span.name)
    w.varint(6, int(span.kind))
    w.fixed64(7, span.start_time_ns)
    w.fixed64(8, end_time_ns(span))
    _write_attributes(w, 9, 10, span.attrs)
    for event in span.events:
        w.message(11, _event(event))
    code, message = span.status
    if code is not StatusCode.UNSET:
        w.message(15, _status(code, message))
    return w


def _scope(name: str, version: str) -> Writer:
    w = Writer()
    w.string(1, name)
    if version:
        w.string(2, version)
    return w


def _resource(service: str, resource: Mapping[str, AttrValue] | None) -> Writer:
    w = Writer()
    _write_attributes(w, 1, 2, resource_attributes(service, resource))
    return w


def _scope_spans(spans: Iterable[Span], scope_name: str, scope_version: str) -> Writer:
    w = Writer()
    w.message(1, _scope(scope_name, scope_version))
    for span in spans:
        w.message(2, _span(span))
    return w


def encode_export_trace_request(
    spans: Sequence[Span],
    /,
    *,
    service: str,
    resource: Mapping[str, AttrValue] | None = None,
    scope_name: str = "pyrock",
    scope_version: str = "",
) -> bytes:
    """Encode ``ExportTraceServiceRequest`` bytes for ``spans``.

    One ``ResourceSpans`` (``service.name`` plus ``resource``) holding one
    ``ScopeSpans``. Exporters pass the package version as ``scope_version``.
    """
    resource_spans = Writer()
    resource_spans.message(1, _resource(service, resource))
    resource_spans.message(2, _scope_spans(spans, scope_name, scope_version))
    request = Writer()
    request.message(1, resource_spans)
    return request.getvalue()
