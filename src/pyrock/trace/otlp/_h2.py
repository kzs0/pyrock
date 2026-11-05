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
