"""Instrumented HTTP client on ``urllib`` (Go: ``NewClient``/``Get``/``Post``/``Do``).

.. code-block:: python

    resp = pyrock.http.get("https://api.example.com/users")   # CLIENT span "HTTP GET"
    resp = pyrock.http.post(url, json={"name": "a"})
    opener = pyrock.http.build_opener(urllib.request.HTTPCookieProcessor())

Client spans record ``http.method``, ``http.host``, ``http.scheme`` and
``http.status_code``. Raw outgoing paths and URLs (``http.target``,
``http.url``) are emitted only when allowlisted in the privacy policy's
``attribute_keys``; credentials, query and fragment are always stripped, and
URL userinfo is never turned into auth. 4xx/5xx set error status. Redirects
stay in the same trace. ``raise_for_status=True`` raises
:class:`urllib.error.HTTPError`.
"""

from __future__ import annotations

import http.client
import http.server
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from email.message import Message
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from pyrock._bedrock import inject_headers

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock

__all__ = [
    "Response",
    "TracingHandler",
    "build_opener",
    "get",
    "inject_headers",
    "instrument_http_server",
    "post",
    "request",
]

HandlerT = TypeVar("HandlerT", bound=type[http.server.BaseHTTPRequestHandler])


class TracingHandler(urllib.request.BaseHandler):
    """A ``urllib`` handler that opens a CLIENT span per request and injects headers."""

    handler_order = 100

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        raise NotImplementedError

    https_request = http_request

    def http_response(
        self, req: urllib.request.Request, response: http.client.HTTPResponse
    ) -> http.client.HTTPResponse:
        raise NotImplementedError

    https_response = http_response


def build_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """``urllib.request.build_opener`` plus a :class:`TracingHandler`."""
    raise NotImplementedError


class Response:
    """A small wrapper over ``http.client.HTTPResponse`` (or a caught ``HTTPError``)."""

    @property
    def status(self) -> int:
        raise NotImplementedError

    @property
    def reason(self) -> str:
        raise NotImplementedError

    @property
    def url(self) -> str:
        raise NotImplementedError

    @property
    def headers(self) -> Message:
        raise NotImplementedError

    @property
    def ok(self) -> bool:
        """``status < 400``."""
        raise NotImplementedError

    @property
    def text(self) -> str:
        """The body decoded with the response charset (default UTF-8)."""
        raise NotImplementedError

    def read(self, amt: int | None = None, /) -> bytes:
        raise NotImplementedError

    def json(self) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Response:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError


def request(
    method: str,
    url: str,
    /,
    *,
    data: bytes | Iterable[bytes] | None = None,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    raise_for_status: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
) -> Response:
    """Send a request.

    ``HTTPError`` responses are returned as a :class:`Response`; with
    ``raise_for_status=True`` a :class:`urllib.error.HTTPError` is raised.
    """
    raise NotImplementedError


def get(
    url: str,
    /,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    raise_for_status: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
) -> Response:
    raise NotImplementedError


def post(
    url: str,
    /,
    *,
    data: bytes | Iterable[bytes] | None = None,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    raise_for_status: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
) -> Response:
    raise NotImplementedError


def instrument_http_server(
    handler_class: HandlerT,
    /,
    *,
    operation_name: str = "http.request",
    route: Callable[[http.server.BaseHTTPRequestHandler], str | None] | None = None,
    bedrock: Bedrock | None = None,
) -> HandlerT:
    """Return a subclass of a ``BaseHTTPRequestHandler`` that traces each request.

    The instance is captured when the middleware is constructed: ``bedrock`` if given, else
    ``pyrock.current()`` at that point. If that is the noop, ``current()`` is
    resolved per request instead. Each request runs inside a context with that
    instance active, so handler code on server worker threads is instrumented
    (there is no process-wide default instance).

    A ``route`` callable returning ``None`` falls back to the framework route
    key, then to ``"_"``.
    """
    raise NotImplementedError
