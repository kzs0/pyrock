"""Recipe: Django with pyrock's WSGI middleware and route templates (copy into your project).

Requires ``django`` (``pip install django``) for this recipe only; pyrock
itself does not depend on it, and nothing in pyrock imports this file.

Two pieces:

1. ``wsgi.py``: initialise pyrock and wrap Django's WSGI application. Use
   ``log_install="pyrock"`` when Django's ``LOGGING`` setting owns the root
   logger, and add :class:`pyrock.logs.CorrelationFilter` to your handlers to
   get ``trace_id``/``span_id`` on every record::

       import pyrock
       from django.core.wsgi import get_wsgi_application
       from pyrock.wsgi import Middleware

       pyrock.init(service="shop", log_install="pyrock")
       application = Middleware(get_wsgi_application())

2. :class:`RouteTemplateMiddleware` below, in ``settings.MIDDLEWARE``. It
   copies the matched URL pattern (``users/<int:pk>/``) into
   ``request.META["pyrock.route"]``; ``request.META`` is the WSGI environ, so
   the pyrock middleware sees it when the request ends. Unmatched requests
   (404s) share the ``"_"`` route::

       MIDDLEWARE = [..., "myproject.observability.RouteTemplateMiddleware"]

With gunicorn or uWSGI in pre-fork mode, call ``pyrock.init()`` in the
worker's post-fork hook rather than at import time in the master.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, HttpResponse

from pyrock.wsgi import ROUTE_KEY


class RouteTemplateMiddleware:
    """Stores the resolved route template for :class:`pyrock.wsgi.Middleware`."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., Any],
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> None:
        match = request.resolver_match
        if match is not None and match.route:
            request.META[ROUTE_KEY] = "/" + match.route.lstrip("/")
