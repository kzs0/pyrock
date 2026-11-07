"""OTLP/gRPC trace exporter over the in-house HTTP/2 client (:mod:`._h2`).

Calls ``/opentelemetry.proto.collector.trace.v1.TraceService/Export`` with a
length-prefixed protobuf body. Metadata header names and values are validated
(no CR/LF, lowercase names). A non-zero ``grpc-status`` raises ``ExportError``;
``INVALID_ARGUMENT``, ``PERMISSION_DENIED`` and ``UNAUTHENTICATED`` raise
``PermanentExportError``.

The connection is opened on the first export and reopened after the peer or
the network closes it; requests on it are serialised by :mod:`._h2`. This
module is cheap to import: :mod:`._h2` and :mod:`ssl` load on the first
export, so :func:`parse_target` can be used to validate configuration.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from urllib.parse import unquote

from pyrock._version import __version__
from pyrock.attr import AttrValue
from pyrock.privacy import sanitize_url
from pyrock.trace import Span
from pyrock.trace.otlp import ExportError, PermanentExportError
from pyrock.trace.otlp._pb import encode_export_trace_request

if TYPE_CHECKING:
    import ssl

    from pyrock.trace.otlp._h2 import H2Connection, H2Response

__all__ = [
    "DEFAULT_PORT",
    "EXPORT_PATH",
    "GRPCExporter",
    "format_target",
    "frame_message",
    "parse_endpoint",
    "parse_target",
]

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


def parse_target(target: str, /, *, insecure: bool = False) -> tuple[str, int, bool]:
    """Parse a gRPC target into ``(host, port, plaintext)``; ``ValueError`` if malformed.

    The one parser for every gRPC target (local ``trace_url`` and cloud
    endpoints alike):

    * ``http://host[:port][/path]`` is plaintext (h2c);
    * ``https://host[:port][/path]`` is TLS;
    * a bare ``host[:port]`` is TLS;
    * ``insecure=True`` makes any of them plaintext.

    The port defaults to :data:`DEFAULT_PORT` (4317); a URL's path, query and
    fragment are ignored. IPv6 literals are written in brackets
    (``[::1]:4317``). Credentials (``user:pass@``) are rejected, and error
    messages never echo them.
    """
    if not isinstance(target, str):
        raise TypeError(f"gRPC target must be a str, not {type(target).__name__}")
    shown = _display(target)
    scheme, sep, rest = target.partition("://")
    if sep:
        scheme = scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported gRPC target scheme in {shown!r}")
        authority = rest
        for delimiter in "/?#":
            authority = authority.partition(delimiter)[0]
        plaintext = insecure or scheme == "http"
    else:
        authority, plaintext = target, insecure
    if "@" in authority:
        raise ValueError(f"gRPC target {shown!r} must not contain credentials")
    host, port_text = _split_authority(authority, shown)
    return host, DEFAULT_PORT if port_text is None else _port(port_text, shown), plaintext


def format_target(host: str, port: int, /) -> str:
    """``host:port``, with an IPv6 literal in brackets."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_endpoint(endpoint: str, /) -> tuple[str, int]:
    """Parse ``host:port`` (or a URL) into ``(host, port)``; ``ValueError`` if malformed.

    Unlike :func:`parse_target`, a bare ``host`` must name its port. A URL's
    scheme and path are ignored and its port defaults to 4317. IPv6 literals
    are written in brackets (``[::1]:4317``).
    """
    if "://" not in endpoint:
        _, port_text = _split_authority(endpoint.rpartition("@")[2], _display(endpoint))
        if port_text is None:
            raise ValueError(f"endpoint {_display(endpoint)!r} has no port")
    host, port, _ = parse_target(endpoint)
    return host, port


def _display(target: str) -> str:
    """``target`` safe for error messages: no credentials, query or fragment."""
    if "://" in target:
        return sanitize_url(target)
    return target.rpartition("@")[2]


def _split_authority(authority: str, shown: str) -> tuple[str, str | None]:
    if authority.startswith("["):
        literal, bracket, after = authority[1:].partition("]")
        if not bracket or (after and not after.startswith(":")):
            raise ValueError(f"malformed IPv6 endpoint {shown!r}")
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            raise ValueError(f"malformed IPv6 endpoint {shown!r}") from None
        return literal, after[1:] if after else None
    host, colon, port = authority.rpartition(":")
    if not colon:
        host = authority
    if not _HOST.fullmatch(host):
        raise ValueError(f"malformed endpoint host in {shown!r}")
    return host, port if colon else None


def _port(text: str, shown: str) -> int:
    if not (text.isascii() and text.isdigit()) or not 0 < int(text) < 65536:
        raise ValueError(f"malformed endpoint port in {shown!r}")
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

    The target is parsed by :func:`parse_target`: an ``http://`` URL or
    ``insecure=True`` uses h2c prior knowledge; otherwise TLS with ALPN ``h2``
    (``ssl_context`` or the system defaults). ``timeout`` bounds connecting
    and each export unless :meth:`export` is given its own.

    Connecting (TCP and TLS) happens outside the exporter's state lock, so
    :meth:`shutdown` never waits behind a slow connect: it marks the exporter
    stopped, closes the open connection within its ``timeout`` and returns; a
    connect still in flight closes its connection as soon as it completes.
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
        self._host, self._port, self._plaintext = parse_target(endpoint, insecure=insecure)
        self._endpoint = endpoint
        self._shown = _display(endpoint)
        self._service = service
        self._resource = dict(resource or {})
        self._metadata = _metadata(headers)
        self._ssl_context = ssl_context
        self._timeout = timeout
        self._lock = threading.Lock()
        """Guards ``_conn`` and ``_stopped``; never held during I/O."""
        self._connect_lock = threading.Lock()
        """Serialises connection attempts, so concurrent exports share one."""
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
        from pyrock.trace.otlp._h2 import H2Error

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
            raise ExportError(f"OTLP/gRPC export to {self._shown} failed: {exc}") from exc
        _check(response)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Close the connection (idempotent); later exports send nothing.

        Returns within ``timeout`` (``None``: when closed): a close that takes
        longer finishes on a daemon thread.
        """
        with self._lock:
            self._stopped = True
            conn, self._conn = self._conn, None
        if conn is not None:
            _close_within(conn, timeout)

    def _after_fork_in_child(self) -> None:
        """Forget the parent's connection without touching it (called by the fork hook).

        Closing it here would send GOAWAY on the socket the parent still uses;
        dropping the reference only releases the child's descriptor.
        """
        self._lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._conn = None

    def _replace_resource(self, resource: Mapping[str, AttrValue], /) -> None:
        """Use ``resource`` for later exports (a fork child's refreshed ``process.pid``)."""
        self._resource = dict(resource)

    def _headers(self, timeout: float) -> list[tuple[str, str]]:
        millis = min(max(int(timeout * 1000), 1), _MAX_TIMEOUT_MS)
        return [
            ("content-type", "application/grpc"),
            ("te", "trailers"),
            ("grpc-timeout", f"{millis}m"),
            ("user-agent", f"pyrock/{__version__}"),
            *self._metadata,
        ]

    def _open(self) -> H2Connection | None:
        """The current connection if it is still usable, else ``None``."""
        with self._lock:
            conn = self._conn
            return conn if conn is not None and not conn.closed else None

    def _connection(self, timeout: float) -> H2Connection | None:
        """The open connection, connecting first if needed; ``None`` after shutdown.

        The state lock is held only to read and publish ``_conn``; the connect
        itself runs under ``_connect_lock`` alone, which :meth:`shutdown`
        never takes.
        """
        from pyrock.trace.otlp._h2 import H2Connection, H2Error

        conn = self._open()
        if conn is not None:
            return conn
        if not self._connect_lock.acquire(timeout=max(timeout, 0.0)):
            raise ExportError(f"OTLP/gRPC connect to {self._shown} timed out waiting")
        try:
            if self._stopped:
                return None
            conn = self._open()  # another export connected while we waited
            if conn is not None:
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
                raise ExportError(f"OTLP/gRPC connect to {self._shown} failed: {exc}") from exc
            if not self._publish(conn):  # shut down while connecting: never leak the socket
                conn.close()
                return None
            return conn
        finally:
            self._connect_lock.release()

    def _publish(self, conn: H2Connection) -> bool:
        """Make ``conn`` the current connection; ``False`` (unpublished) once shut down."""
        with self._lock:
            if self._stopped:
                return False
            self._conn = conn
            return True

    def _tls_context(self) -> ssl.SSLContext | None:
        if self._plaintext:
            return None
        if self._ssl_context is None:
            import ssl

            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    def _discard(self, conn: H2Connection) -> None:
        with self._lock:
            if self._conn is conn:
                self._conn = None
        conn.close()


def _close_within(conn: H2Connection, timeout: float | None) -> None:
    """``conn.close()``, waiting at most ``timeout``; a slower close finishes in the background."""
    if timeout is None:
        conn.close()
        return
    closer = threading.Thread(target=conn.close, name="pyrock-grpc-close", daemon=True)
    closer.start()
    closer.join(max(timeout, 0.0))
