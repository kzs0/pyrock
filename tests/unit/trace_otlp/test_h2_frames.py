"""HTTP/2 frame encoding/decoding seams of ``pyrock.trace.otlp._h2`` (RFC 9113 §4, §6)."""

from __future__ import annotations

import struct

import pytest

from pyrock.trace.otlp import _h2
from pyrock.trace.otlp._h2 import (
    FLAG_ACK,
    FLAG_END_HEADERS,
    FLAG_END_STREAM,
    FLAG_PADDED,
    FLAG_PRIORITY,
    FRAME_CONTINUATION,
    FRAME_DATA,
    FRAME_GOAWAY,
    FRAME_HEADERS,
    FRAME_PING,
    FRAME_RST_STREAM,
    FRAME_SETTINGS,
    FRAME_WINDOW_UPDATE,
    Frame,
    H2Error,
    H2Response,
    ProtocolError,
    StreamReset,
    decode_frame,
    decode_goaway,
    decode_rst_stream,
    decode_settings,
    decode_window_update,
    encode_frame,
    encode_goaway,
    encode_header_block_frames,
    encode_rst_stream,
    encode_settings,
    encode_window_update,
    frame_body,
)


def _header(length: int, ftype: int, flags: int, stream: int) -> bytes:
    return length.to_bytes(3, "big") + bytes([ftype, flags]) + stream.to_bytes(4, "big")


# ------------------------------------------------------------- constants


def test_wire_constants() -> None:
    assert _h2.CONNECTION_PREFACE == b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
    assert (FRAME_DATA, FRAME_HEADERS, FRAME_RST_STREAM, FRAME_SETTINGS) == (0, 1, 3, 4)
    assert (FRAME_PING, FRAME_GOAWAY, FRAME_WINDOW_UPDATE, FRAME_CONTINUATION) == (6, 7, 8, 9)
    assert (FLAG_END_STREAM, FLAG_ACK, FLAG_END_HEADERS, FLAG_PADDED, FLAG_PRIORITY) == (
        1,
        1,
        4,
        8,
        0x20,
    )
    assert _h2.DEFAULT_WINDOW_SIZE == 65535
    assert _h2.DEFAULT_MAX_FRAME_SIZE == 16384


def test_error_hierarchy() -> None:
    assert issubclass(ProtocolError, H2Error)
    assert issubclass(StreamReset, H2Error)
    reset = StreamReset(8)
    assert reset.error_code == 8
    assert "8" in str(reset)


def test_h2_response_header_lookup() -> None:
    response = H2Response(
        200,
        headers=[(":status", "200"), ("content-type", "application/grpc")],
        body=b"x",
        trailers=[("grpc-status", "0"), ("Grpc-Message", "ok")],
    )
    assert response.header("Content-Type") == "application/grpc"
    assert response.header("grpc-status") == "0"
    assert response.header("grpc-message") == "ok"
    assert response.header("missing") is None


# ---------------------------------------------------------------- frames


def test_encode_frame_header_layout() -> None:
    data = encode_frame(FRAME_DATA, FLAG_END_STREAM, 3, b"hello")
    assert data == _header(5, 0, 1, 3) + b"hello"


def test_encode_frame_empty_payload() -> None:
    assert encode_frame(FRAME_SETTINGS, FLAG_ACK, 0) == _header(0, 4, 1, 0)


def test_encode_frame_large_length_and_stream() -> None:
    payload = b"x" * 70000
    data = encode_frame(FRAME_DATA, 0, 2**31 - 1, payload)
    assert data[:3] == (70000).to_bytes(3, "big")
    assert data[5:9] == b"\x7f\xff\xff\xff"
    assert data[9:] == payload


@pytest.mark.parametrize(("stream", "payload"), [(2**31, b""), (-1, b""), (1, b"x" * (2**24))])
def test_encode_frame_rejects_out_of_range(stream: int, payload: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        encode_frame(FRAME_DATA, 0, stream, payload)


def test_decode_frame_round_trip() -> None:
    data = encode_frame(FRAME_HEADERS, FLAG_END_HEADERS, 5, b"abc")
    assert decode_frame(data) == (Frame(FRAME_HEADERS, FLAG_END_HEADERS, 5, b"abc"), 12)


def test_decode_frame_incomplete_returns_none() -> None:
    data = encode_frame(FRAME_DATA, 0, 1, b"0123456789")
    for cut in (0, 1, 8, 9, 18):
        assert decode_frame(data[:cut]) is None


def test_decode_frame_consumes_only_first_frame() -> None:
    first = encode_frame(FRAME_PING, 0, 0, b"12345678")
    second = encode_frame(FRAME_DATA, 0, 1, b"z")
    frame, consumed = decode_frame(first + second)  # type: ignore[misc]
    assert frame == Frame(FRAME_PING, 0, 0, b"12345678")
    assert consumed == len(first)
    assert decode_frame((first + second)[consumed:]) == (Frame(FRAME_DATA, 0, 1, b"z"), 10)


def test_decode_frame_ignores_reserved_bit() -> None:
    data = _header(1, FRAME_DATA, 0, 0x80000003) + b"x"
    result = decode_frame(data)
    assert result is not None
    assert result[0].stream_id == 3


def test_decode_frame_rejects_oversized_frame() -> None:
    data = _header(16385, FRAME_DATA, 0, 1)
    with pytest.raises(ProtocolError):
        decode_frame(data)
    big = encode_frame(FRAME_DATA, 0, 1, b"x" * 20000)
    assert decode_frame(big, max_frame_size=20000) == (
        Frame(FRAME_DATA, 0, 1, b"x" * 20000),
        len(big),
    )


def test_decode_frame_unknown_type_is_returned() -> None:
    data = encode_frame(0xEE, 0x5A, 0, b"??")
    assert decode_frame(data) == (Frame(0xEE, 0x5A, 0, b"??"), 11)


# --------------------------------------------------- padding / priority


def test_frame_body_plain() -> None:
    assert frame_body(Frame(FRAME_DATA, 0, 1, b"data")) == b"data"
    assert frame_body(Frame(FRAME_HEADERS, FLAG_END_HEADERS, 1, b"\x88")) == b"\x88"


def test_frame_body_strips_data_padding() -> None:
    payload = bytes([3]) + b"data" + b"\x00\x00\x00"
    assert frame_body(Frame(FRAME_DATA, FLAG_PADDED, 1, payload)) == b"data"


def test_frame_body_zero_padding_length() -> None:
    assert frame_body(Frame(FRAME_DATA, FLAG_PADDED, 1, b"\x00data")) == b"data"


def test_frame_body_padded_empty_data() -> None:
    assert frame_body(Frame(FRAME_DATA, FLAG_PADDED, 1, b"\x02\x00\x00")) == b""
    assert frame_body(Frame(FRAME_DATA, FLAG_PADDED, 1, b"\x03abc")) == b""


def test_frame_body_strips_headers_priority() -> None:
    priority = struct.pack(">IB", 0x80000001, 15)
    frame = Frame(FRAME_HEADERS, FLAG_END_HEADERS | FLAG_PRIORITY, 3, priority + b"\x88")
    assert frame_body(frame) == b"\x88"


def test_frame_body_strips_headers_padding_and_priority() -> None:
    priority = struct.pack(">IB", 1, 0)
    payload = bytes([2]) + priority + b"\x88\x89" + b"\x00\x00"
    frame = Frame(FRAME_HEADERS, FLAG_PADDED | FLAG_PRIORITY | FLAG_END_HEADERS, 3, payload)
    assert frame_body(frame) == b"\x88\x89"


@pytest.mark.parametrize(
    "frame",
    [
        Frame(FRAME_DATA, FLAG_PADDED, 1, b""),
        Frame(FRAME_DATA, FLAG_PADDED, 1, b"\x05abc"),
        Frame(FRAME_HEADERS, FLAG_PRIORITY, 1, b"\x00\x00"),
        Frame(FRAME_HEADERS, FLAG_PADDED | FLAG_PRIORITY, 1, b"\x01\x00\x00\x00\x01\x10"),
    ],
)
def test_frame_body_rejects_bad_padding(frame: Frame) -> None:
    with pytest.raises(ProtocolError):
        frame_body(frame)


def test_frame_body_other_types_unchanged() -> None:
    assert frame_body(Frame(FRAME_PING, FLAG_PADDED, 0, b"\x01" * 8)) == b"\x01" * 8


# ------------------------------------------------ HEADERS + CONTINUATION


def _frames(data: bytes, max_frame_size: int = 16384) -> list[Frame]:
    out = []
    while data:
        result = decode_frame(data, max_frame_size=max_frame_size)
        assert result is not None
        frame, consumed = result
        out.append(frame)
        data = data[consumed:]
    return out


def test_header_block_fits_in_one_frame() -> None:
    frames = _frames(encode_header_block_frames(1, b"abc"))
    assert frames == [Frame(FRAME_HEADERS, FLAG_END_HEADERS, 1, b"abc")]


def test_header_block_with_end_stream() -> None:
    frames = _frames(encode_header_block_frames(1, b"abc", end_stream=True))
    assert frames == [Frame(FRAME_HEADERS, FLAG_END_HEADERS | FLAG_END_STREAM, 1, b"abc")]


def test_header_block_split_into_continuations() -> None:
    block = bytes(range(256)) * 10  # 2560 bytes
    frames = _frames(encode_header_block_frames(7, block, max_frame_size=1000, end_stream=True))
    assert [f.type for f in frames] == [FRAME_HEADERS, FRAME_CONTINUATION, FRAME_CONTINUATION]
    assert [len(f.payload) for f in frames] == [1000, 1000, 560]
    assert all(f.stream_id == 7 for f in frames)
    assert frames[0].flags == FLAG_END_STREAM
    assert frames[1].flags == 0
    assert frames[2].flags == FLAG_END_HEADERS
    assert b"".join(f.payload for f in frames) == block


def test_header_block_exact_multiple_has_no_empty_continuation() -> None:
    frames = _frames(encode_header_block_frames(1, b"x" * 200, max_frame_size=100))
    assert [len(f.payload) for f in frames] == [100, 100]
    assert frames[-1].flags & FLAG_END_HEADERS


# --------------------------------------------------------------- payloads


def test_settings_payload() -> None:
    payload = encode_settings(
        [(_h2.SETTINGS_ENABLE_PUSH, 0), (_h2.SETTINGS_INITIAL_WINDOW_SIZE, 2**20)]
    )
    assert payload == struct.pack(">HIHI", 2, 0, 4, 2**20)
    assert decode_settings(payload) == [(2, 0), (4, 2**20)]
    assert encode_settings([]) == b""
    assert decode_settings(b"") == []


def test_settings_unknown_identifiers_are_kept() -> None:
    payload = struct.pack(">HI", 0xABCD, 7)
    assert decode_settings(payload) == [(0xABCD, 7)]


@pytest.mark.parametrize("size", [1, 5, 7, 13])
def test_settings_bad_length(size: int) -> None:
    with pytest.raises(ProtocolError):
        decode_settings(b"\x00" * size)


def test_window_update_payload() -> None:
    assert encode_window_update(1) == b"\x00\x00\x00\x01"
    assert encode_window_update(2**31 - 1) == b"\x7f\xff\xff\xff"
    assert decode_window_update(b"\x00\x01\x00\x00") == 65536
    assert decode_window_update(b"\x80\x00\x00\x05") == 5  # reserved bit ignored


@pytest.mark.parametrize("increment", [0, -1, 2**31])
def test_window_update_rejects_bad_increment(increment: int) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        encode_window_update(increment)


@pytest.mark.parametrize("payload", [b"\x00\x00\x00\x00", b"\x80\x00\x00\x00", b"\x00\x01", b""])
def test_window_update_decode_errors(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_window_update(payload)


def test_goaway_payload() -> None:
    payload = encode_goaway(5, 2, b"debug")
    assert payload == struct.pack(">II", 5, 2) + b"debug"
    assert decode_goaway(payload) == (5, 2, b"debug")
    assert decode_goaway(encode_goaway(0, 0)) == (0, 0, b"")
    assert decode_goaway(b"\x80\x00\x00\x03" + b"\x00\x00\x00\x00") == (3, 0, b"")


def test_goaway_too_short() -> None:
    with pytest.raises(ProtocolError):
        decode_goaway(b"\x00" * 7)


def test_rst_stream_payload() -> None:
    assert encode_rst_stream(8) == b"\x00\x00\x00\x08"
    assert decode_rst_stream(b"\x00\x00\x00\x07") == 7


@pytest.mark.parametrize("payload", [b"", b"\x00\x00\x00", b"\x00" * 5])
def test_rst_stream_bad_length(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_rst_stream(payload)
