"""A deliberately small HTTP/2 client for unary gRPC calls.

* One long-lived connection: TLS with ALPN ``h2``, or h2c prior-knowledge when
  insecure.
* Sends SETTINGS, HEADERS, DATA, WINDOW_UPDATE, PING and GOAWAY; reads trailers.
* HPACK encoding uses only literal-without-indexing, no Huffman. Decoding
  supports the static table, the dynamic table and Huffman.
"""

from __future__ import annotations

import ssl
from collections.abc import Sequence
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


class H2Error(Exception):
    """Base HTTP/2 error (connection failure, timeout, GOAWAY)."""


class ProtocolError(H2Error):
    """The peer violated HTTP/2 or HPACK."""


class StreamReset(H2Error):
    """The peer reset the stream (RST_STREAM)."""

    def __init__(self, error_code: int) -> None:
        self.error_code = error_code
        super().__init__(f"stream reset by peer (error code {error_code})")


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
    raise NotImplementedError


def decode_frame(
    buffer: bytes, /, *, max_frame_size: int = DEFAULT_MAX_FRAME_SIZE
) -> tuple[Frame, int] | None:
    """Decode the first frame in ``buffer``.

    Returns ``(frame, consumed_bytes)``, or ``None`` when ``buffer`` does not
    yet hold a complete frame. The reserved stream-ID bit is ignored. Raises
    :class:`ProtocolError` when the declared length exceeds ``max_frame_size``.
    """
    raise NotImplementedError


def frame_body(frame: Frame, /) -> bytes:
    """Return the data of a DATA frame or the header-block fragment of a HEADERS frame.

    Strips padding (``FLAG_PADDED``) and, for HEADERS, the priority fields
    (``FLAG_PRIORITY``). Other frame types return the payload unchanged.
    Raises :class:`ProtocolError` when the payload is too short for the pad
    length byte, the priority fields and the declared padding (RFC 9113 §6.1).
    """
    raise NotImplementedError


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
    raise NotImplementedError


def encode_settings(params: Sequence[tuple[int, int]], /) -> bytes:
    """Serialise a SETTINGS payload (6 bytes per ``(identifier, value)``)."""
    raise NotImplementedError


def decode_settings(payload: bytes, /) -> list[tuple[int, int]]:
    """Parse a SETTINGS payload. :class:`ProtocolError` if its length is not a multiple of 6."""
    raise NotImplementedError


def encode_window_update(increment: int, /) -> bytes:
    """Serialise a WINDOW_UPDATE payload. ``ValueError`` unless ``1 <= increment < 2**31``."""
    raise NotImplementedError


def decode_window_update(payload: bytes, /) -> int:
    """Parse a WINDOW_UPDATE payload (reserved bit ignored).

    :class:`ProtocolError` when the payload is not 4 bytes or the increment is 0.
    """
    raise NotImplementedError


def encode_goaway(last_stream_id: int, error_code: int, debug_data: bytes = b"", /) -> bytes:
    """Serialise a GOAWAY payload."""
    raise NotImplementedError


def decode_goaway(payload: bytes, /) -> tuple[int, int, bytes]:
    """Parse a GOAWAY payload into ``(last_stream_id, error_code, debug_data)``.

    :class:`ProtocolError` when the payload is shorter than 8 bytes.
    """
    raise NotImplementedError


def encode_rst_stream(error_code: int, /) -> bytes:
    """Serialise an RST_STREAM payload."""
    raise NotImplementedError


def decode_rst_stream(payload: bytes, /) -> int:
    """Parse an RST_STREAM payload. :class:`ProtocolError` unless it is 4 bytes."""
    raise NotImplementedError


def encode_integer(value: int, prefix_bits: int, /, first_byte: int = 0) -> bytes:
    """HPACK integer (RFC 7541 §5.1) with an N-bit prefix.

    ``first_byte`` carries the representation's high-order flag bits.
    """
    raise NotImplementedError


def decode_integer(data: bytes, offset: int, prefix_bits: int, /) -> tuple[int, int]:
    """Decode an HPACK integer at ``offset``; returns ``(value, next_offset)``.

    :class:`ProtocolError` on truncation or an implausibly large value.
    """
    raise NotImplementedError


def encode_headers(headers: Sequence[Header], /) -> bytes:
    """HPACK-encode headers as literal-without-indexing, no Huffman."""
    raise NotImplementedError


def huffman_decode(data: bytes, /) -> bytes:
    """Decode an HPACK Huffman string (RFC 7541 Appendix B). Raises :class:`ProtocolError`."""
    raise NotImplementedError


class HPACKDecoder:
    """Stateful HPACK decoder (static + dynamic table, Huffman)."""

    def __init__(self, max_table_size: int = 4096) -> None:
        raise NotImplementedError

    def decode(self, block: bytes, /) -> list[Header]:
        raise NotImplementedError

    @property
    def max_table_size(self) -> int:
        """The current dynamic table size limit (changed by size updates in a block)."""
        raise NotImplementedError

    @property
    def table_size(self) -> int:
        """Current dynamic table size in RFC 7541 octets (name + value + 32 per entry)."""
        raise NotImplementedError

    @property
    def dynamic_table(self) -> list[Header]:
        """Dynamic table entries, newest first (index 62 is element 0)."""
        raise NotImplementedError

    def set_max_table_size(self, size: int, /) -> None:
        """Set the protocol limit (our SETTINGS_HEADER_TABLE_SIZE); evicts as needed.

        Size updates in later blocks may not exceed it (:class:`ProtocolError`).
        """
        raise NotImplementedError


class H2Connection:
    """A single HTTP/2 connection for sequential or concurrent unary requests. Thread-safe."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        """``ssl_context=None`` means h2c prior-knowledge (plaintext)."""
        raise NotImplementedError

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
        """Send one request and wait for the full response and trailers."""
        raise NotImplementedError

    def ping(self, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Send GOAWAY and close the socket (idempotent)."""
        raise NotImplementedError

    @property
    def closed(self) -> bool:
        raise NotImplementedError
