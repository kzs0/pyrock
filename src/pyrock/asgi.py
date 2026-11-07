"""ASGI 3 middleware for FastAPI, Starlette, Litestar, ...

.. code-block:: python

    app = Middleware(app, operation_name="http.request")
    # FastAPI / Starlette: app.add_middleware(Middleware)

Same attributes, labels and route rules as :mod:`pyrock.wsgi`. The final
status is taken from ``http.response.start``, skipping informational 1xx
responses; the duration ends at the last body message (``more_body=False``).
``lifespan`` scopes pass through untouched; ``websocket`` scopes pass through
unless ``websocket=True``, which covers the whole connection as one operation.
Route templates come from ``route=``, then ``scope["route"].path_format``
(Starlette/FastAPI), then ``scope["pyrock.route"]``, else ``"_"``.

``http.user_agent`` is emitted only when allowlisted in the privacy policy's
``attribute_keys``. An exception after ``http.response.start`` marks failure,
keeps the status already sent as ``http.status_code``, and is re-raised.

The operation is active in the task that runs the app (and so in the tasks
and ``asyncio.to_thread`` calls it starts) until the app returns. Nothing here
blocks: the middleware only wraps ``send`` and awaits the app.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Callable, Container, Iterable, Mapping
from typing import TYPE_CHECKING

from pyrock._httpcore import ROUTE_KEY, Instrumentation, RequestInfo
from pyrock._types import ASGIApp, ASGIMessage, ASGIReceive, ASGIScope, ASGISend

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock
    from pyrock._operation import Operation
    from pyrock.attr import AttrValue

__all__ = ["ROUTE_KEY", "Middleware"]

HTTP_OPERATION = "http.request"
WEBSOCKET_OPERATION = "http.websocket"


class Middleware:
    """Wraps an ASGI app with an operation per HTTP request (and optionally websocket)."""

    __slots__ = (
        "_app",
        "_instrumentation",
        "_operation_name",
        "_tracked",
        "_tracked_lock",
        "_websocket",
    )

    def __init__(
        self,
        app: ASGIApp,
        /,
        *,
        operation_name: str | None = None,
        additional_labels: Iterable[str] = (),
        additional_attrs: Callable[[ASGIScope], Mapping[str, AttrValue]] | None = None,
        success_codes: Container[int] | None = None,
        trace_propagation: bool = True,
        route: Callable[[ASGIScope], str | None] | None = None,
        websocket: bool = False,
        bedrock: Bedrock | None = None,
    ) -> None:
        """``success_codes=None`` treats every status below 400 as success.

        ``operation_name=None`` names HTTP operations ``"http.request"`` and
        websocket operations ``"http.websocket"``; an explicit name is used for
        both. With ``websocket=True`` each connection is one ``SERVER``
        operation with ``http.route`` and ``http.scheme`` (``ws``/``wss``); it
        ends when the connection closes. Websocket operations use the same
        metric labels as HTTP requests, with ``"_"`` for ``http_method`` and
        ``http_status_code``, so a shared ``operation_name`` keeps one schema.

        The running event loop is passed to the instance's ``track_loop`` once
        per loop (on lifespan startup and on the first traced request), so
        asyncio task metrics and profiles cover it.

        The instance is captured when the middleware is constructed: ``bedrock`` if given, else
        ``pyrock.current()`` at that point. If that is the noop, ``current()`` is
        resolved per request instead. Each request runs inside a context with that
        instance active, so handler code on server worker threads is instrumented
        (there is no process-wide default instance).

        A ``route`` callable returning ``None`` falls back to the framework
        route key, then to ``"_"``.
        """
        self._app = app
        self._operation_name = operation_name
        self._websocket = websocket
        self._instrumentation: Instrumentation[ASGIScope] = Instrumentation(
            additional_labels=additional_labels,
            additional_attrs=additional_attrs,
            success_codes=success_codes,
            trace_propagation=trace_propagation,
            route=route,
            bedrock=bedrock,
        )
        # The instance each loop was last tracked with (loops are held weakly).
        self._tracked: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Bedrock] = (
            weakref.WeakKeyDictionary()
        )
        self._tracked_lock = threading.Lock()

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self._app(scope, self._lifespan_receive(receive), send)
            return
        traced = kind == "http" or (kind == "websocket" and self._websocket)
        bedrock = self._instrumentation.instance() if traced else None
        if bedrock is None or bedrock.is_noop:
            await self._app(scope, receive, send)
            return
        self._track_running_loop(bedrock)
        if kind == "http":
            await _HTTPExchange(self._instrumentation, scope).run(
                self._app, bedrock, self._operation_name or HTTP_OPERATION, receive, send
            )
        else:
            await self._websocket_connection(bedrock, scope, receive, send)

    def _lifespan_receive(self, receive: ASGIReceive) -> ASGIReceive:
        """Wrap ``receive`` to track the loop when ``lifespan.startup`` arrives."""

        async def lifespan_receive() -> ASGIMessage:
            message = await receive()
            if message.get("type") == "lifespan.startup":
                bedrock = self._instrumentation.instance()
                if not bedrock.is_noop:
                    self._track_running_loop(bedrock)
            return message

        return lifespan_receive

    def _track_running_loop(self, bedrock: Bedrock) -> None:
        """Call ``bedrock.track_loop`` for the running loop, once per (loop, instance)."""
        loop = asyncio.get_running_loop()
        if self._tracked.get(loop) is bedrock:
            return
        with self._tracked_lock:
            if self._tracked.get(loop) is bedrock:
                return
            bedrock.track_loop(loop)
            self._tracked[loop] = bedrock

    async def _websocket_connection(
        self, bedrock: Bedrock, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        """Run a websocket connection as one operation, ended when the app returns."""
        instrumentation = self._instrumentation
        op = instrumentation.start(
            bedrock,
            self._operation_name or WEBSOCKET_OPERATION,
            scope,
            _scope_info(scope),
        )
        activation = op.activate()
        error: BaseException | None = None
        try:
            await self._app(scope, receive, send)
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                instrumentation.finish_connection(
                    op, scope, framework_route=_framework_route(scope), error=error
                )
            finally:
                op.deactivate(activation)


def _scope_info(scope: ASGIScope) -> RequestInfo:
    """Request details from one pass over the scope's headers (which are not copied)."""
    headers = scope.get("headers", ())
    host: str | None = None
    agent: str | None = None
    for key, value in headers:
        name = bytes(key).lower()
        if name == b"host":
            if host is None:
                host = bytes(value).decode("latin-1")
        elif name == b"user-agent" and agent is None:
            agent = bytes(value).decode("latin-1")
    if host is None:
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else ""
    websocket = scope.get("type") == "websocket"
    return RequestInfo(
        method=None if websocket else str(scope.get("method", "GET")),
        scheme=str(scope.get("scheme", "ws" if websocket else "http")),
        host=host,
        user_agent=agent,
        headers=headers,
    )


def _framework_route(scope: ASGIScope) -> str | None:
    """``scope["route"].path_format`` (Starlette/FastAPI), else ``scope[ROUTE_KEY]``."""
    template = getattr(scope.get("route"), "path_format", None)
    if isinstance(template, str):
        return template
    template = scope.get(ROUTE_KEY)
    return template if isinstance(template, str) else None


class _HTTPExchange:
    """One HTTP request: wraps ``send`` to track the status and the end of the body."""

    __slots__ = ("_instrumentation", "_op", "_scope", "_status")

    def __init__(self, instrumentation: Instrumentation[ASGIScope], scope: ASGIScope) -> None:
        self._instrumentation = instrumentation
        self._scope = scope
        self._status: int | None = None
        self._op: Operation | None = None

    async def run(
        self, app: ASGIApp, bedrock: Bedrock, name: str, receive: ASGIReceive, send: ASGISend
    ) -> None:
        op = self._instrumentation.start(bedrock, name, self._scope, _scope_info(self._scope))
        self._op = op
        activation = op.activate()

        async def traced_send(message: ASGIMessage) -> None:
            await send(message)
            self._sent(message)

        try:
            await app(self._scope, receive, traced_send)
        except BaseException as exc:
            self._finish(exc)
            raise
        else:
            self._finish(None)
        finally:
            op.deactivate(activation)

    def _sent(self, message: ASGIMessage) -> None:
        """Record a message the server accepted: the final status, or the last body chunk."""
        kind = message.get("type")
        if kind == "http.response.start":
            status = int(message["status"])
            if status >= 200:
                self._status = status
        elif kind == "http.response.body" and not message.get("more_body", False):
            self._finish(None)

    def _finish(self, error: BaseException | None) -> None:
        """End the operation once (status ``500`` when no response was started)."""
        op = self._op
        if op is None or op.ended:
            return
        self._instrumentation.finish(
            op,
            self._scope,
            framework_route=_framework_route(self._scope),
            status=self._status,
            error=error,
        )
