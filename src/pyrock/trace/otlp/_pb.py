"""A minimal hand-written protobuf encoder (varint, fixed64, length-delimited).

Covers the fixed OTLP ``ExportTraceServiceRequest`` schema; the profile
encoders reuse :class:`Writer`. There is no reflection and no ``.proto``
compilation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pyrock.attr import AttrValue
from pyrock.trace import Span

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


def encode_varint(value: int, /) -> bytes:
    """Encode a non-negative int (negative int64 values use two's complement, 10 bytes)."""
    raise NotImplementedError


class Writer:
    """Appends protobuf fields to an internal buffer. Default values are still written."""

    def __init__(self) -> None:
        raise NotImplementedError

    def varint(self, field: int, value: int, /) -> None:
        raise NotImplementedError

    def sint(self, field: int, value: int, /) -> None:
        """Zig-zag encoded signed varint."""
        raise NotImplementedError

    def bool_(self, field: int, value: bool, /) -> None:
        raise NotImplementedError

    def fixed64(self, field: int, value: int, /) -> None:
        raise NotImplementedError

    def fixed32(self, field: int, value: int, /) -> None:
        raise NotImplementedError

    def double(self, field: int, value: float, /) -> None:
        raise NotImplementedError

    def bytes_(self, field: int, value: bytes, /) -> None:
        raise NotImplementedError

    def string(self, field: int, value: str, /) -> None:
        raise NotImplementedError

    def message(self, field: int, value: bytes | Writer, /) -> None:
        """Write an embedded message (length-delimited)."""
        raise NotImplementedError

    def packed_varints(self, field: int, values: Sequence[int], /) -> None:
        raise NotImplementedError

    def getvalue(self) -> bytes:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError


def encode_any_value(value: AttrValue, /) -> bytes | None:
    """Encode an OTLP ``AnyValue``; ``None`` for unsupported values (dropped)."""
    raise NotImplementedError


def encode_key_value(key: str, value: AttrValue, /) -> bytes | None:
    """Encode an OTLP ``KeyValue``; ``None`` for unsupported values (dropped)."""
    raise NotImplementedError


def encode_export_trace_request(
    spans: Sequence[Span],
    /,
    *,
    service: str,
    resource: Mapping[str, AttrValue] | None = None,
    scope_name: str = "pyrock",
    scope_version: str = "",
) -> bytes:
    """Encode ``ExportTraceServiceRequest`` bytes for ``spans``."""
    raise NotImplementedError
