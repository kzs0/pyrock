"""A minimal WSGI driver: call an app like a server would, no framework needed.

.. code-block:: python

    resp = call_wsgi(app, "GET", "/users/1?x=1", headers={"traceparent": tp})
    assert resp.status_code == 200 and resp.body == b"ok"

    # Streaming: consume lazily and close yourself.
    resp = call_wsgi(app, "GET", "/stream", consume=False)
    first = resp.next_chunk()
    resp.close()          # runs the iterable's close(), like a real server

Exceptions raised by the app (or while iterating) propagate to the caller.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["WSGIResponse", "call_wsgi", "make_environ"]


def make_environ(
    method: str = "GET",
    path: str = "/",
    *,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    body: bytes = b"",
    scheme: str = "http",
    host: str = "testserver",
    port: int | None = None,
    remote_addr: str = "127.0.0.1",
    script_name: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a PEP 3333 environ. ``path`` may include ``?query``.

    Header names map to ``HTTP_*`` (``Content-Type``/``Content-Length`` to the
    bare CGI names); repeated headers are joined with ``","``.
    """
    path_part, _, query = path.partition("?")
    if port is None:
        port = 443 if scheme == "https" else 80
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method.upper(),
        "SCRIPT_NAME": script_name,
        "PATH_INFO": path_part or "/",
        "QUERY_STRING": query,
        "SERVER_NAME": host,
        "SERVER_PORT": str(port),
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": remote_addr,
        "CONTENT_LENGTH": str(len(body)) if body else "",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    items = headers.items() if isinstance(headers, Mapping) else (headers or [])
    has_host = False
    for name, value in items:
        key = name.upper().replace("-", "_")
        if key == "HOST":
            has_host = True
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            environ[key] = value
            continue
        cgi = "HTTP_" + key
        environ[cgi] = f"{environ[cgi]},{value}" if cgi in environ else value
    if not has_host:
        default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
        environ["HTTP_HOST"] = host if default_port else f"{host}:{port}"
    if extra:
        environ.update(extra)
    return environ


@dataclass
class WSGIResponse:
    """What the app produced. ``status`` is from the **last** ``start_response`` call."""

    status: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    chunks: list[bytes] = field(default_factory=list)
    """Body chunks in order (``write()`` calls first, then iterable items)."""
    start_response_calls: list[tuple[str, list[tuple[str, str]], Any]] = field(default_factory=list)
    closed: bool = False
    """Whether the iterable's ``close()`` was called."""
    environ: dict[str, Any] = field(default_factory=dict)
    _iterator: Iterator[bytes] | None = field(default=None, repr=False)
    _iterable: Iterable[bytes] | None = field(default=None, repr=False)

    @property
    def status_code(self) -> int:
        return int(self.status.split(" ", 1)[0]) if self.status else 0

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def header(self, name: str) -> str | None:
        lname = name.lower()
        for key, value in self.headers:
            if key.lower() == lname:
                return value
        return None

    def next_chunk(self) -> bytes | None:
        """Pull one chunk from the iterable (``None`` when exhausted)."""
        if self._iterator is None:
            return None
        try:
            chunk = next(self._iterator)
        except StopIteration:
            self._iterator = None
            return None
        self.chunks.append(chunk)
        return chunk

    def consume(self) -> bytes:
        """Pull every remaining chunk; returns the full body."""
        while self.next_chunk() is not None:
            pass
        return self.body

    def close(self) -> None:
        """Call the iterable's ``close()`` once (as a WSGI server must)."""
        if self.closed:
            return
        self.closed = True
        self._iterator = None
        close = getattr(self._iterable, "close", None)
        if close is not None:
            close()


def call_wsgi(
    app: Callable[..., Iterable[bytes]],
    method: str = "GET",
    path: str = "/",
    *,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    body: bytes = b"",
    environ: Mapping[str, Any] | None = None,
    consume: bool = True,
    **environ_kwargs: Any,
) -> WSGIResponse:
    """Call ``app`` and (by default) consume and close the response iterable.

    ``environ`` entries override the generated environ; ``environ_kwargs`` go
    to :func:`make_environ` (``scheme=``, ``host=``, ``port=`` ...). With
    ``consume=False`` the iterable is left open for :meth:`WSGIResponse.next_chunk`
    and :meth:`WSGIResponse.close`. If iteration raises, ``close()`` is still
    called and the exception propagates.
    """
    env = make_environ(method, path, headers=headers, body=body, **environ_kwargs)
    if environ:
        env.update(environ)
    resp = WSGIResponse(environ=env)

    def start_response(
        status: str, response_headers: list[tuple[str, str]], exc_info: Any = None
    ) -> Callable[[bytes], None]:
        if resp.start_response_calls and exc_info is None and resp.chunks:
            raise AssertionError("start_response called again after body without exc_info")
        resp.start_response_calls.append((status, list(response_headers), exc_info))
        resp.status = status
        resp.headers = list(response_headers)

        def write(data: bytes) -> None:
            resp.chunks.append(data)

        return write

    iterable = app(env, start_response)
    resp._iterable = iterable
    resp._iterator = iter(iterable)
    if consume:
        try:
            resp.consume()
        finally:
            resp.close()
    return resp
