"""The hardened observability HTTP server (``/metrics``, ``/health``, ``/ready``, pprof).

Security defaults match Go: loopback-only address, pprof off unless both
enabled and given an explicit handler, deadline-based header reads (Slowloris),
read/write/idle timeouts, a header byte limit (431 and close), a connection
cap with reject-on-full (a rejected connection gets ``503`` and is then
closed), and graceful shutdown.

With ``pprof`` and a ``profile_handler``, requests under ``/debug/pprof/`` are
passed to the handler with ``PATH_INFO`` unchanged (the full
``/debug/pprof/...`` path).

.. code-block:: python

    srv = server.Server(bedrock.metrics, server.Config(addr="0.0.0.0:9090"))
    srv.start()
    srv.shutdown(timeout=15)

Design: a small HTTP/1.1 server that owns its socket reads, so every timeout
is a real deadline. One accept thread waits on the listener and a wake-up
socket; each accepted connection holds a slot of a bounded semaphore and is
served on its own daemon thread. An idle keep-alive connection waits on its
socket *and* the same wake-up socket, so :meth:`Server.shutdown` ends idle
waits at once on every platform (closing or shutting down a socket does not
wake a thread blocked on it on Windows). A connection over the cap is answered ``503``
with ``Connection: close`` by a short-lived (and itself bounded) thread.
Responses are buffered, so a failing application never leaves partial output
and every response carries ``Content-Length`` for keep-alive. Request bodies
are read in full (at most 1 MiB); ``Transfer-Encoding`` is not supported
(``501``), which the observability endpoints never need.

Import rules: ``pyrock.server`` imports only ``pyrock.metric`` (and leaf
modules) from pyrock.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from pyrock._compat import is_positive_number, seconds
from pyrock._types import ExcInfo, StartResponse, WSGIApp, WSGIEnviron
from pyrock.metric import Registry, prometheus

__all__ = ["DEFAULT_ADDR", "Config", "Server", "health_app", "metrics_app"]

DEFAULT_ADDR = "127.0.0.1:9090"

_logger = logging.getLogger(__name__)

_RECV_SIZE = 64 * 1024
_MAX_BODY_BYTES = 1 << 20
_LINGER_SECONDS = 1.0
"""How long a closing connection drains unread input (so the peer sees our reply, not a reset)."""
_MAX_REJECTING = 16
"""Concurrent ``503`` rejections; beyond this an over-cap connection is closed at once."""
_ACCEPT_BACKOFF_SECONDS = 0.1
"""Pause after a failed ``accept`` (e.g. out of file descriptors) instead of spinning."""
_PPROF_PREFIX = "/debug/pprof/"

_DIGITS = re.compile(r"[0-9]+")
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_TARGET = re.compile(r"/[\x21-\x7e]*")
_VERSION = re.compile(r"HTTP/[0-9]\.[0-9]")
_FIELD_TEXT = re.compile(r"[\t\x20-\x7e\x80-\xff]*")
_STATUS = re.compile(r"[1-5][0-9]{2}(?: [\t\x20-\x7e\x80-\xff]*)?")
_SUPPORTED_VERSIONS = frozenset({"HTTP/1.0", "HTTP/1.1"})
_MANAGED_HEADERS = frozenset(
    {"connection", "content-length", "date", "keep-alive", "transfer-encoding", "upgrade"}
)
"""Response headers the server sets itself; an application's values are dropped."""

_REASONS = {
    400: "Bad Request",
    404: "Not Found",
    408: "Request Timeout",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------- config


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Server settings (Go: ``server.Config``); durations in seconds."""

    addr: str = DEFAULT_ADDR
    """``host:port``; port ``0`` picks a free port (see :attr:`Server.address`)."""
    metrics: bool = True
    pprof: bool = False
    profile_handler: WSGIApp | None = field(default=None, compare=False)
    read_timeout: float = 10.0
    read_header_timeout: float = 5.0
    write_timeout: float = 30.0
    idle_timeout: float = 120.0
    max_header_bytes: int = 1 << 20
    shutdown_timeout: float = 30.0
    max_connections: int = 64
    """Concurrent connection cap; a connection over it gets ``503`` and is closed.

    ``pyrock.Config.server_max_connections`` maps here.
    """

    def __post_init__(self) -> None:
        problems: list[str] = []
        for name in (
            "read_timeout",
            "read_header_timeout",
            "write_timeout",
            "idle_timeout",
            "shutdown_timeout",
        ):
            value = seconds(getattr(self, name))
            object.__setattr__(self, name, value)
            if not is_positive_number(value):
                problems.append(f"{name} must be positive")
        if self.max_header_bytes <= 0:
            problems.append("max_header_bytes must be positive")
        if self.max_connections <= 0:
            problems.append("max_connections must be positive")
        try:
            _split_addr(self.addr)
        except ValueError:
            problems.append("addr must be 'host:port'")
        if problems:
            raise ValueError("invalid server Config: " + "; ".join(problems))


def _split_addr(addr: str, /) -> tuple[str, int]:
    """Split ``host:port`` (``[v6]:port`` too) into a bindable host and an int port."""
    host, sep, port = addr.rpartition(":")
    if not sep or not _DIGITS.fullmatch(port) or int(port) > 65535:
        raise ValueError(f"invalid address {addr!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, int(port)


# --------------------------------------------------------------------- reading


class _HeaderTooLarge(Exception):
    """The request line plus headers exceeded ``max_header_bytes`` (answered with 431)."""


class _HTTPError(Exception):
    """A request the server answers with an error ``status`` and then closes."""

    def __init__(self, status: int, /) -> None:
        super().__init__(status)
        self.status = status


def _read_head(
    sock: socket.socket,
    /,
    *,
    timeout: float,
    max_bytes: int,
    clock: Callable[[], float] = time.monotonic,
    initial: bytes = b"",
) -> tuple[bytes, bytes]:
    """Read the request line and headers within ``timeout`` seconds **in total**.

    The remaining time is recomputed from ``clock`` before every ``recv``, so a
    slow drip cannot extend the deadline. Returns ``(head, rest)``: ``head``
    runs through the terminating blank line (``\r\n\r\n``) and ``rest`` is
    whatever arrived after it. Returns ``(b"", b"")`` when the peer closes
    before sending anything. Raises :class:`TimeoutError` when the deadline
    passes, :class:`_HeaderTooLarge` when the head (request line included)
    is longer than ``max_bytes`` or more than ``max_bytes`` arrive without a
    terminator (without waiting for more data), and :class:`ConnectionError`
    if the peer closes mid-head.

    ``initial`` is data already read from the connection (a pipelined request).
    """
    deadline = clock() + timeout
    buffer = bytearray(initial)
    searched = 0
    while True:
        end = buffer.find(b"\r\n\r\n", max(0, searched - 3))
        if end >= 0:
            if end + 4 > max_bytes:
                raise _HeaderTooLarge
            return bytes(buffer[: end + 4]), bytes(buffer[end + 4 :])
        if len(buffer) >= max_bytes:
            raise _HeaderTooLarge
        searched = len(buffer)
        chunk = _recv_before(sock, deadline - clock())
        if not chunk:
            if buffer:
                raise ConnectionError("peer closed the connection mid-head")
            return b"", b""
        buffer += chunk


def _read_body(
    sock: socket.socket, length: int, buffered: bytes, /, *, deadline: float
) -> tuple[bytes, bytes]:
    """Read a ``length``-byte body by ``deadline`` (monotonic); return ``(body, rest)``."""
    data = bytearray(buffered)
    while len(data) < length:
        chunk = _recv_before(sock, deadline - time.monotonic())
        if not chunk:
            raise ConnectionError("peer closed the connection mid-body")
        data += chunk
    return bytes(data[:length]), bytes(data[length:])


def _recv_before(sock: socket.socket, remaining: float, /) -> bytes:
    """One ``recv`` that may wait at most ``remaining`` seconds (``TimeoutError`` if none left)."""
    if remaining <= 0:
        raise TimeoutError("deadline passed")
    sock.settimeout(remaining)
    return sock.recv(_RECV_SIZE)


# --------------------------------------------------------------------- parsing


@dataclass(slots=True)
class _Request:
    """A parsed request head; header names are lower-cased."""

    method: str
    path: str
    query: str
    version: str
    headers: list[tuple[str, str]]

    def header(self, name: str, /) -> str | None:
        """The values of header ``name`` joined with ``,``, or ``None`` if absent."""
        values = [value for key, value in self.headers if key == name]
        return ",".join(values) if values else None

    @property
    def keep_alive(self) -> bool:
        """Whether the client allows the connection to stay open after the response."""
        tokens = {t.strip().lower() for t in (self.header("connection") or "").split(",")}
        if self.version == "HTTP/1.0":
            return "keep-alive" in tokens
        return "close" not in tokens

    @property
    def content_length(self) -> int:
        """The request body length (``_HTTPError`` for unsupported or invalid framing)."""
        if self.header("transfer-encoding") is not None:
            raise _HTTPError(501)
        values = {value for key, value in self.headers if key == "content-length"}
        if not values:
            return 0
        value = values.pop()
        if values or not _DIGITS.fullmatch(value):
            raise _HTTPError(400)
        if int(value) > _MAX_BODY_BYTES:
            raise _HTTPError(413)
        return int(value)


def _parse_head(head: bytes, /) -> _Request:
    """Parse a request head ending in ``\r\n\r\n``; ``_HTTPError`` if malformed."""
    request_line, *fields = head[:-4].decode("latin-1").split("\r\n")
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise _HTTPError(400)
    method, target, version = parts
    if not (_TOKEN.fullmatch(method) and _TARGET.fullmatch(target) and _VERSION.fullmatch(version)):
        raise _HTTPError(400)
    if version not in _SUPPORTED_VERSIONS:
        raise _HTTPError(505)
    path, _, query = target.partition("?")
    request = _Request(method, path, query, version, _parse_fields(fields))
    hosts = [value for key, value in request.headers if key == "host"]
    if len(hosts) > 1 or (version == "HTTP/1.1" and not hosts):
        raise _HTTPError(400)
    return request


def _parse_fields(lines: Iterable[str], /) -> list[tuple[str, str]]:
    """Parse ``name: value`` header lines (no obsolete folding, no control characters)."""
    fields: list[tuple[str, str]] = []
    for line in lines:
        name, sep, value = line.partition(":")
        value = value.strip(" \t")
        if not (sep and _TOKEN.fullmatch(name) and _FIELD_TEXT.fullmatch(value)):
            raise _HTTPError(400)
        fields.append((name.lower(), value))
    return fields


# --------------------------------------------------------------------- WSGI apps


def metrics_app(registry: Registry, /) -> WSGIApp:
    """A WSGI app serving Prometheus text for ``registry`` (500, no body, on ExpositionError)."""

    def app(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        try:
            body = prometheus.encode(registry)
        except prometheus.ExpositionError:
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "0")],
            )
            return []
        start_response(
            "200 OK",
            [("Content-Type", prometheus.CONTENT_TYPE), ("Content-Length", str(len(body)))],
        )
        return [body]

    return app


def health_app() -> WSGIApp:
    """A WSGI app answering ``200 ok``."""

    def app(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        start_response(
            "200 OK", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "2")]
        )
        return [b"ok"]

    return app


def _not_found_app(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
    body = b"404 page not found\n"
    start_response(
        "404 Not Found",
        [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
    )
    return [body]


@dataclass(slots=True)
class _Response:
    """A complete, buffered response."""

    status: str
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def code(self) -> int:
        return int(self.status[:3])


def _run_app(app: WSGIApp, environ: WSGIEnviron, /) -> _Response:
    """Call a WSGI app and buffer its whole response (any failure propagates)."""
    started: list[tuple[str, list[tuple[str, str]]]] = []
    chunks: list[bytes] = []

    def start_response(
        status: str, headers: list[tuple[str, str]], exc_info: ExcInfo | None = None
    ) -> Callable[[bytes], object]:
        # Nothing is sent before the app returns, so a later call (an error
        # page, with ``exc_info``) simply replaces the earlier status and output.
        started[:] = [(status, list(headers))]
        chunks.clear()
        return chunks.append

    result = app(environ, start_response)
    try:
        chunks.extend(result)
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    if not started:
        raise RuntimeError("the application did not call start_response")
    status, headers = started[0]
    _check_response_head(status, headers)
    return _Response(status, headers, b"".join(chunks))  # TypeError for non-bytes chunks


def _check_response_head(status: str, headers: list[tuple[str, str]], /) -> None:
    """Reject a status or header an application could use to inject response text."""
    if not _STATUS.fullmatch(status):
        raise ValueError(f"invalid WSGI status {status!r}")
    for name, value in headers:
        if not (_TOKEN.fullmatch(name) and _FIELD_TEXT.fullmatch(value)):
            raise ValueError(f"invalid WSGI response header {name!r}")


def _environ(
    request: _Request, body: bytes, /, *, server: tuple[str, int], peer: tuple[Any, ...]
) -> WSGIEnviron:
    """Build the PEP 3333 environ for ``request``."""
    environ: WSGIEnviron = {
        "REQUEST_METHOD": request.method,
        "SCRIPT_NAME": "",
        "PATH_INFO": unquote(request.path, "latin-1"),
        "QUERY_STRING": request.query,
        "SERVER_NAME": server[0],
        "SERVER_PORT": str(server[1]),
        "SERVER_PROTOCOL": request.version,
        "REMOTE_ADDR": str(peer[0]),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    for name, value in request.headers:
        if "_" in name:
            continue  # ``X_A`` would alias ``X-A`` in the environ, so drop it (as nginx does)
        key = name.upper().replace("-", "_")
        if key not in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            key = "HTTP_" + key
        environ[key] = f"{environ[key]},{value}" if key in environ else value
    return environ


# --------------------------------------------------------------------- writing


def _encode_response(response: _Response, /, *, keep_alive: bool, head_only: bool) -> bytes:
    """Serialize ``response`` with the server-managed framing headers."""
    bodyless = response.code < 200 or response.code in (204, 304)
    lines = [f"HTTP/1.1 {response.status}", f"Date: {_http_date()}"]
    lines += [f"{n}: {v}" for n, v in response.headers if n.lower() not in _MANAGED_HEADERS]
    if not bodyless:
        lines.append(f"Content-Length: {len(response.body)}")
    lines.append("Connection: keep-alive" if keep_alive else "Connection: close")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head if head_only or bodyless else head + response.body


def _error_response(code: int, /) -> _Response:
    reason = _REASONS[code]
    body = f"{code} {reason}\n".encode()
    return _Response(f"{code} {reason}", [("Content-Type", "text/plain; charset=utf-8")], body)


def _http_date() -> str:
    """The current time as an IMF-fixdate (``Sun, 06 Nov 1994 08:49:37 GMT``), locale-free."""
    t = time.gmtime()
    return (
        f"{_DAYS[t.tm_wday]}, {t.tm_mday:02d} {_MONTHS[t.tm_mon - 1]} {t.tm_year} "
        f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} GMT"
    )


def _send_before(sock: socket.socket, data: bytes, deadline: float, /) -> None:
    """Send all of ``data`` by ``deadline`` (``sendall``'s timeout bounds the whole call)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("write deadline passed")
    sock.settimeout(remaining)
    sock.sendall(data)


def _linger(sock: socket.socket, /) -> None:
    """Finish our side and briefly drain the peer's unread input before the caller closes.

    Closing with unread input makes the kernel send a reset, which can destroy
    the response we just wrote before the peer reads it.
    """
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + _LINGER_SECONDS
        while _recv_before(sock, deadline - time.monotonic()):
            pass


def _wait_readable(sock: socket.socket, stop: socket.socket, timeout: float, /) -> bool:
    """Wait up to ``timeout`` for ``sock`` to be readable; ``False`` on timeout or ``stop``.

    ``stop`` becomes (and stays) readable at shutdown. Input or end-of-stream
    on ``sock`` both count as readable; the request read tells them apart.
    """
    if timeout <= 0:
        return False
    with selectors.DefaultSelector() as selector:
        selector.register(sock, selectors.EVENT_READ)
        selector.register(stop, selectors.EVENT_READ)
        ready = {key.fileobj for key, _ in selector.select(timeout)}
    return sock in ready and stop not in ready


def _interrupt(sock: socket.socket, /) -> None:
    """Cut ``sock`` so the peer sees it close (on POSIX this also wakes a blocked reader)."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


# --------------------------------------------------------------------- server


@dataclass(eq=False, slots=True)
class _Connection:
    """An accepted connection."""

    sock: socket.socket
    peer: tuple[Any, ...]


class Server:
    """The observability server (Go: ``server.Server``)."""

    def __init__(self, registry: Registry, config: Config | None = None, /) -> None:
        self._config = config if config is not None else Config()
        self._metrics = metrics_app(registry)
        self._health = health_app()
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(self._config.max_connections)
        self._rejecting = threading.BoundedSemaphore(_MAX_REJECTING)
        self._accept_done = threading.Event()
        # Guarded by ``_lock``:
        self._connections: set[_Connection] = set()
        self._address: tuple[str, int] | None = None
        self._bound = False
        self._wake: socket.socket | None = None
        """Written once by :meth:`shutdown` and never drained, so it stays readable."""
        self._wake_reader: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    @property
    def config(self) -> Config:
        return self._config

    @property
    def address(self) -> tuple[str, int] | None:
        """The bound ``(host, port)`` once started, else ``None``."""
        with self._lock:
            return self._address

    def start(self) -> None:
        """Bind and serve on a daemon thread. ``OSError`` if the address is unavailable."""
        listener, wake = self._bind()
        thread = threading.Thread(
            target=self._accept_loop, args=(listener, wake), name="pyrock-server", daemon=True
        )
        with self._lock:
            self._accept_thread = thread
        thread.start()

    def serve_forever(self) -> None:
        """Bind and serve on the calling thread until :meth:`shutdown`."""
        self._accept_loop(*self._bind())

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Stop accepting, wait for in-flight requests up to ``timeout``, close (idempotent).

        Idle keep-alive connections are closed at once; connections still busy
        when the timeout passes are cut (their threads finish on their own,
        within their read or write timeout).
        """
        budget = self._config.shutdown_timeout if timeout is None else float(seconds(timeout))
        deadline = time.monotonic() + budget

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        with self._lock:
            self._stopping.set()
            if self._wake is not None:
                with contextlib.suppress(OSError):
                    self._wake.send(b"\0")
            bound, accept_thread = self._bound, self._accept_thread
        if bound:
            self._accept_done.wait(remaining())
        if accept_thread is not None:
            accept_thread.join(remaining())
        with self._lock:
            if not self._drained.wait_for(lambda: not self._connections, remaining()):
                for conn in self._connections:
                    _interrupt(conn.sock)

    # -- accepting --------------------------------------------------------

    def _bind(self) -> tuple[socket.socket, socket.socket]:
        """Open the listener and the accept loop's wake-up socket pair."""
        host, port = _split_addr(self._config.addr)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with self._lock:  # held while binding, so concurrent starts cannot both bind
            if self._stopping.is_set():
                raise RuntimeError("the server has been shut down")
            if self._bound:
                raise RuntimeError("the server is already started")
            listener = socket.create_server((host, port), family=family, backlog=128)
            listener.setblocking(False)
            wake_reader, wake_writer = socket.socketpair()
            wake_writer.setblocking(False)
            self._bound = True
            self._wake, self._wake_reader = wake_writer, wake_reader
            self._address = listener.getsockname()[:2]
        return listener, wake_reader

    def _accept_loop(self, listener: socket.socket, wake: socket.socket) -> None:
        """Accept until :meth:`shutdown` writes to ``wake``; then close the listener."""
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(listener, selectors.EVENT_READ)
                selector.register(wake, selectors.EVENT_READ)
                while not self._stopping.is_set():
                    ready = {key.fileobj for key, _ in selector.select()}
                    if wake not in ready:
                        self._accept(listener)
        finally:
            with self._lock:
                listener.close()  # also resets connections still queued in the backlog
                self._accept_done.set()
                self._close_wake_if_done()

    def _close_wake_if_done(self) -> None:
        """Close the wake-up pair once nothing can wait on it any more (``_lock`` held)."""
        if self._accept_done.is_set() and not self._connections:
            for sock in (self._wake, self._wake_reader):
                if sock is not None:
                    sock.close()
            self._wake = self._wake_reader = None

    def _accept(self, listener: socket.socket) -> None:
        """Accept one connection and hand it to a worker thread or the rejecter."""
        try:
            sock, peer = listener.accept()
        except (BlockingIOError, ConnectionAbortedError):
            return  # another wake-up raced us, or the peer gave up while queued
        except OSError:
            _logger.warning("observability server: accept failed", exc_info=True)
            self._stopping.wait(_ACCEPT_BACKOFF_SECONDS)
            return
        if not self._slots.acquire(blocking=False):
            self._reject(sock)
            return
        conn = _Connection(sock, peer)
        with self._lock:
            self._connections.add(conn)
        worker = threading.Thread(
            target=self._serve_connection, args=(conn,), name="pyrock-server-conn", daemon=True
        )
        try:
            worker.start()
        except RuntimeError:  # the interpreter cannot start more threads
            self._release(conn)

    def _reject(self, sock: socket.socket) -> None:
        """Answer an over-cap connection ``503`` + ``Connection: close`` off the accept thread."""
        if not self._rejecting.acquire(blocking=False):
            sock.close()
            return

        def run() -> None:
            try:
                with sock, contextlib.suppress(OSError):
                    response = _encode_response(
                        _error_response(503), keep_alive=False, head_only=False
                    )
                    _send_before(sock, response, time.monotonic() + _LINGER_SECONDS)
                    _linger(sock)
            finally:
                self._rejecting.release()

        try:
            threading.Thread(target=run, name="pyrock-server-reject", daemon=True).start()
        except RuntimeError:
            self._rejecting.release()
            sock.close()

    def _release(self, conn: _Connection) -> None:
        """Forget, close and free the slot of a finished connection."""
        with self._lock:
            self._connections.discard(conn)
            conn.sock.close()
            self._close_wake_if_done()
            self._drained.notify_all()
        self._slots.release()

    # -- serving a connection ----------------------------------------------

    def _serve_connection(self, conn: _Connection) -> None:
        """Serve requests on ``conn`` until it closes, errs, idles out or the server stops."""
        try:
            buffered = b""
            started = time.monotonic()  # the first request's header clock starts at accept
            first = True
            while True:
                if not buffered:
                    wait = self._first_byte_wait(started) if first else self._config.idle_timeout
                    if not self._await_request(conn, wait):
                        return
                if not first:
                    started = time.monotonic()
                first = False
                buffered, keep_alive = self._serve_request(conn, buffered, started)
                if not keep_alive:
                    _linger(conn.sock)
                    return
        except OSError:
            pass  # the peer went away or a deadline passed while writing: just close
        finally:
            self._release(conn)

    def _first_byte_wait(self, accepted: float) -> float:
        return self._config.read_header_timeout - (time.monotonic() - accepted)

    def _await_request(self, conn: _Connection, timeout: float) -> bool:
        """Wait (as an idle connection) up to ``timeout`` for the next request to start."""
        with self._lock:
            stop = self._wake_reader
            if self._stopping.is_set() or stop is None:
                return False
        # ``stop`` stays open while ``conn`` is registered (see ``_close_wake_if_done``).
        return _wait_readable(conn.sock, stop, timeout) and not self._stopping.is_set()

    def _serve_request(
        self, conn: _Connection, buffered: bytes, started: float
    ) -> tuple[bytes, bool]:
        """Read, dispatch and answer one request; return ``(unread input, keep alive)``."""
        cfg = self._config
        try:
            head, buffered = _read_head(
                conn.sock,
                timeout=min(cfg.read_header_timeout, cfg.read_timeout)
                - (time.monotonic() - started),
                max_bytes=cfg.max_header_bytes,
                initial=buffered,
            )
            if not head:
                return b"", False
            request = _parse_head(head)
            write_deadline = time.monotonic() + cfg.write_timeout
            body, buffered = _read_body(
                conn.sock, request.content_length, buffered, deadline=started + cfg.read_timeout
            )
        except (TimeoutError, _HeaderTooLarge, _HTTPError) as exc:
            self._send_error(conn.sock, _error_status(exc))
            return b"", False
        response = self._dispatch(request, body, conn.peer)
        keep_alive = request.keep_alive and not self._stopping.is_set()
        data = _encode_response(response, keep_alive=keep_alive, head_only=request.method == "HEAD")
        _send_before(conn.sock, data, write_deadline)
        return buffered, keep_alive

    def _send_error(self, sock: socket.socket, code: int) -> None:
        data = _encode_response(_error_response(code), keep_alive=False, head_only=False)
        _send_before(sock, data, time.monotonic() + self._config.write_timeout)

    def _dispatch(self, request: _Request, body: bytes, peer: tuple[Any, ...]) -> _Response:
        """Run the app for ``request``; an application failure becomes a bare 500."""
        environ = _environ(request, body, server=self.address or ("", 0), peer=peer)
        app = self._route(environ["PATH_INFO"])
        try:
            return _run_app(app, environ)
        except Exception as exc:
            # Only the type: messages and tracebacks may carry sensitive data.
            _logger.error(
                "observability server: handler for %s failed: %s",
                environ["PATH_INFO"],
                type(exc).__qualname__,
            )
            return _error_response(500)

    def _route(self, path: str) -> WSGIApp:
        cfg = self._config
        if path == "/metrics" and cfg.metrics:
            return self._metrics
        if path in ("/health", "/ready"):
            return self._health
        if path.startswith(_PPROF_PREFIX) and cfg.pprof and cfg.profile_handler is not None:
            return cfg.profile_handler
        return _not_found_app


def _error_status(exc: Exception, /) -> int:
    """The status code answering a failed request read."""
    if isinstance(exc, _HTTPError):
        return exc.status
    if isinstance(exc, _HeaderTooLarge):
        return 431
    return 408
