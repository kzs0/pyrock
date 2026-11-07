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

from collections.abc import Callable, Container, Iterable, Mapping
from typing import TYPE_CHECKING

from pyrock._types import ASGIApp, ASGIMessage, ASGIReceive, ASGIScope, ASGISend
from pyrock.attr import AttrValue
from pyrock.wsgi import Instrumentation, RequestInfo

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock
    from pyrock._operation import Operation

__all__ = ["ROUTE_KEY", "Middleware"]

ROUTE_KEY = "pyrock.route"
"""Scope key a framework hook may set to the matched route template."""

HTTP_OPERATION = "http.request"
WEBSOCKET_OPERATION = "http.websocket"


class Middleware:
    """Wraps an ASGI app with an operation per HTTP request (and optionally websocket)."""

    __slots__ = ("_app", "_instrumentation", "_operation_name", "_websocket", "_websocket_labels")

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
        ends when the connection closes.

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
        # A connection has no method or status, so websocket metrics omit those labels.
        self._websocket_labels = tuple(
            label
            for label in self._instrumentation.labels
            if label not in ("http.method", "http.status_code")
        )

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        kind = scope.get("type")
        traced = kind == "http" or (kind == "websocket" and self._websocket)
        bedrock = self._instrumentation.instance() if traced else None
        if bedrock is None or bedrock.is_noop:
            await self._app(scope, receive, send)
        elif kind == "http":
            await _HTTPExchange(self._instrumentation, scope).run(
                self._app, bedrock, self._operation_name or HTTP_OPERATION, receive, send
            )
        else:
            await self._websocket_connection(bedrock, scope, receive, send)

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
            labels=self._websocket_labels,
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


def _header(scope: ASGIScope, name: bytes) -> str | None:
    """The first value of header ``name`` (lowercase) in the scope, decoded as latin-1."""
    for key, value in scope.get("headers", ()):
        if bytes(key).lower() == name:
            return bytes(value).decode("latin-1")
    return None


def _scope_info(scope: ASGIScope) -> RequestInfo:
    host = _header(scope, b"host")
    if host is None:
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else ""
    websocket = scope.get("type") == "websocket"
    return RequestInfo(
        method=None if websocket else str(scope.get("method", "GET")),
        scheme=str(scope.get("scheme", "ws" if websocket else "http")),
        host=host,
        user_agent=_header(scope, b"user-agent"),
        headers=[(bytes(k), bytes(v)) for k, v in scope.get("headers", ())],
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
