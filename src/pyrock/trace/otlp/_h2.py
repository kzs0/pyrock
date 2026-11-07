"""A deliberately small HTTP/2 client for unary gRPC calls.

* One long-lived connection: TLS with ALPN ``h2``, or h2c prior-knowledge when
  insecure.
* Sends SETTINGS, HEADERS, DATA, WINDOW_UPDATE, PING, RST_STREAM and GOAWAY;
  reads trailers.
* HPACK encoding uses only literal-without-indexing, no Huffman. Decoding
  supports the static table, the dynamic table and Huffman.

Requests on one connection are serialised by a lock: the calling thread writes
its request and then reads frames until its own response is complete. The
exporter issues one call at a time, so multiplexing would add a reader thread
and per-stream wake-ups without making anything faster. Frames that arrive for
a stream abandoned after a timeout are still accounted for (connection flow
control, HPACK state) and otherwise ignored.
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

__all__ = [
    "H2Connection",
    "H2Error",
    "H2Response",
    "HPACKDecoder",
    "ProtocolError",
    "StreamReset",
    "encode_headers",
    "huffman_decode",
]

Header = tuple[str, str]

# ------------------------------------------------------------ wire constants
# Internal seams (not in ``__all__``): RFC 9113 frame types, flags, SETTINGS
# identifiers and error codes, used by the connection and by unit tests.

CONNECTION_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
FRAME_HEADER_SIZE = 9
DEFAULT_WINDOW_SIZE = 65535
DEFAULT_MAX_FRAME_SIZE = 16384
MAX_WINDOW_SIZE = 2**31 - 1

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_PRIORITY = 0x2
FRAME_RST_STREAM = 0x3
FRAME_SETTINGS = 0x4
FRAME_PUSH_PROMISE = 0x5
FRAME_PING = 0x6
FRAME_GOAWAY = 0x7
FRAME_WINDOW_UPDATE = 0x8
FRAME_CONTINUATION = 0x9

FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20

SETTINGS_HEADER_TABLE_SIZE = 0x1
SETTINGS_ENABLE_PUSH = 0x2
SETTINGS_MAX_CONCURRENT_STREAMS = 0x3
SETTINGS_INITIAL_WINDOW_SIZE = 0x4
SETTINGS_MAX_FRAME_SIZE = 0x5
SETTINGS_MAX_HEADER_LIST_SIZE = 0x6

ERROR_NO_ERROR = 0x0
ERROR_PROTOCOL = 0x1
ERROR_INTERNAL = 0x2
ERROR_FLOW_CONTROL = 0x3
ERROR_SETTINGS_TIMEOUT = 0x4
ERROR_STREAM_CLOSED = 0x5
ERROR_FRAME_SIZE = 0x6
ERROR_REFUSED_STREAM = 0x7
ERROR_CANCEL = 0x8
ERROR_COMPRESSION = 0x9

MAX_STREAM_ID = 2**31 - 1
_MAX_FRAME_LENGTH = 2**24 - 1
_PRIORITY_FIELDS_SIZE = 5
_PING_SIZE = 8

# Local safety limits on what a peer can make us buffer.
_MAX_HEADER_BLOCK = 64 * 1024
_MAX_RESPONSE_BODY = 16 * 1024 * 1024
# Bound on blocking writes of control frames (acks, RST_STREAM, GOAWAY), which
# must go out even when the caller's own deadline has already passed.
_CONTROL_TIMEOUT = 1.0
_READ_SIZE = 65536


class H2Error(Exception):
    """Base HTTP/2 error (connection failure, timeout, GOAWAY)."""


class ProtocolError(H2Error):
    """The peer violated HTTP/2 or HPACK.

    ``error_code`` is the RFC 9113 code sent in our GOAWAY when the violation
    ends the connection.
    """

    def __init__(self, message: str, error_code: int = ERROR_PROTOCOL) -> None:
        self.error_code = error_code
        super().__init__(message)


class StreamReset(H2Error):
    """The peer reset the stream (RST_STREAM)."""

    def __init__(self, error_code: int) -> None:
        self.error_code = error_code
        super().__init__(f"stream reset by peer (error code {error_code})")


class _StreamError(H2Error):
    """A local failure confined to one stream (timeout, oversized response).

    The stream is reset with CANCEL and the connection stays usable.
    """


@dataclass(frozen=True, slots=True)
class H2Response:
    """A completed unary response."""

    status: int
    headers: Sequence[Header] = field(default_factory=tuple)
    body: bytes = b""
    trailers: Sequence[Header] = field(default_factory=tuple)

    def header(self, name: str, /) -> str | None:
        """First value of ``name`` in headers, then trailers (case-insensitive)."""
        lname = name.lower()
        for key, value in (*self.headers, *self.trailers):
            if key.lower() == lname:
                return value
        return None


# ------------------------------------------------------------------ frames


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded HTTP/2 frame (internal seam). ``payload`` is the raw frame payload."""

    type: int
    flags: int
    stream_id: int
    payload: bytes = b""


def encode_frame(frame_type: int, flags: int, stream_id: int, payload: bytes = b"", /) -> bytes:
    """Serialise one frame: 9-byte header (24-bit length, type, flags, 31-bit stream) + payload.

    The reserved stream-ID bit is always written as 0. ``ValueError`` when the
    payload exceeds 2**24 - 1 bytes or the stream ID exceeds 2**31 - 1.
    """
    if not 0 <= stream_id <= MAX_STREAM_ID:
        raise ValueError(f"stream ID out of range: {stream_id}")
    if len(payload) > _MAX_FRAME_LENGTH:
        raise ValueError(f"frame payload too large: {len(payload)} bytes")
    header = len(payload).to_bytes(3, "big") + bytes((frame_type, flags))
    return header + stream_id.to_bytes(4, "big") + payload


def decode_frame(
    buffer: bytes, /, *, max_frame_size: int = DEFAULT_MAX_FRAME_SIZE
) -> tuple[Frame, int] | None:
    """Decode the first frame in ``buffer``.

    Returns ``(frame, consumed_bytes)``, or ``None`` when ``buffer`` does not
    yet hold a complete frame. The reserved stream-ID bit is ignored. Raises
    :class:`ProtocolError` when the declared length exceeds ``max_frame_size``.
    """
    if len(buffer) < FRAME_HEADER_SIZE:
        return None
    length = int.from_bytes(buffer[:3], "big")
    if length > max_frame_size:
        raise ProtocolError(f"frame of {length} bytes exceeds the maximum", ERROR_FRAME_SIZE)
    end = FRAME_HEADER_SIZE + length
    if len(buffer) < end:
        return None
    stream_id = int.from_bytes(buffer[5:9], "big") & MAX_STREAM_ID
    return Frame(buffer[3], buffer[4], stream_id, bytes(buffer[FRAME_HEADER_SIZE:end])), end


def frame_body(frame: Frame, /) -> bytes:
    """Return the data of a DATA frame or the header-block fragment of a HEADERS frame.

    Strips padding (``FLAG_PADDED``) and, for HEADERS, the priority fields
    (``FLAG_PRIORITY``). Other frame types return the payload unchanged.
    Raises :class:`ProtocolError` when the payload is too short for the pad
    length byte, the priority fields and the declared padding (RFC 9113 §6.1).
    """
    payload = frame.payload
    if frame.type not in (FRAME_DATA, FRAME_HEADERS):
        return payload
    start = pad = 0
    if frame.flags & FLAG_PADDED:
        if not payload:
            raise ProtocolError("padded frame without a pad length")
        pad = payload[0]
        start = 1
    if frame.type == FRAME_HEADERS and frame.flags & FLAG_PRIORITY:
        start += _PRIORITY_FIELDS_SIZE
    end = len(payload) - pad
    if end < start:
        raise ProtocolError("padding exceeds the frame payload")
    return payload[start:end]


def encode_header_block_frames(
    stream_id: int,
    block: bytes,
    /,
    *,
    max_frame_size: int = DEFAULT_MAX_FRAME_SIZE,
    end_stream: bool = False,
) -> bytes:
    """Serialise a header block as one HEADERS frame plus CONTINUATION frames.

    No frame payload exceeds ``max_frame_size``. ``FLAG_END_HEADERS`` is set
    only on the last frame; ``FLAG_END_STREAM`` (when ``end_stream``) only on
    the HEADERS frame.
    """
    chunks = [block[i : i + max_frame_size] for i in range(0, len(block), max_frame_size)]
    chunks = chunks or [b""]
    frames = []
    for index, chunk in enumerate(chunks):
        flags = FLAG_END_HEADERS if index == len(chunks) - 1 else 0
        if index == 0:
            flags |= FLAG_END_STREAM if end_stream else 0
            frames.append(encode_frame(FRAME_HEADERS, flags, stream_id, chunk))
        else:
            frames.append(encode_frame(FRAME_CONTINUATION, flags, stream_id, chunk))
    return b"".join(frames)


def encode_settings(params: Sequence[tuple[int, int]], /) -> bytes:
    """Serialise a SETTINGS payload (6 bytes per ``(identifier, value)``)."""
    return b"".join(struct.pack(">HI", key, value) for key, value in params)


def decode_settings(payload: bytes, /) -> list[tuple[int, int]]:
    """Parse a SETTINGS payload. :class:`ProtocolError` if its length is not a multiple of 6."""
    if len(payload) % 6:
        raise ProtocolError("SETTINGS length is not a multiple of 6", ERROR_FRAME_SIZE)
    return list(struct.iter_unpack(">HI", payload))


def encode_window_update(increment: int, /) -> bytes:
    """Serialise a WINDOW_UPDATE payload. ``ValueError`` unless ``1 <= increment < 2**31``."""
    if not 1 <= increment <= MAX_WINDOW_SIZE:
        raise ValueError(f"window increment out of range: {increment}")
    return increment.to_bytes(4, "big")


def decode_window_update(payload: bytes, /) -> int:
    """Parse a WINDOW_UPDATE payload (reserved bit ignored).

    :class:`ProtocolError` when the payload is not 4 bytes or the increment is 0.
    """
    if len(payload) != 4:
        raise ProtocolError("WINDOW_UPDATE payload is not 4 bytes", ERROR_FRAME_SIZE)
    increment = int.from_bytes(payload, "big") & MAX_WINDOW_SIZE
    if not increment:
        raise ProtocolError("WINDOW_UPDATE with a zero increment")
    return increment


def encode_goaway(last_stream_id: int, error_code: int, debug_data: bytes = b"", /) -> bytes:
    """Serialise a GOAWAY payload."""
    return struct.pack(">II", last_stream_id, error_code) + debug_data


def decode_goaway(payload: bytes, /) -> tuple[int, int, bytes]:
    """Parse a GOAWAY payload into ``(last_stream_id, error_code, debug_data)``.

    :class:`ProtocolError` when the payload is shorter than 8 bytes.
    """
    if len(payload) < 8:
        raise ProtocolError("GOAWAY payload shorter than 8 bytes", ERROR_FRAME_SIZE)
    last_stream_id, error_code = struct.unpack_from(">II", payload)
    return last_stream_id & MAX_STREAM_ID, error_code, payload[8:]


def encode_rst_stream(error_code: int, /) -> bytes:
    """Serialise an RST_STREAM payload."""
    return error_code.to_bytes(4, "big")


def decode_rst_stream(payload: bytes, /) -> int:
    """Parse an RST_STREAM payload. :class:`ProtocolError` unless it is 4 bytes."""
    if len(payload) != 4:
        raise ProtocolError("RST_STREAM payload is not 4 bytes", ERROR_FRAME_SIZE)
    return int.from_bytes(payload, "big")


# ------------------------------------------------------------------- HPACK

# Continuation bytes carry 7 bits each; more than five (35 bits) is never a
# plausible length or index and only serves to make us do big-int arithmetic.
_MAX_INTEGER_SHIFT = 28


def encode_integer(value: int, prefix_bits: int, /, first_byte: int = 0) -> bytes:
    """HPACK integer (RFC 7541 §5.1) with an N-bit prefix.

    ``first_byte`` carries the representation's high-order flag bits.
    """
    limit = (1 << prefix_bits) - 1
    if value < limit:
        return bytes((first_byte | value,))
    out = bytearray((first_byte | limit,))
    value -= limit
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_integer(data: bytes, offset: int, prefix_bits: int, /) -> tuple[int, int]:
    """Decode an HPACK integer at ``offset``; returns ``(value, next_offset)``.

    :class:`ProtocolError` on truncation or an implausibly large value.
    """
    if offset >= len(data):
        raise ProtocolError("HPACK integer truncated")
    limit = (1 << prefix_bits) - 1
    value = data[offset] & limit
    offset += 1
    if value < limit:
        return value, offset
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > _MAX_INTEGER_SHIFT:
            raise ProtocolError("HPACK integer too large")
    raise ProtocolError("HPACK integer truncated")


def encode_headers(headers: Sequence[Header], /) -> bytes:
    """HPACK-encode headers as literal-without-indexing, no Huffman.

    Never touching the peer's dynamic table keeps the encoder stateless, so
    the peer's SETTINGS_HEADER_TABLE_SIZE never matters to us.
    """
    out = bytearray()
    for name, value in headers:
        out.append(0x00)  # literal without indexing, new name
        for text in (name, value):
            data = text.encode()
            out += encode_integer(len(data), 7)
            out += data
    return bytes(out)


# Code length in bits of each symbol 0-255 and EOS (256), RFC 7541 Appendix B.
# The code is canonical: sorting symbols by (length, symbol) and counting up
# reproduces every code, so the lengths are the whole table.
_HUFFMAN_LENGTHS = bytes.fromhex(
    "0d171c1c1c1c1c1c1c181e1c1c1e1c1c1c1c1c1c1c1c1e1c1c1c1c1c1c1c1c1c060a0a0c0d06080b"
    "0a0a080b080606060505050606060606060607080f060c0a0d060707070707070707070707070707"
    "07070707070707070807080d130d0e060f0506050605060606050707060606050607060505060707"
    "0707070f0b0e0d1c1416141416161617161717171717181718181617181717171715161716171718"
    "16151416161717151716161815161717151516151716171714161616171616171a1a141316171619"
    "1a1a1a1b1b1a181913151a1b1b1a1b1815151a1a1c1b1b1b14181415161515171616191918181a17"
    "1a1b1a1a1b1b1b1b1b1c1b1b1b1b1b1a1e"
)
_HUFFMAN_EOS = 256
_HUFFMAN_MAX_LENGTH = 30


def _canonical_tables() -> tuple[list[int], list[int], list[int], list[int]]:
    """Per code length: first code, symbol count and offset into the symbol list.

    A ``length``-bit code ``c`` is symbol ``symbols[offset[length] + c - first[length]]``
    exactly when ``0 <= c - first[length] < count[length]``.
    """
    symbols = sorted(range(len(_HUFFMAN_LENGTHS)), key=lambda s: (_HUFFMAN_LENGTHS[s], s))
    size = _HUFFMAN_MAX_LENGTH + 1
    count = [0] * size
    for length in _HUFFMAN_LENGTHS:
        count[length] += 1
    first, offset = [0] * size, [0] * size
    code = index = 0
    for length in range(1, size):
        code = (code + count[length - 1]) << 1
        first[length], offset[length] = code, index
        index += count[length]
    return symbols, first, count, offset


_HUFFMAN_SYMBOLS, _HUFFMAN_FIRST, _HUFFMAN_COUNT, _HUFFMAN_OFFSET = _canonical_tables()


def huffman_decode(data: bytes, /) -> bytes:
    """Decode an HPACK Huffman string (RFC 7541 Appendix B). Raises :class:`ProtocolError`.

    The string must end with at most 7 bits of padding, all ones (a prefix of
    EOS); EOS itself must never appear (RFC 7541 §5.2).
    """
    out = bytearray()
    code = length = 0
    for byte in data:
        for shift in range(7, -1, -1):
            code = code << 1 | (byte >> shift) & 1
            length += 1
            index = code - _HUFFMAN_FIRST[length]
            if 0 <= index < _HUFFMAN_COUNT[length]:
                symbol = _HUFFMAN_SYMBOLS[_HUFFMAN_OFFSET[length] + index]
                if symbol == _HUFFMAN_EOS:
                    raise ProtocolError("Huffman string contains EOS")
                out.append(symbol)
                code = length = 0
    if length > 7 or code != (1 << length) - 1:
        raise ProtocolError("invalid Huffman padding")
    return bytes(out)


def _static_table() -> tuple[tuple[bytes, bytes], ...]:
    """The 61-entry HPACK static table (RFC 7541 Appendix A)."""
    entries = [
        (":authority", ""),
        (":method", "GET"),
        (":method", "POST"),
        (":path", "/"),
        (":path", "/index.html"),
        (":scheme", "http"),
        (":scheme", "https"),
        *((":status", code) for code in ("200", "204", "206", "304", "400", "404", "500")),
        ("accept-charset", ""),
        ("accept-encoding", "gzip, deflate"),
    ]
    names = (
        "accept-language accept-ranges accept access-control-allow-origin age allow"
        " authorization cache-control content-disposition content-encoding content-language"
        " content-length content-location content-range content-type cookie date etag expect"
        " expires from host if-match if-modified-since if-none-match if-range"
        " if-unmodified-since last-modified link location max-forwards proxy-authenticate"
        " proxy-authorization range referer refresh retry-after server set-cookie"
        " strict-transport-security transfer-encoding user-agent vary via www-authenticate"
    )
    entries += [(name, "") for name in names.split()]
    return tuple((name.encode(), value.encode()) for name, value in entries)


_STATIC_TABLE = _static_table()
_ENTRY_OVERHEAD = 32


def _entry_size(name: bytes, value: bytes) -> int:
    return len(name) + len(value) + _ENTRY_OVERHEAD


def _text(data: bytes) -> str:
    """Field bytes as text. HTTP allows obs-text, so invalid UTF-8 is replaced, not fatal."""
    return data.decode(errors="replace")


class HPACKDecoder:
    """Stateful HPACK decoder (static + dynamic table, Huffman).

    Not thread-safe; the connection decodes every header block, in order, under
    its lock. Entries are kept as raw octets so table sizes are exact.
    """

    def __init__(self, max_table_size: int = 4096) -> None:
        self._limit = max_table_size
        self._max_size = max_table_size
        self._size = 0
        self._table: list[tuple[bytes, bytes]] = []

    def decode(self, block: bytes, /) -> list[Header]:
        """Decode one complete header block. Raises :class:`ProtocolError`."""
        fields: list[tuple[bytes, bytes]] = []
        offset = 0
        while offset < len(block):
            byte = block[offset]
            if byte & 0x80:  # indexed field
                index, offset = decode_integer(block, offset, 7)
                fields.append(self._entry(index))
            elif byte & 0x40:  # literal with incremental indexing
                name, value, offset = self._literal(block, offset, 6)
                self._insert(name, value)
                fields.append((name, value))
            elif byte & 0x20:  # dynamic table size update
                if fields:
                    raise ProtocolError("table size update after a header field")
                size, offset = decode_integer(block, offset, 5)
                self._resize(size)
            else:  # literal without indexing (0000) or never indexed (0001)
                name, value, offset = self._literal(block, offset, 4)
                fields.append((name, value))
        return [(_text(name), _text(value)) for name, value in fields]

    @property
    def max_table_size(self) -> int:
        """The current dynamic table size limit (changed by size updates in a block)."""
        return self._max_size

    @property
    def table_size(self) -> int:
        """Current dynamic table size in RFC 7541 octets (name + value + 32 per entry)."""
        return self._size

    @property
    def dynamic_table(self) -> list[Header]:
        """Dynamic table entries, newest first (index 62 is element 0)."""
        return [(_text(name), _text(value)) for name, value in self._table]

    def set_max_table_size(self, size: int, /) -> None:
        """Set the protocol limit (our SETTINGS_HEADER_TABLE_SIZE); evicts as needed.

        Size updates in later blocks may not exceed it (:class:`ProtocolError`).
        """
        self._limit = size
        self._max_size = min(self._max_size, size)
        self._evict(self._max_size)

    def _entry(self, index: int) -> tuple[bytes, bytes]:
        if 1 <= index <= len(_STATIC_TABLE):
            return _STATIC_TABLE[index - 1]
        dynamic = index - len(_STATIC_TABLE) - 1
        if 0 <= dynamic < len(self._table):
            return self._table[dynamic]
        raise ProtocolError(f"HPACK index {index} out of range")

    def _literal(self, block: bytes, offset: int, prefix_bits: int) -> tuple[bytes, bytes, int]:
        index, offset = decode_integer(block, offset, prefix_bits)
        if index:
            name = self._entry(index)[0]
        else:
            name, offset = self._string(block, offset)
        value, offset = self._string(block, offset)
        return name, value, offset

    @staticmethod
    def _string(block: bytes, offset: int) -> tuple[bytes, int]:
        if offset >= len(block):
            raise ProtocolError("HPACK string missing")
        huffman = block[offset] & 0x80
        length, offset = decode_integer(block, offset, 7)
        end = offset + length
        if end > len(block):
            raise ProtocolError("HPACK string truncated")
        data = block[offset:end]
        return (huffman_decode(data) if huffman else data), end

    def _resize(self, size: int) -> None:
        if size > self._limit:
            raise ProtocolError(f"table size update {size} exceeds the limit {self._limit}")
        self._max_size = size
        self._evict(size)

    def _insert(self, name: bytes, value: bytes) -> None:
        """Add an entry, evicting the oldest first; one larger than the table empties it."""
        size = _entry_size(name, value)
        self._evict(self._max_size - size)
        if size <= self._max_size:
            self._table.insert(0, (name, value))
            self._size += size

    def _evict(self, target: int) -> None:
        while self._table and self._size > target:
            self._size -= _entry_size(*self._table.pop())


# -------------------------------------------------------------- connection


def _deadline(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + timeout


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _status(fields: Sequence[Header]) -> int:
    value = next((v for k, v in fields if k == ":status"), "")
    if len(value) != 3 or not (value.isascii() and value.isdigit()):
        raise ProtocolError(f"invalid :status {value!r}")
    return int(value)


@dataclass(slots=True)
class _Stream:
    """The one in-flight request stream."""

    id: int
    send_window: int
    recv_window: int = DEFAULT_WINDOW_SIZE
    status: int = 0
    headers: list[Header] | None = None
    trailers: list[Header] = field(default_factory=list)
    body: bytearray = field(default_factory=bytearray)
    ended: bool = False

    def response(self) -> H2Response:
        return H2Response(
            self.status, tuple(self.headers or ()), bytes(self.body), tuple(self.trailers)
        )


@dataclass(slots=True)
class _PendingBlock:
    """A header block whose HEADERS frame lacked END_HEADERS (CONTINUATION follows)."""

    stream_id: int
    end_stream: bool
    data: bytearray


class H2Connection:
    """A single HTTP/2 connection for sequential or concurrent unary requests. Thread-safe.

    Connects in the constructor. Concurrent callers are served one at a time.
    After any connection-level failure, a GOAWAY from the peer or :meth:`close`,
    :attr:`closed` is true and the caller should open a new connection.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        """``ssl_context=None`` means h2c prior-knowledge (plaintext).

        A given context gets ALPN ``h2`` set on it. :class:`H2Error` when the
        connection, TLS handshake or ALPN negotiation fails.
        """
        self._lock = threading.Lock()
        self._scheme = "http" if ssl_context is None else "https"
        self._authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self._buffer = b""
        self._decoder = HPACKDecoder()
        self._next_stream_id = 1
        self._send_window = DEFAULT_WINDOW_SIZE
        self._recv_window = DEFAULT_WINDOW_SIZE
        self._peer_initial_window = DEFAULT_WINDOW_SIZE
        self._peer_max_frame = DEFAULT_MAX_FRAME_SIZE
        self._draining = False
        self._stream: _Stream | None = None
        self._pending: _PendingBlock | None = None
        self._ping: bytes | None = None
        self._handlers: dict[int, Callable[[Frame], None]] = {
            FRAME_DATA: self._on_data,
            FRAME_HEADERS: self._on_headers,
            FRAME_RST_STREAM: self._on_rst_stream,
            FRAME_SETTINGS: self._on_settings,
            FRAME_PUSH_PROMISE: self._on_push_promise,
            FRAME_PING: self._on_ping,
            FRAME_GOAWAY: self._on_goaway,
            FRAME_WINDOW_UPDATE: self._on_window_update,
            FRAME_CONTINUATION: self._on_continuation,
        }
        self._sock: socket.socket | None = _connect(
            host, port, ssl_context, server_hostname or host, connect_timeout
        )

    def request(
        self,
        method: str,
        path: str,
        headers: Sequence[Header],
        body: bytes,
        /,
        *,
        timeout: float | None = None,
    ) -> H2Response:
        """Send one request and wait for the full response and trailers.

        ``timeout`` (``None``: wait indefinitely) covers waiting for the
        connection, sending and receiving. Header names are lowercased. Raises
        :class:`StreamReset` when the peer resets the stream, and
        :class:`H2Error` on timeout or connection failure; a timeout resets
        only this stream.
        """
        deadline = _deadline(timeout)
        with self._exclusive(deadline), self._connection_errors():
            stream = self._open_stream()
            try:
                self._send_request(stream, method, path, headers, body, deadline)
                self._read_until(lambda: stream.ended, deadline)
            except _StreamError:
                self._send_control(FRAME_RST_STREAM, 0, stream.id, encode_rst_stream(ERROR_CANCEL))
                raise
            finally:
                self._stream = None
            if self._draining:
                self._close_socket()
            return stream.response()

    def ping(self, *, timeout: float | None = None) -> None:
        """Send a PING and wait for its acknowledgement. :class:`H2Error` on failure."""
        deadline = _deadline(timeout)
        with self._exclusive(deadline), self._connection_errors():
            payload = os.urandom(_PING_SIZE)
            self._ping = payload
            self._write(encode_frame(FRAME_PING, 0, 0, payload), deadline)
            self._read_until(lambda: self._ping is None, deadline)

    def close(self) -> None:
        """Send GOAWAY and close the socket (idempotent).

        If a call holds the connection for longer than a short grace period,
        the socket is shut down under it so that call fails promptly.
        """
        if not self._lock.acquire(timeout=_CONTROL_TIMEOUT):
            sock = self._sock
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            self._lock.acquire()
        try:
            self._close_socket()
        finally:
            self._lock.release()

    @property
    def closed(self) -> bool:
        """True once closed, failed or told by the peer (GOAWAY) to stop opening streams."""
        return self._sock is None or self._draining

    # ------------------------------------------------------------ calls

    @contextlib.contextmanager
    def _exclusive(self, deadline: float | None) -> Iterator[None]:
        remaining = _remaining(deadline)
        if not self._lock.acquire(timeout=-1 if remaining is None else remaining):
            raise H2Error("timed out waiting for the connection")
        try:
            if self.closed:
                raise H2Error("connection is closed")
            yield
        finally:
            self._lock.release()

    @contextlib.contextmanager
    def _connection_errors(self) -> Iterator[None]:
        """Tear the connection down on errors that are not confined to one stream."""
        try:
            yield
        except (_StreamError, StreamReset):
            raise
        except ProtocolError as exc:
            with contextlib.suppress(H2Error):
                payload = encode_goaway(0, exc.error_code, str(exc).encode()[:256])
                self._send_control(FRAME_GOAWAY, 0, 0, payload)
            self._abort()
            raise
        except H2Error:
            self._abort()
            raise

    def _open_stream(self) -> _Stream:
        stream = _Stream(self._next_stream_id, self._peer_initial_window)
        self._next_stream_id += 2
        if self._next_stream_id > MAX_STREAM_ID:
            self._draining = True  # IDs exhausted: finish this call, then close
        self._stream = stream
        return stream

    def _send_request(
        self,
        stream: _Stream,
        method: str,
        path: str,
        headers: Sequence[Header],
        body: bytes,
        deadline: float | None,
    ) -> None:
        pseudo = [(":method", method), (":scheme", self._scheme), (":path", path)]
        fields = [*pseudo, (":authority", self._authority)]
        fields += [(name.lower(), value) for name, value in headers]
        block = encode_header_block_frames(
            stream.id,
            encode_headers(fields),
            max_frame_size=self._peer_max_frame,
            end_stream=not body,
        )
        self._write(block, deadline)
        self._send_body(stream, memoryview(body), deadline)

    def _send_body(self, stream: _Stream, body: memoryview, deadline: float | None) -> None:
        """Send DATA within the connection and stream send windows and the peer's frame size."""
        while body:
            room = min(self._send_window, stream.send_window, self._peer_max_frame)
            if room <= 0:
                self._read_until(
                    lambda: stream.ended or min(self._send_window, stream.send_window) > 0,
                    deadline,
                )
                if stream.ended:  # the peer answered early; stop sending (RFC 9113 §8.1)
                    payload = encode_rst_stream(ERROR_CANCEL)
                    self._send_control(FRAME_RST_STREAM, 0, stream.id, payload)
                    return
                continue
            chunk, body = body[:room], body[room:]
            flags = 0 if body else FLAG_END_STREAM
            self._write(encode_frame(FRAME_DATA, flags, stream.id, bytes(chunk)), deadline)
            self._send_window -= len(chunk)
            stream.send_window -= len(chunk)

    # -------------------------------------------------------------- I/O

    def _socket(self) -> socket.socket:
        if self._sock is None:
            raise H2Error("connection is closed")
        return self._sock

    def _write(self, data: bytes, deadline: float | None) -> None:
        """Write whole frames. A failed or timed-out write leaves the connection unusable."""
        remaining = _remaining(deadline)
        if remaining == 0:
            raise _StreamError("request timed out")
        sock = self._socket()
        try:
            sock.settimeout(remaining)
            sock.sendall(data)
        except OSError as exc:
            raise H2Error(f"connection write failed: {exc}") from exc

    def _send_control(self, frame_type: int, flags: int, stream_id: int, payload: bytes) -> None:
        """Write a control frame with its own short deadline (it must not be skipped)."""
        frame = encode_frame(frame_type, flags, stream_id, payload)
        self._write(frame, time.monotonic() + _CONTROL_TIMEOUT)

    def _read_frame(self, deadline: float | None) -> Frame:
        while True:
            decoded = decode_frame(self._buffer)
            if decoded is not None:
                frame, consumed = decoded
                self._buffer = self._buffer[consumed:]
                return frame
            remaining = _remaining(deadline)
            if remaining == 0:
                raise _StreamError("request timed out")
            sock = self._socket()
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(_READ_SIZE)
            except TimeoutError as exc:
                raise _StreamError("request timed out") from exc
            except OSError as exc:
                raise H2Error(f"connection lost: {exc}") from exc
            if not chunk:
                raise H2Error("connection closed by peer")
            self._buffer += chunk

    def _read_until(self, done: Callable[[], bool], deadline: float | None) -> None:
        while not done():
            frame = self._read_frame(deadline)
            if self._pending is not None and frame.type != FRAME_CONTINUATION:
                raise ProtocolError("header block interrupted before END_HEADERS")
            handler = self._handlers.get(frame.type)
            if handler is not None:  # PRIORITY and unknown types are ignored
                handler(frame)

    def _close_socket(self) -> None:
        """Say goodbye (best effort) and release the socket."""
        if self._sock is not None:
            with contextlib.suppress(H2Error):
                self._send_control(FRAME_GOAWAY, 0, 0, encode_goaway(0, ERROR_NO_ERROR))
            self._abort()

    def _abort(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    # ----------------------------------------------------------- frames

    def _current(self, stream_id: int) -> _Stream | None:
        """The in-flight stream if the frame is for it; ``None`` for abandoned streams."""
        stream = self._stream
        return stream if stream is not None and stream.id == stream_id else None

    def _require_opened(self, stream_id: int) -> None:
        """Server push is disabled, so only odd IDs we have used can carry frames."""
        if stream_id % 2 == 0 or stream_id >= self._next_stream_id:
            raise ProtocolError(f"frame on idle stream {stream_id}")

    def _on_data(self, frame: Frame) -> None:
        self._require_opened(frame.stream_id)
        size = len(frame.payload)  # padding counts against flow control too
        self._recv_window -= size
        if self._recv_window < 0:
            raise ProtocolError("peer exceeded the connection window", ERROR_FLOW_CONTROL)
        self._recv_window = self._replenish(0, self._recv_window)
        stream = self._current(frame.stream_id)
        if stream is None:
            return
        if stream.headers is None:
            raise ProtocolError("DATA before HEADERS")
        stream.recv_window -= size
        if stream.recv_window < 0:
            raise ProtocolError("peer exceeded the stream window", ERROR_FLOW_CONTROL)
        stream.body += frame_body(frame)
        if len(stream.body) > _MAX_RESPONSE_BODY:
            raise _StreamError("response body too large")
        if frame.flags & FLAG_END_STREAM:
            stream.ended = True
        else:
            stream.recv_window = self._replenish(stream.id, stream.recv_window)

    def _replenish(self, stream_id: int, window: int) -> int:
        """Top a receive window back up once half of it is used; returns the new window."""
        if window > DEFAULT_WINDOW_SIZE // 2:
            return window
        increment = encode_window_update(DEFAULT_WINDOW_SIZE - window)
        self._send_control(FRAME_WINDOW_UPDATE, 0, stream_id, increment)
        return DEFAULT_WINDOW_SIZE

    def _on_headers(self, frame: Frame) -> None:
        self._require_opened(frame.stream_id)
        pending = _PendingBlock(
            frame.stream_id, bool(frame.flags & FLAG_END_STREAM), bytearray(frame_body(frame))
        )
        self._extend_block(pending, frame.flags)

    def _on_continuation(self, frame: Frame) -> None:
        pending = self._pending
        if pending is None or pending.stream_id != frame.stream_id:
            raise ProtocolError("unexpected CONTINUATION")
        pending.data += frame.payload
        self._extend_block(pending, frame.flags)

    def _extend_block(self, pending: _PendingBlock, flags: int) -> None:
        if len(pending.data) > _MAX_HEADER_BLOCK:
            raise ProtocolError("header block too large")
        if not flags & FLAG_END_HEADERS:
            self._pending = pending
            return
        self._pending = None
        self._on_header_block(pending)

    def _on_header_block(self, block: _PendingBlock) -> None:
        """Decode every block (HPACK state is per connection), then apply it to the stream."""
        try:
            fields = self._decoder.decode(bytes(block.data))
        except ProtocolError as exc:
            raise ProtocolError(f"HPACK: {exc}", ERROR_COMPRESSION) from exc
        stream = self._current(block.stream_id)
        if stream is None:
            return
        if stream.headers is None:
            status = _status(fields)
            if status < 200 and not block.end_stream:
                return  # informational (1xx); the final response follows
            stream.status, stream.headers = status, fields
        elif block.end_stream:
            stream.trailers = fields
        else:
            raise ProtocolError("trailers without END_STREAM")
        stream.ended = block.end_stream

    def _on_rst_stream(self, frame: Frame) -> None:
        self._require_opened(frame.stream_id)
        error_code = decode_rst_stream(frame.payload)
        if self._current(frame.stream_id) is not None:
            raise StreamReset(error_code)

    def _on_settings(self, frame: Frame) -> None:
        if frame.stream_id:
            raise ProtocolError("SETTINGS on a stream")
        if frame.flags & FLAG_ACK:
            if frame.payload:
                raise ProtocolError("SETTINGS ACK with a payload", ERROR_FRAME_SIZE)
            return
        for key, value in decode_settings(frame.payload):
            if key == SETTINGS_INITIAL_WINDOW_SIZE:
                self._set_initial_window(value)
            elif key == SETTINGS_MAX_FRAME_SIZE:
                if not DEFAULT_MAX_FRAME_SIZE <= value <= _MAX_FRAME_LENGTH:
                    raise ProtocolError(f"invalid SETTINGS_MAX_FRAME_SIZE {value}")
                self._peer_max_frame = value
        self._send_control(FRAME_SETTINGS, FLAG_ACK, 0, b"")

    def _set_initial_window(self, value: int) -> None:
        """A new initial window shifts open streams' windows by the difference (§6.9.2)."""
        if value > MAX_WINDOW_SIZE:
            raise ProtocolError("SETTINGS_INITIAL_WINDOW_SIZE too large", ERROR_FLOW_CONTROL)
        if self._stream is not None:
            self._stream.send_window += value - self._peer_initial_window
        self._peer_initial_window = value

    def _on_push_promise(self, frame: Frame) -> None:
        raise ProtocolError("PUSH_PROMISE received with push disabled")

    def _on_ping(self, frame: Frame) -> None:
        if frame.stream_id:
            raise ProtocolError("PING on a stream")
        if len(frame.payload) != _PING_SIZE:
            raise ProtocolError("PING payload is not 8 bytes", ERROR_FRAME_SIZE)
        if not frame.flags & FLAG_ACK:
            self._send_control(FRAME_PING, FLAG_ACK, 0, frame.payload)
        elif frame.payload == self._ping:
            self._ping = None

    def _on_goaway(self, frame: Frame) -> None:
        """Streams above ``last_stream_id`` were never processed; the rest may finish."""
        if frame.stream_id:
            raise ProtocolError("GOAWAY on a stream")
        last_stream_id, error_code, _ = decode_goaway(frame.payload)
        self._draining = True
        if self._stream is not None and self._stream.id > last_stream_id:
            raise H2Error(f"stream refused by GOAWAY (error code {error_code})")

    def _on_window_update(self, frame: Frame) -> None:
        increment = decode_window_update(frame.payload)
        if frame.stream_id == 0:
            self._send_window += increment
            if self._send_window > MAX_WINDOW_SIZE:
                raise ProtocolError("connection window overflow", ERROR_FLOW_CONTROL)
            return
        self._require_opened(frame.stream_id)
        stream = self._current(frame.stream_id)
        if stream is not None:
            stream.send_window += increment
            if stream.send_window > MAX_WINDOW_SIZE:
                raise ProtocolError("stream window overflow", ERROR_FLOW_CONTROL)


def _connect(
    host: str,
    port: int,
    ssl_context: ssl.SSLContext | None,
    server_hostname: str,
    timeout: float,
) -> socket.socket:
    """Open the socket, negotiate TLS/ALPN if asked, and send the preface and SETTINGS.

    The server's SETTINGS are not awaited: until they arrive the RFC defaults
    apply, and they are processed with the first response.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise H2Error(f"cannot connect to {host}:{port}: {exc}") from exc
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if ssl_context is not None:
            ssl_context.set_alpn_protocols(["h2"])
            sock = ssl_context.wrap_socket(sock, server_hostname=server_hostname)
            if sock.selected_alpn_protocol() != "h2":
                raise H2Error(f"{host}:{port} did not negotiate HTTP/2 (ALPN h2)")
        settings = encode_settings([(SETTINGS_ENABLE_PUSH, 0)])
        sock.sendall(CONNECTION_PREFACE + encode_frame(FRAME_SETTINGS, 0, 0, settings))
    except OSError as exc:
        sock.close()
        raise H2Error(f"cannot set up HTTP/2 with {host}:{port}: {exc}") from exc
    except BaseException:
        sock.close()
        raise
    return sock
