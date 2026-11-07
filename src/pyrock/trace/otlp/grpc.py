"""OTLP/gRPC trace exporter over the in-house HTTP/2 client (:mod:`._h2`).

Calls ``/opentelemetry.proto.collector.trace.v1.TraceService/Export`` with a
length-prefixed protobuf body. Metadata header names and values are validated
(no CR/LF, lowercase names). A non-zero ``grpc-status`` raises ``ExportError``;
``INVALID_ARGUMENT``, ``PERMISSION_DENIED`` and ``UNAUTHENTICATED`` raise
``PermanentExportError``.

The connection is opened on the first export and reopened after the peer or
the network closes it; requests on it are serialised by :mod:`._h2`.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
import threading
from collections.abc import Mapping, Sequence
from urllib.parse import unquote

from pyrock._version import __version__
from pyrock.attr import AttrValue
from pyrock.trace import Span
from pyrock.trace.otlp import ExportError, PermanentExportError
from pyrock.trace.otlp._h2 import H2Connection, H2Error, H2Response
from pyrock.trace.otlp._pb import encode_export_trace_request

__all__ = ["EXPORT_PATH", "GRPCExporter", "frame_message", "parse_endpoint"]

EXPORT_PATH = "/opentelemetry.proto.collector.trace.v1.TraceService/Export"

DEFAULT_PORT = 4317
"""The OTLP/gRPC port, used when a URL endpoint names none."""

_CODE_NAMES = (
    "OK",
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "ABORTED",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
    "INTERNAL",
    "UNAVAILABLE",
    "DATA_LOSS",
    "UNAUTHENTICATED",
)
_UNKNOWN = 2
_PERMANENT_CODES = frozenset({3, 7, 16})  # INVALID_ARGUMENT, PERMISSION_DENIED, UNAUTHENTICATED
# gRPC's HTTP-to-status mapping for responses without grpc-status
# (doc/http-grpc-status-mapping.md); everything else is UNKNOWN.
_HTTP_TO_GRPC = {400: 13, 401: 16, 403: 7, 404: 12, 429: 14, 502: 14, 503: 14, 504: 14}

_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9\-_.]*[A-Za-z0-9])?")
_METADATA_NAME = re.compile(r"[0-9a-z_.\-]+")
_METADATA_VALUE = re.compile(r"[\x20-\x7e]*")
_RESERVED_METADATA = frozenset(
    {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade"}
    | {"content-type", "te", "host", "user-agent"}
)
_MAX_TIMEOUT_MS = 99_999_999  # grpc-timeout allows at most 8 digits


def frame_message(payload: bytes, /) -> bytes:
    """Prefix ``payload`` with the gRPC 5-byte header (uncompressed)."""
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def parse_endpoint(endpoint: str, /) -> tuple[str, int]:
    """Parse ``host:port`` (or a URL) into ``(host, port)``; ``ValueError`` if malformed.

    A URL's scheme and path are ignored and its port defaults to 4317. IPv6
    literals are written in brackets (``[::1]:4317``).
    """
    scheme, sep, rest = endpoint.partition("://")
    if sep:
        if scheme.lower() not in ("http", "https"):
            raise ValueError(f"unsupported endpoint scheme in {endpoint!r}")
        authority = rest.partition("/")[0]
        default_port: int | None = DEFAULT_PORT
    else:
        authority, default_port = endpoint, None
    host, port_text = _split_authority(authority, endpoint)
    if port_text is None:
        if default_port is None:
            raise ValueError(f"endpoint {endpoint!r} has no port")
        return host, default_port
    return host, _port(port_text, endpoint)


def _split_authority(authority: str, endpoint: str) -> tuple[str, str | None]:
    if authority.startswith("["):
        literal, bracket, after = authority[1:].partition("]")
        if not bracket or (after and not after.startswith(":")):
            raise ValueError(f"malformed IPv6 endpoint {endpoint!r}")
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            raise ValueError(f"malformed IPv6 endpoint {endpoint!r}") from None
        return literal, after[1:] if after else None
    host, colon, port = authority.rpartition(":")
    if not colon:
        host = authority
    if not _HOST.fullmatch(host):
        raise ValueError(f"malformed endpoint host in {endpoint!r}")
    return host, port if colon else None


def _port(text: str, endpoint: str) -> int:
    if not (text.isascii() and text.isdigit()) or not 0 < int(text) < 65536:
        raise ValueError(f"malformed endpoint port in {endpoint!r}")
    return int(text)


def _metadata(headers: Mapping[str, str] | None) -> list[tuple[str, str]]:
    """Lowercased, validated metadata; ``ValueError`` for invalid or duplicate names."""
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        lower = name.lower()
        if (
            not _METADATA_NAME.fullmatch(lower)
            or lower in _RESERVED_METADATA
            or lower.startswith("grpc-")
        ):
            raise ValueError(f"invalid gRPC metadata name {name!r}")
        if not _METADATA_VALUE.fullmatch(value):
            raise ValueError(f"invalid gRPC metadata value for {name!r}")
        if lower in out:
            raise ValueError(f"duplicate gRPC metadata {lower!r}")
        out[lower] = value
    return list(out.items())


def _status_error(code: int, message: str) -> ExportError:
    name = _CODE_NAMES[code] if 0 <= code < len(_CODE_NAMES) else str(code)
    text = f"OTLP/gRPC export failed: {name} ({code})"
    if message:
        text += f": {message}"
    cls = PermanentExportError if code in _PERMANENT_CODES else ExportError
    return cls(text)


def _check(response: H2Response) -> None:
    """Raise the error a non-OK response means (trailers-only responses included)."""
    raw = response.header("grpc-status")
    if raw is None:
        if response.status != 200:
            code = _HTTP_TO_GRPC.get(response.status, _UNKNOWN)
            raise _status_error(code, f"HTTP {response.status}")
        raise ExportError("OTLP/gRPC export failed: response has no grpc-status")
    code = int(raw) if raw.isascii() and raw.isdigit() else _UNKNOWN
    if code:
        raise _status_error(code, unquote(response.header("grpc-message") or "", errors="replace"))


class GRPCExporter:
    """Exports spans with OTLP/gRPC. Raises ``ExportError`` on failure.

    ``insecure=True`` uses h2c prior knowledge; otherwise TLS with ALPN ``h2``
    (``ssl_context`` or the system defaults). ``timeout`` bounds connecting
    and each export unless :meth:`export` is given its own.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        service: str,
        resource: Mapping[str, AttrValue] | None = None,
        headers: Mapping[str, str] | None = None,
        insecure: bool = False,
        timeout: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """``ValueError`` for invalid metadata names/values or a malformed endpoint."""
        self._host, self._port = parse_endpoint(endpoint)
        self._endpoint = endpoint
        self._service = service
        self._resource = dict(resource or {})
        self._metadata = _metadata(headers)
        self._insecure = insecure
        self._ssl_context = ssl_context
        self._timeout = timeout
        self._lock = threading.Lock()
        self._conn: H2Connection | None = None
        self._stopped = False

    @property
    def endpoint(self) -> str:
        """The endpoint as configured."""
        return self._endpoint

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        """Send one ``Export`` call; nothing is sent for an empty batch or after shutdown."""
        if not spans or self._stopped:
            return
        timeout = self._timeout if timeout is None else timeout
        payload = encode_export_trace_request(
            spans, service=self._service, resource=self._resource, scope_version=__version__
        )
        conn = self._connection(timeout)
        if conn is None:
            return
        try:
            response = conn.request(
                "POST", EXPORT_PATH, self._headers(timeout), frame_message(payload), timeout=timeout
            )
        except H2Error as exc:
            if conn.closed:
                self._discard(conn)
            raise ExportError(f"OTLP/gRPC export to {self._endpoint} failed: {exc}") from exc
        _check(response)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Close the connection (idempotent); later exports send nothing."""
        with self._lock:
            self._stopped = True
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def _after_fork_in_child(self) -> None:
        """Forget the parent's connection without touching it (called by the fork hook).

        Closing it here would send GOAWAY on the socket the parent still uses;
        dropping the reference only releases the child's descriptor.
        """
        self._lock = threading.Lock()
        self._conn = None

    def _headers(self, timeout: float) -> list[tuple[str, str]]:
        millis = min(max(int(timeout * 1000), 1), _MAX_TIMEOUT_MS)
        return [
            ("content-type", "application/grpc"),
            ("te", "trailers"),
            ("grpc-timeout", f"{millis}m"),
            ("user-agent", f"pyrock/{__version__}"),
            *self._metadata,
        ]

    def _connection(self, timeout: float) -> H2Connection | None:
        """The open connection, connecting first if needed; ``None`` after shutdown."""
        with self._lock:
            if self._stopped:
                return None
            conn = self._conn
            if conn is not None and not conn.closed:
                return conn
            try:
                conn = H2Connection(
                    self._host,
                    self._port,
                    ssl_context=self._tls_context(),
                    server_hostname=self._host,
                    connect_timeout=timeout,
                )
            except H2Error as exc:
                self._conn = None
                raise ExportError(f"OTLP/gRPC connect to {self._endpoint} failed: {exc}") from exc
            self._conn = conn
            return conn

    def _tls_context(self) -> ssl.SSLContext | None:
        if self._insecure:
            return None
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    def _discard(self, conn: H2Connection) -> None:
        with self._lock:
            if self._conn is conn:
                self._conn = None
        conn.close()
