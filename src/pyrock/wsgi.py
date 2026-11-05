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

``http.user_agent`` is sensitive: it is emitted only when the policy's
``attribute_keys`` allowlist matches it (``"http.user_agent"`` or
``"http.*"``). An exception escaping the app marks failure with
``http.status_code=500`` and is re-raised; an exception after the response
has started (headers sent) also marks failure, but ``http.status_code`` keeps
the status already sent.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Mapping
from typing import TYPE_CHECKING

from pyrock._types import StartResponse, WSGIApp, WSGIEnviron
from pyrock.attr import AttrValue

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock

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
        bedrock: Bedrock | None = None,
    ) -> None:
        """``success_codes=None`` treats every status below 400 as success.

        The instance is captured when the middleware is constructed: ``bedrock`` if given, else
        ``pyrock.current()`` at that point. If that is the noop, ``current()`` is
        resolved per request instead. Each request runs inside a context with that
        instance active, so handler code on server worker threads is instrumented
        (there is no process-wide default instance).

        A ``route`` callable returning ``None`` falls back to
        ``environ[ROUTE_KEY]``, then to ``"_"``.
        """
        raise NotImplementedError

    def __call__(self, environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        raise NotImplementedError
