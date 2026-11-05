"""WSGI middleware (Go: ``HTTPMiddleware``) for Flask, Django, Falcon, Pyramid, ...

.. code-block:: python

    app = Middleware(app, operation_name="http.request",
                     additional_labels=["user_type"],
                     additional_attrs=lambda environ: {"user_type": ...},
                     route=lambda environ: environ.get("pyrock.route"))

Opens a ``SERVER`` operation per request with ``http.method``, ``http.route``,
``http.scheme``, ``http.host`` and ``http.status_code`` (``http.user_agent``
only when the privacy policy allows it); default labels ``http_method``,
``http_route``, ``http_status_code``. The final status is the last
``start_response`` call. Duration ends when the response iterable's
``close()`` runs. ``http.route`` is a template or ``"_"``, never a raw path.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Mapping

from pyrock._types import StartResponse, WSGIApp, WSGIEnviron
from pyrock.attr import AttrValue

__all__ = ["ROUTE_KEY", "Middleware"]

ROUTE_KEY = "pyrock.route"
"""``environ`` key a framework hook may set to the matched route template."""


class Middleware:
    """Wraps a WSGI app with an operation per request."""

    def __init__(
        self,
        app: WSGIApp,
        /,
        *,
        operation_name: str = "http.request",
        additional_labels: Iterable[str] = (),
        additional_attrs: Callable[[WSGIEnviron], Mapping[str, AttrValue]] | None = None,
        success_codes: Container[int] | None = None,
        trace_propagation: bool = True,
        route: Callable[[WSGIEnviron], str | None] | None = None,
    ) -> None:
        """``success_codes=None`` treats every status below 400 as success."""
        raise NotImplementedError

    def __call__(self, environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        raise NotImplementedError
