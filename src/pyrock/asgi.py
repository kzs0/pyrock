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
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Mapping

from pyrock._types import ASGIApp, ASGIReceive, ASGIScope, ASGISend
from pyrock.attr import AttrValue

__all__ = ["ROUTE_KEY", "Middleware"]

ROUTE_KEY = "pyrock.route"


class Middleware:
    """Wraps an ASGI app with an operation per HTTP request (and optionally websocket)."""

    def __init__(
        self,
        app: ASGIApp,
        /,
        *,
        operation_name: str = "http.request",
        additional_labels: Iterable[str] = (),
        additional_attrs: Callable[[ASGIScope], Mapping[str, AttrValue]] | None = None,
        success_codes: Container[int] | None = None,
        trace_propagation: bool = True,
        route: Callable[[ASGIScope], str | None] | None = None,
        websocket: bool = False,
    ) -> None:
        raise NotImplementedError

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        raise NotImplementedError
