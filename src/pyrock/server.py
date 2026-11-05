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
    raise NotImplementedError


def metrics_app(registry: Registry, /) -> WSGIApp:
    """A WSGI app serving Prometheus text for ``registry`` (500, no body, on ExpositionError)."""
    raise NotImplementedError


def health_app() -> WSGIApp:
    """A WSGI app answering ``200 ok``."""
    raise NotImplementedError


class Server:
    """The observability server (Go: ``server.Server``)."""

    def __init__(self, registry: Registry, config: Config | None = None, /) -> None:
        raise NotImplementedError

    @property
    def config(self) -> Config:
        raise NotImplementedError

    @property
    def address(self) -> tuple[str, int] | None:
        """The bound ``(host, port)`` once started, else ``None``."""
        raise NotImplementedError

    def start(self) -> None:
        """Bind and serve on a daemon thread. ``OSError`` if the address is unavailable."""
        raise NotImplementedError

    def serve_forever(self) -> None:
        """Bind and serve on the calling thread until :meth:`shutdown`."""
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Stop accepting, wait for in-flight requests up to ``timeout``, close (idempotent)."""
        raise NotImplementedError
