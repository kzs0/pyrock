"""A tiny, independent protobuf reference encoder/decoder used only to build expectations."""

from __future__ import annotations

import struct

VARINT, I64, LEN, I32 = 0, 1, 2, 5


def varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def vfield(field: int, value: int) -> bytes:
    return tag(field, VARINT) + varint(value)


def ld(field: int, payload: bytes | str) -> bytes:
    data = payload.encode() if isinstance(payload, str) else payload
    return tag(field, LEN) + varint(len(data)) + data


def f64(field: int, value: int) -> bytes:
    return tag(field, I64) + struct.pack("<Q", value)


def dbl(field: int, value: float) -> bytes:
    return tag(field, I64) + struct.pack("<d", value)


def any_str(value: str) -> bytes:
    return ld(1, value)


def any_bool(value: bool) -> bytes:
    return vfield(2, int(value))


def any_int(value: int) -> bytes:
    return vfield(3, value)


def any_double(value: float) -> bytes:
    return dbl(4, value)


def any_bytes(value: bytes) -> bytes:
    return ld(7, value)


def any_array(*values: bytes) -> bytes:
    return ld(5, b"".join(ld(1, v) for v in values))


def kv(key: str, any_value: bytes) -> bytes:
    return ld(1, key) + ld(2, any_value)


def parse(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """Decode one message level into ``(field, wire_type, value)`` triples."""
    out: list[tuple[int, int, int | bytes]] = []
    i = 0

    def read_varint() -> int:
        nonlocal i
        shift = result = 0
        while True:
            byte = buf[i]
            i += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return result

    while i < len(buf):
        key = read_varint()
        field, wire = key >> 3, key & 7
        value: int | bytes
        if wire == VARINT:
            value = read_varint()
        elif wire == I64:
            value = buf[i : i + 8]
            i += 8
        elif wire == LEN:
            n = read_varint()
            value = buf[i : i + n]
            i += n
        elif wire == I32:
            value = buf[i : i + 4]
            i += 4
        else:
            raise ValueError(f"bad wire type {wire}")
        out.append((field, wire, value))
    if i != len(buf):
        raise ValueError("trailing bytes")
    return out
