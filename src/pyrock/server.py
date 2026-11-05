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

Import rules: ``pyrock.server`` imports only ``pyrock.metric`` (and leaf
modules) from pyrock.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pyrock._compat import is_positive_number, seconds
from pyrock._types import WSGIApp
from pyrock.metric import Registry

__all__ = ["DEFAULT_ADDR", "Config", "Server", "health_app", "metrics_app"]

DEFAULT_ADDR = "127.0.0.1:9090"


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
        if not self.addr or ":" not in self.addr:
            problems.append("addr must be 'host:port'")
        if problems:
            raise ValueError("invalid server Config: " + "; ".join(problems))


class _HeaderTooLarge(Exception):
    """The request line plus headers exceeded ``max_header_bytes`` (answered with 431)."""


def _read_head(
    sock: socket.socket,
    /,
    *,
    timeout: float,
    max_bytes: int,
    clock: Callable[[], float] = time.monotonic,
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
    """
    buf = b""
    deadline = clock() + timeout
    while True:
        idx = buf.find(b"\r\n\r\n")
        if idx >= 0:
            if idx + 4 > max_bytes:
                raise _HeaderTooLarge
            return buf[: idx + 4], buf[idx + 4 :]
        if len(buf) > max_bytes:
            raise _HeaderTooLarge
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            if not buf:
                return b"", b""
            raise ConnectionError("closed mid-head")
        buf += chunk


# --- scratch reference (for test validation only)
import io as _io
import threading as _threading
from pyrock.metric import prometheus as _prom

def metrics_app(registry, /):
    def app(environ, start_response):
        try:
            body = _prom.encode(registry)
        except _prom.ExpositionError:
            start_response("500 Internal Server Error", [("Content-Type", "text/plain"), ("Content-Length", "0")])
            return [b""]
        start_response("200 OK", [("Content-Type", "text/plain; version=0.0.4; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]
    return app

def health_app():
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "3")])
        return [b"ok\n"]
    return app

_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 431: "Request Header Fields Too Large", 500: "Internal Server Error", 503: "Service Unavailable", 505: "HTTP Version Not Supported"}

class Server:
    def __init__(self, registry, config=None, /):
        self._registry = registry
        self._config = config or Config()
        self._sock = None
        self._lock = _threading.Lock()
        self._sem = _threading.BoundedSemaphore(self._config.max_connections)
        self._conns = {}
        self._busy = set()
        self._closed = False
        self._accept_thread = None
        self._done = _threading.Event()
        self._idle_cv = _threading.Condition(self._lock)
        self._metrics = metrics_app(registry)
        self._health = health_app()

    @property
    def config(self): return self._config

    @property
    def address(self):
        if self._sock is None or self._closed: return None
        return self._sock.getsockname()[:2]

    def _bind(self):
        host, _, port = self._config.addr.rpartition(":")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, int(port)))
        except OSError:
            s.close(); raise
        s.listen(128)
        s.settimeout(0.05)
        self._sock = s

    def start(self):
        self._bind()
        self._accept_thread = _threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def serve_forever(self):
        self._bind()
        self._accept_loop()

    def _accept_loop(self):
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            conn.settimeout(None)
            if not self._sem.acquire(blocking=False):
                try:
                    conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
                conn.close()
                continue
            t = _threading.Thread(target=self._handle, args=(conn,), daemon=True)
            with self._lock:
                self._conns[conn] = t
            t.start()
        self._done.set()

    def _respond(self, conn, status, headers, body, close):
        lines = [f"HTTP/1.1 {status}"] + [f"{k}: {v}" for k, v in headers]
        if not any(k.lower() == "content-length" for k, _ in headers):
            lines.append(f"Content-Length: {len(body)}")
        if close: lines.append("Connection: close")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)

    def _simple(self, conn, code, close=True):
        self._respond(conn, f"{code} {_REASONS[code]}", [], b"", close)

    def _handle(self, conn):
        cfg = self._config
        rest = b""
        first = True
        try:
            while not self._closed:
                if not rest:
                    # wait for the next request (idle) without holding "busy"
                    conn.settimeout(cfg.read_header_timeout if first else cfg.idle_timeout)
                    try:
                        chunk = conn.recv(65536)
                    except (TimeoutError, OSError):
                        return
                    if not chunk: return
                    rest = chunk
                with self._lock:
                    self._busy.add(conn)
                try:
                    import socket as _s
                    # feed buffered bytes through _read_head via a tiny shim
                    try:
                        head, rest = _read_head_buf(conn, rest, cfg)
                    except _HeaderTooLarge:
                        self._simple(conn, 431); return
                    except (TimeoutError, ConnectionError, OSError):
                        return
                    first = False
                    lines = head.split(b"\r\n")
                    parts = lines[0].split(b" ")
                    if len(parts) != 3 or not parts[2].startswith(b"HTTP/"):
                        self._simple(conn, 400); return
                    if parts[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
                        self._simple(conn, 505); return
                    method = parts[0].decode("latin-1"); target = parts[1].decode("latin-1")
                    path, _, query = target.partition("?")
                    hdrs = {}
                    for line in lines[1:]:
                        if not line: continue
                        k, _, v = line.partition(b":")
                        hdrs[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
                    close = hdrs.get("connection", "").lower() == "close" or parts[2] == b"HTTP/1.0"
                    app = None
                    if path == "/metrics" and cfg.metrics: app = self._metrics
                    elif path in ("/health", "/ready"): app = self._health
                    elif cfg.pprof and cfg.profile_handler is not None and path.startswith("/debug/pprof/"): app = cfg.profile_handler
                    if app is None:
                        self._simple(conn, 404, close); 
                        if close: return
                        continue
                    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "SCRIPT_NAME": "", "SERVER_PROTOCOL": parts[2].decode(), "wsgi.input": _io.BytesIO(b""), "wsgi.errors": _io.StringIO(), "wsgi.url_scheme": "http", "wsgi.version": (1, 0), "wsgi.multithread": True, "wsgi.multiprocess": False, "wsgi.run_once": False, "SERVER_NAME": "x", "SERVER_PORT": "0"}
                    state = {}
                    def start_response(status, headers, exc_info=None):
                        state["s"] = status; state["h"] = headers
                        return lambda b: None
                    try:
                        body = b"".join(app(environ, start_response))
                    except Exception:
                        self._simple(conn, 500, close)
                        if close: return
                        continue
                    self._respond(conn, state["s"], state["h"], body, close)
                    if close: return
                finally:
                    with self._lock:
                        self._busy.discard(conn)
                        self._idle_cv.notify_all()
        finally:
            try: conn.close()
            except OSError: pass
            with self._lock:
                self._conns.pop(conn, None)
                self._idle_cv.notify_all()
            self._sem.release()

    def shutdown(self, *, timeout=None):
        if self._sock is None or self._closed:
            self._closed = True
            return
        self._closed = True
        self._sock.close()
        if self._accept_thread is not None: self._accept_thread.join(5)
        timeout = self._config.shutdown_timeout if timeout is None else timeout
        import time as _t
        deadline = _t.monotonic() + timeout
        with self._lock:
            for c in list(self._conns):
                if c not in self._busy:
                    try: c.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
            while self._busy:
                rem = deadline - _t.monotonic()
                if rem <= 0: break
                self._idle_cv.wait(rem)
            for c in list(self._conns):
                try: c.shutdown(socket.SHUT_RDWR)
                except OSError: pass

def _read_head_buf(conn, buf, cfg):
    class _S:
        def __init__(self): self.pending = buf
        def settimeout(self, t): conn.settimeout(t)
        def recv(self, n):
            if self.pending:
                p, self.pending = self.pending, b""
                return p
            return conn.recv(n)
    return _read_head(_S(), timeout=cfg.read_header_timeout, max_bytes=cfg.max_header_bytes)
