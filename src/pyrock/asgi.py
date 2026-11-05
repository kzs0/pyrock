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
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Mapping
from typing import TYPE_CHECKING

from pyrock._types import ASGIApp, ASGIReceive, ASGIScope, ASGISend
from pyrock.attr import AttrValue

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock

__all__ = ["ROUTE_KEY", "Middleware"]

ROUTE_KEY = "pyrock.route"


class Middleware:
    """Wraps an ASGI app with an operation per HTTP request (and optionally websocket)."""

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
        raise NotImplementedError

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        raise NotImplementedError
