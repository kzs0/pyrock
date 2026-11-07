"""Optional ``/debug/pprof/*`` WSGI handler serving gzipped pprof.

.. code-block:: python

    from pyrock.profile import debug
    pyrock.init(server_pprof=True, server_profile_handler=debug.app())

Endpoints: ``profile?seconds=N`` (``N`` above ``max_seconds`` is rejected with
``400``, as in Go), ``heap``,
``goroutine`` (threads and tasks; ``?debug=1`` returns text), ``mutex`` and
``block``. GET only (others: 405). Importing ``pyrock`` registers nothing; this
module must be imported explicitly (like Go's ``net/http/pprof``).

``profile`` samples CPU for ``seconds`` (default: ``max_seconds``) on the
request thread and answers ``503`` while another sampler owns the process.
``heap`` reports allocations since the previous ``heap`` request of the same
app; it needs tracemalloc to be tracing already (for example through HEAP
export or ``PYTHONTRACEMALLOC``), otherwise the profile is empty with a note.
Full source paths are served: protect these endpoints like Go's.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from pyrock._types import StartResponse, WSGIApp, WSGIEnviron
from pyrock.profile import _locks
from pyrock.profile._heap import HeapProfiler
from pyrock.profile._pprof import Profile, encode_gzip
from pyrock.profile._sampler import SamplingProfiler
from pyrock.profile._stacks import format_text, thread_profile

if TYPE_CHECKING:
    import asyncio

__all__ = ["app"]

_ENDPOINTS = ("profile", "heap", "goroutine", "mutex", "block")


class _BadRequest(Exception):
    """A query parameter is invalid (answered with ``400``)."""


def _respond(
    start_response: StartResponse, status: str, body: bytes, content_type: str
) -> list[bytes]:
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("Cache-Control", "no-store"),
    ]
    if content_type == "application/octet-stream":
        headers.append(("Content-Disposition", 'attachment; filename="profile"'))
    start_response(status, headers)
    return [body]


def _text(start_response: StartResponse, status: str, text: str) -> list[bytes]:
    return _respond(start_response, status, text.encode(), "text/plain; charset=utf-8")


def _query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[-1] if values else None


def _seconds(query: dict[str, list[str]], max_seconds: float) -> float:
    raw = _query_value(query, "seconds")
    if raw is None:
        return max_seconds
    try:
        value = float(raw)
    except ValueError:
        raise _BadRequest(f"seconds must be a number within (0, {max_seconds:g}]") from None
    if not (math.isfinite(value) and 0 < value <= max_seconds):
        raise _BadRequest(f"seconds must be a number within (0, {max_seconds:g}]")
    return value


def _debug_level(query: dict[str, list[str]]) -> int:
    raw = _query_value(query, "debug")
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        raise _BadRequest("debug must be an integer") from None


def app(
    *,
    max_seconds: float = 30.0,
    prefix: str = "/debug/pprof",
    loops: Callable[[], Iterable[asyncio.AbstractEventLoop]] | None = None,
) -> WSGIApp:
    """Return the WSGI app handling ``{prefix}/*``.

    ``loops`` supplies the event loops whose pending tasks ``goroutine`` lists.
    """
    if not (math.isfinite(max_seconds) and max_seconds > 0):
        raise ValueError("max_seconds must be positive")
    base = prefix.rstrip("/")
    heap = HeapProfiler()

    def collect(name: str, query: dict[str, list[str]]) -> Profile | str | None:
        """The profile (or ``goroutine?debug`` text); ``None`` when the sampler is busy."""
        if name == "profile":  # ``seconds`` is validated only where it is used
            return SamplingProfiler(cpu=True).collect(_seconds(query, max_seconds))
        if name == "heap":
            return heap.snapshot()
        if name == "goroutine":
            current = loops() if loops else ()
            if _debug_level(query) > 0:
                return format_text(loops=current)
            return thread_profile(loops=current)
        return _locks.mutex_profile() if name == "mutex" else _locks.block_profile()

    def handle(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        path = environ.get("PATH_INFO") or "/"
        if path in (base, base + "/"):
            name = ""
        elif path.startswith(base + "/") and path[len(base) + 1 :] in _ENDPOINTS:
            name = path[len(base) + 1 :]
        else:
            return _text(start_response, "404 Not Found", "404 page not found\n")
        if environ.get("REQUEST_METHOD") != "GET":
            start_response(
                "405 Method Not Allowed",
                [("Allow", "GET"), ("Content-Type", "text/plain; charset=utf-8")],
            )
            return [b"method not allowed\n"]
        if not name:
            return _text(start_response, "200 OK", "".join(f"{e}\n" for e in _ENDPOINTS))
        try:
            result = collect(name, parse_qs(environ.get("QUERY_STRING") or ""))
        except _BadRequest as exc:
            return _text(start_response, "400 Bad Request", f"{exc}\n")
        if result is None:
            return _text(start_response, "503 Service Unavailable", "profiling is busy\n")
        if isinstance(result, str):
            return _text(start_response, "200 OK", result)
        return _respond(start_response, "200 OK", encode_gzip(result), "application/octet-stream")

    return handle
