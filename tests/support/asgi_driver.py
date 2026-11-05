"""A minimal ASGI 3 driver for HTTP, websocket and lifespan scopes.

.. code-block:: python

    resp = await call_asgi(app, "GET", "/users/1", headers={"traceparent": tp})
    assert resp.status == 200 and resp.body == b"ok"
    assert resp.informational == [103]      # 1xx responses seen before the final one

    resp = run_asgi(app, "POST", "/x", body=[b"chunk1", b"chunk2"])  # sync wrapper

Request bodies may be one ``bytes`` or a list of chunks (sent with
``more_body``). After the body, ``receive()`` waits until the response is
complete and then returns ``http.disconnect``. Exceptions raised by the app
propagate to the caller (the partial response is attached as
``exc.asgi_response``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ASGIResponse",
    "LifespanResult",
    "WebSocketResult",
    "call_asgi",
    "call_lifespan",
    "call_websocket",
    "make_scope",
    "run_asgi",
]

ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]


def _encode_headers(
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
) -> list[tuple[bytes, bytes]]:
    items = headers.items() if isinstance(headers, Mapping) else (headers or [])
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in items]


def make_scope(
    method: str = "GET",
    path: str = "/",
    *,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    scheme: str = "http",
    host: str = "testserver",
    port: int | None = None,
    client: tuple[str, int] = ("127.0.0.1", 50000),
    root_path: str = "",
    scope_type: str = "http",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an ASGI ``http`` (or ``websocket``) scope. ``path`` may include ``?query``."""
    path_part, _, query = path.partition("?")
    if port is None:
        port = 443 if scheme in ("https", "wss") else 80
    raw_headers = _encode_headers(headers)
    if not any(k == b"host" for k, _ in raw_headers):
        raw_headers.insert(0, (b"host", f"{host}:{port}".encode()))
    scope: dict[str, Any] = {
        "type": scope_type,
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": scheme,
        "path": path_part or "/",
        "raw_path": (path_part or "/").encode(),
        "query_string": query.encode(),
        "root_path": root_path,
        "headers": raw_headers,
        "client": client,
        "server": (host, port),
        "state": {},
    }
    if scope_type == "http":
        scope["method"] = method.upper()
    if extra:
        scope.update(extra)
    return scope


@dataclass
class ASGIResponse:
    """Messages the app sent for one HTTP request."""

    status: int = 0
    """The final (non-1xx) status, 0 if none was sent."""
    informational: list[int] = field(default_factory=list)
    """1xx statuses from ``http.response.start`` messages before the final one."""
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    chunks: list[bytes] = field(default_factory=list)
    complete: bool = False
    """Whether a body message with ``more_body=False`` was sent."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def header(self, name: str) -> str | None:
        lname = name.lower().encode("latin-1")
        for key, value in self.headers:
            if key.lower() == lname:
                return value.decode("latin-1")
        return None


async def call_asgi(
    app: ASGIApp,
    method: str = "GET",
    path: str = "/",
    *,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    body: bytes | Iterable[bytes] = b"",
    scope: Mapping[str, Any] | None = None,
    on_message: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    **scope_kwargs: Any,
) -> ASGIResponse:
    """Run one HTTP request through ``app``.

    ``scope`` entries override the generated scope; ``scope_kwargs`` go to
    :func:`make_scope`. ``on_message`` is called for every message the app
    sends (await-able), e.g. to observe timing while streaming.
    """
    sc = make_scope(method, path, headers=headers, **scope_kwargs)
    if scope:
        sc.update(scope)
    chunks = [body] if isinstance(body, bytes) else list(body)
    if not chunks:
        chunks = [b""]
    pending: list[dict[str, Any]] = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    resp = ASGIResponse(scope=sc)
    done = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if pending:
            return pending.pop(0)
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        msg = dict(message)
        resp.messages.append(msg)
        kind = msg.get("type")
        if kind == "http.response.start":
            status = int(msg["status"])
            if 100 <= status < 200:
                resp.informational.append(status)
            else:
                if resp.status:
                    raise AssertionError("http.response.start sent twice")
                resp.status = status
                resp.headers = [(bytes(k), bytes(v)) for k, v in msg.get("headers", [])]
        elif kind == "http.response.body":
            if not resp.status:
                raise AssertionError("http.response.body before http.response.start")
            resp.chunks.append(bytes(msg.get("body", b"")))
            if not msg.get("more_body", False):
                resp.complete = True
                done.set()
        if on_message is not None:
            result = on_message(msg)
            if result is not None:
                await result

    try:
        await app(sc, receive, send)
    except BaseException as exc:
        with contextlib.suppress(AttributeError):
            exc.asgi_response = resp  # type: ignore[attr-defined]
        raise
    finally:
        done.set()
    return resp


def run_asgi(app: ASGIApp, method: str = "GET", path: str = "/", **kwargs: Any) -> ASGIResponse:
    """Synchronous wrapper around :func:`call_asgi` (``asyncio.run``)."""
    return asyncio.run(call_asgi(app, method, path, **kwargs))


@dataclass
class LifespanResult:
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.messages]


async def call_lifespan(app: ASGIApp, *, state: dict[str, Any] | None = None) -> LifespanResult:
    """Send ``lifespan.startup`` then ``lifespan.shutdown``; records the app's replies."""
    sc: dict[str, Any] = {
        "type": "lifespan",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "state": state if state is not None else {},
    }
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})
    result = LifespanResult()

    async def receive() -> dict[str, Any]:
        return await inbox.get()

    async def send(message: Any) -> None:
        msg = dict(message)
        result.messages.append(msg)
        if msg["type"].startswith("lifespan.startup."):
            await inbox.put({"type": "lifespan.shutdown"})

    await app(sc, receive, send)
    return result


@dataclass
class WebSocketResult:
    messages: list[dict[str, Any]] = field(default_factory=list)
    """Messages the app sent (``websocket.accept``, ``websocket.send``, ``websocket.close``)."""

    @property
    def accepted(self) -> bool:
        return any(m["type"] == "websocket.accept" for m in self.messages)

    @property
    def sent(self) -> list[str | bytes]:
        return [
            m.get("text") if m.get("text") is not None else m.get("bytes", b"")
            for m in self.messages
            if m["type"] == "websocket.send"
        ]


async def call_websocket(
    app: ASGIApp,
    path: str = "/ws",
    *,
    incoming: Iterable[str | bytes] = (),
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    close_code: int = 1000,
    **scope_kwargs: Any,
) -> WebSocketResult:
    """Connect, deliver ``incoming`` frames once accepted, then disconnect."""
    sc = make_scope(
        "GET", path, headers=headers, scope_type="websocket", scheme="ws", **scope_kwargs
    )
    frames: list[dict[str, Any]] = []
    for item in incoming:
        frame: dict[str, Any] = {"type": "websocket.receive"}
        frame["text" if isinstance(item, str) else "bytes"] = item
        frames.append(frame)
    connected = False
    disconnected = False
    settled = asyncio.Event()  # set once the app accepted or closed
    accepted = asyncio.Event()
    closed = asyncio.Event()
    result = WebSocketResult()

    async def receive() -> dict[str, Any]:
        nonlocal connected, disconnected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await settled.wait()
        if accepted.is_set() and frames and not closed.is_set():
            return frames.pop(0)
        if not disconnected:
            disconnected = True
            return {"type": "websocket.disconnect", "code": close_code}
        raise RuntimeError("receive() called after websocket.disconnect")

    async def send(message: Any) -> None:
        msg = dict(message)
        result.messages.append(msg)
        if msg["type"] == "websocket.accept":
            accepted.set()
            settled.set()
        elif msg["type"] == "websocket.close":
            closed.set()
            settled.set()

    await app(sc, receive, send)
    return result
