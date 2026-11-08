"""A minimal raw ASGI 3 application instrumented with pyrock (no framework).

Serve it with any ASGI server, for example::

    pip install uvicorn                        # only for serving; pyrock does not depend on it
    uvicorn examples.asgi_app:app --port 8000
    curl http://127.0.0.1:8000/items/7

Or run the file directly: ``python examples/asgi_app.py`` drives a few
requests through the app in-process (no server, standard library only) and
prints the resulting operation metrics.

The instance is built at import time with :class:`pyrock.Bedrock` (live but
not activated) and passed to the middleware with ``bedrock=``; the middleware
activates it for every request, so handler code sees it whichever task or
thread the server runs it on. The lifespan ``shutdown`` event flushes and
stops it. The app sets ``scope["pyrock.route"]`` to the route template;
Starlette and FastAPI set ``scope["route"]`` instead, which the middleware
reads on its own.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import pyrock
from pyrock.asgi import ROUTE_KEY, Middleware
from pyrock.metric import prometheus

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

bedrock = pyrock.Bedrock(service="example-asgi")


async def respond(send: Send, status: int, body: bytes, content_type: str) -> None:
    headers = [(b"content-type", content_type.encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


@pyrock.step("load_item")
async def load_item(item_id: str) -> dict[str, str]:
    await asyncio.sleep(0)  # stand-in for real I/O
    pyrock.register({"item.id": item_id})
    return {"id": item_id, "name": f"item-{item_id}"}


async def lifespan(receive: Receive, send: Send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await bedrock.ashutdown()  # flush spans, final pushes, stop the server
            await send({"type": "lifespan.shutdown.complete"})
            return


async def application(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "lifespan":
        await lifespan(receive, send)
        return
    if scope["type"] != "http":
        return
    path: str = scope["path"]
    if path.startswith("/items/"):
        scope[ROUTE_KEY] = "/items/{item_id}"
        item = await load_item(path.removeprefix("/items/"))
        pyrock.info("item served", {"item.id": item["id"]})
        await respond(send, 200, json.dumps(item).encode(), "application/json")
    else:
        await respond(send, 404, b"not found\n", "text/plain")


app = Middleware(application, bedrock=bedrock)


async def _demo() -> None:
    """Drive two requests and a lifespan cycle through ``app`` without a server."""

    async def call(path: str) -> int:
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "query_string": b"",
            "headers": [(b"host", b"example.test")],
            "server": ("example.test", 80),
        }
        await app(scope, receive, send)
        return int(sent[0]["status"])

    statuses = [await call("/items/7"), await call("/missing")]
    sys.stdout.write(f"statuses: {statuses}\n")
    exposition = prometheus.encode(bedrock.metrics).decode()
    for line in exposition.splitlines():
        if line.startswith("http_request_count"):
            sys.stdout.write(line + "\n")
    events = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def lifespan_receive() -> Message:
        return next(events)

    async def lifespan_send(message: Message) -> None:
        return None

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, lifespan_receive, lifespan_send)


if __name__ == "__main__":
    asyncio.run(_demo())
