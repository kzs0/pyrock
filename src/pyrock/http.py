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

As in Go, a client span is a plain span (no operation metrics). It ends when
the response headers arrive, or with the error when the request fails.

The module also provides :func:`instrument_http_server` for stdlib
``http.server`` services.
"""

from __future__ import annotations

import contextvars
import functools
import http.client
import json as _json
import threading
import urllib.error
import urllib.parse
import urllib.request
import weakref
from collections.abc import Callable, Container, Iterable, Mapping
from email.message import Message
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from pyrock._bedrock import current, current_span_context, inject_headers
from pyrock._context import no_trace_var
from pyrock._httpcore import Instrumentation, RequestInfo
from pyrock.attr import AttrValue
from pyrock.privacy import sanitize_url
from pyrock.trace import Span, SpanKind, StatusCode
from pyrock.trace.w3c import TRACESTATE_HEADER

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

    from pyrock._bedrock import Bedrock
    from pyrock._operation import Operation
    from pyrock.privacy import Policy

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

HandlerT = TypeVar("HandlerT", bound="type[BaseHTTPRequestHandler]")

_RawResponse = http.client.HTTPResponse | urllib.error.HTTPError


# ---------------------------------------------------------------------- client


def _client_attrs(policy: Policy, req: urllib.request.Request) -> dict[str, AttrValue]:
    """Span attributes for ``req``: never credentials, query or fragment."""
    url = sanitize_url(req.full_url)
    parts = urllib.parse.urlsplit(url)
    attrs: dict[str, AttrValue] = {
        "http.method": req.get_method(),
        "http.host": parts.netloc,
        "http.scheme": parts.scheme,
    }
    target = parts.path or "/"
    # Raw URLs and paths are kept only when the policy allowlists them.
    raw = (("http.url", url), ("http.target", target))
    attrs.update({key: value for key, value in raw if policy.value(key, value)[1]})
    return attrs


def _inject(req: urllib.request.Request, span: Span | None) -> None:
    """Set ``traceparent``/``tracestate`` on ``req`` (not copied onto redirects)."""
    carrier: dict[str, str] = inject_headers({}, span.context if span is not None else None)
    if carrier and TRACESTATE_HEADER not in carrier:
        req.remove_header(TRACESTATE_HEADER.capitalize())  # stale for the new traceparent
    for name, value in carrier.items():
        req.add_unredirected_header(name, value)


class TracingHandler(urllib.request.BaseHandler):
    """A ``urllib`` handler that opens a CLIENT span per request and injects headers.

    The span ends in :meth:`http_response` (4xx/5xx set error status). urllib
    has no hook for requests that fail before a response, so connection errors
    and timeouts are recorded only by openers from :func:`build_opener` (and
    therefore :func:`request`); on a plain ``urllib`` opener such a span is
    never exported. Safe to share between threads.
    """

    handler_order = 100

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: weakref.WeakKeyDictionary[urllib.request.Request, Span] = (
            weakref.WeakKeyDictionary()
        )

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        """Start the span and inject its context (only the context under ``no_trace``)."""
        bedrock = current()
        if bedrock.is_noop:
            return req
        if no_trace_var.get():
            _inject(req, None)
            return req
        span = bedrock.tracer.start_span(
            f"HTTP {req.get_method()}",
            parent=current_span_context(),
            kind=SpanKind.CLIENT,
            attrs=_client_attrs(bedrock.policy, req),
        )
        _inject(req, span)
        with self._lock:
            self._spans[req] = span
        return req

    https_request = http_request

    def http_response(
        self, req: urllib.request.Request, response: http.client.HTTPResponse
    ) -> http.client.HTTPResponse:
        """End the span with the response status."""
        span = self._pop(req)
        if span is not None:
            status = response.status
            span.set_attrs({"http.status_code": status})
            if status >= 400:
                span.set_status(StatusCode.ERROR, f"HTTP {status}")
            else:
                span.set_status(StatusCode.OK)
            span.end()
        return response

    https_response = http_response

    def request_failed(self, req: urllib.request.Request, exc: BaseException) -> None:
        """End the span of ``req`` (if still open) with ``exc`` as its error."""
        span = self._pop(req)
        if span is None:
            return
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, type(exc).__name__)
        span.end()

    def _pop(self, req: urllib.request.Request) -> Span | None:
        with self._lock:
            return self._spans.pop(req, None)


def _handlers(opener: urllib.request.OpenerDirector) -> list[urllib.request.BaseHandler]:
    """The handlers of ``opener`` (an attribute typeshed does not declare)."""
    handlers: list[urllib.request.BaseHandler] = opener.handlers  # type: ignore[attr-defined]
    return handlers


class _TracingOpener(urllib.request.OpenerDirector):
    """An opener that reports failed requests to its :class:`TracingHandler` instances."""

    def __init__(self) -> None:
        super().__init__()
        self.tracing_handlers: list[TracingHandler] = []

    def add_handler(self, handler: urllib.request.BaseHandler) -> None:
        super().add_handler(handler)
        if isinstance(handler, TracingHandler):
            self.tracing_handlers.append(handler)

    def open(self, fullurl: str | urllib.request.Request, *args: Any, **kwargs: Any) -> Any:
        """``OpenerDirector.open``; a failure ends the request's span before propagating."""
        req = urllib.request.Request(fullurl) if isinstance(fullurl, str) else fullurl
        try:
            return super().open(req, *args, **kwargs)
        except BaseException as exc:
            for handler in self.tracing_handlers:
                handler.request_failed(req, exc)
            raise


def build_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """``urllib.request.build_opener`` plus a :class:`TracingHandler`.

    The default handlers and ``handlers`` are kept, as in urllib. The returned
    opener also records requests that fail before a response.
    """
    traced = _TracingOpener()
    for handler in _handlers(urllib.request.build_opener(*handlers)):
        traced.add_handler(handler)
    if not traced.tracing_handlers:
        traced.add_handler(TracingHandler())
    return traced


@functools.cache
def _default_opener() -> urllib.request.OpenerDirector:
    """The shared opener of :func:`request` (built once, like ``urlopen``'s)."""
    return build_opener()


class Response:
    """A small wrapper over ``http.client.HTTPResponse`` (or a caught ``HTTPError``).

    :attr:`text` and :meth:`json` read the rest of the body once and cache it.
    """

    __slots__ = ("_body", "_raw", "_status")

    def __init__(self, raw: _RawResponse, /) -> None:
        self._raw = raw
        self._status = raw.code if isinstance(raw, urllib.error.HTTPError) else raw.status
        self._body: bytes | None = None

    @property
    def status(self) -> int:
        return self._status

    @property
    def reason(self) -> str:
        return self._raw.reason

    @property
    def url(self) -> str:
        """The final URL (after redirects)."""
        return self._raw.url

    @property
    def headers(self) -> Message:
        return self._raw.headers

    @property
    def ok(self) -> bool:
        """``status < 400``."""
        return self.status < 400

    @property
    def text(self) -> str:
        """The body decoded with the response charset (default UTF-8)."""
        charset = self.headers.get_content_charset() or "utf-8"
        try:
            return self._content().decode(charset, errors="replace")
        except LookupError:
            return self._content().decode("utf-8", errors="replace")

    def read(self, amt: int | None = None, /) -> bytes:
        """Read up to ``amt`` bytes of the body (all of the rest by default)."""
        return self._raw.read() if amt is None else self._raw.read(amt)

    def json(self) -> Any:
        """The body parsed as JSON."""
        return _json.loads(self._content())

    def close(self) -> None:
        self._raw.close()

    def _content(self) -> bytes:
        if self._body is None:
            self._body = self._raw.read()
        return self._body

    def __enter__(self) -> Response:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<pyrock.http.Response [{self.status}]>"


def _body(
    data: bytes | Iterable[bytes] | None, json: Any, headers: dict[str, str]
) -> bytes | Iterable[bytes] | None:
    """The request body; ``json=`` also defaults ``Content-Type`` to JSON."""
    if json is None:
        return data
    if data is not None:
        raise TypeError("pass either data= or json=, not both")
    if not any(name.lower() == "content-type" for name in headers):
        headers["Content-Type"] = "application/json"
    return _json.dumps(json).encode()


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
    ``opener`` should come from :func:`build_opener` to be traced; ``headers``
    is copied, never mutated.
    """
    all_headers = dict(headers or {})
    body = _body(data, json, all_headers)
    req = urllib.request.Request(url, data=body, headers=all_headers, method=method.upper())
    director = _default_opener() if opener is None else opener
    try:
        raw = director.open(req) if timeout is None else director.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if raise_for_status:
            raise
        return Response(exc)
    return Response(raw)


def get(
    url: str,
    /,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    raise_for_status: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
) -> Response:
    """Send a ``GET`` request (see :func:`request`)."""
    return request(
        "GET",
        url,
        headers=headers,
        timeout=timeout,
        raise_for_status=raise_for_status,
        opener=opener,
    )


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
    """Send a ``POST`` request (see :func:`request`)."""
    return request(
        "POST",
        url,
        data=data,
        json=json,
        headers=headers,
        timeout=timeout,
        raise_for_status=raise_for_status,
        opener=opener,
    )


# ---------------------------------------------------------------------- server


@functools.cache
def _traced_mixin() -> type[BaseHTTPRequestHandler]:
    """The mixin :func:`instrument_http_server` places before the user's handler class.

    Built on first use so that importing this module does not import ``http.server``.
    """
    import http.server

    class _TracedRequestHandler(http.server.BaseHTTPRequestHandler):
        _pyrock_instrumentation: ClassVar[Instrumentation[BaseHTTPRequestHandler]]
        _pyrock_operation_name: ClassVar[str]
        _pyrock_op: Operation | None = None
        _pyrock_status: int | None = None

        def handle_one_request(self) -> None:
            """Handle one request in its own context (keep-alive connections loop over this)."""
            contextvars.copy_context().run(self._pyrock_handle_one_request)

        def _pyrock_handle_one_request(self) -> None:
            self._pyrock_op = None
            self._pyrock_status = None
            try:
                super().handle_one_request()
            except BaseException as exc:
                self._pyrock_finish(exc)
                raise
            self._pyrock_finish(None)

        def parse_request(self) -> bool:
            """Parse as usual, then start and activate the request's operation."""
            if not super().parse_request():
                return False
            bedrock = self._pyrock_instrumentation.instance()
            if not bedrock.is_noop:
                op = self._pyrock_instrumentation.start(
                    bedrock, self._pyrock_operation_name, self, self._pyrock_info()
                )
                # The context is private to this request, so the activation is never undone.
                op.activate()
                self._pyrock_op = op
            return True

        def send_response_only(self, code: int, message: str | None = None) -> None:
            super().send_response_only(code, message)
            if code >= 200:
                self._pyrock_status = code

        def _pyrock_info(self) -> RequestInfo:
            host = self.headers.get("Host") or "{}:{}".format(*self.connection.getsockname()[:2])
            return RequestInfo(
                method=self.command,
                scheme="https" if hasattr(self.connection, "getpeercert") else "http",
                host=host,
                user_agent=self.headers.get("User-Agent"),
                headers=self.headers.items(),
            )

        def _pyrock_finish(self, error: BaseException | None) -> None:
            op = self._pyrock_op
            if op is None:
                return
            self._pyrock_op = None
            self._pyrock_instrumentation.finish(
                op, self, framework_route=None, status=self._pyrock_status, error=error
            )

    return _TracedRequestHandler


def instrument_http_server(
    handler_class: HandlerT,
    /,
    *,
    operation_name: str = "http.request",
    additional_labels: Iterable[str] = (),
    additional_attrs: Callable[[BaseHTTPRequestHandler], Mapping[str, AttrValue]] | None = None,
    success_codes: Container[int] | None = None,
    trace_propagation: bool = True,
    route: Callable[[BaseHTTPRequestHandler], str | None] | None = None,
    bedrock: Bedrock | None = None,
) -> HandlerT:
    """Return a subclass of a ``BaseHTTPRequestHandler`` that traces each request.

    The options mean what they mean on :class:`pyrock.wsgi.Middleware`:
    ``additional_labels`` extends the metric labels, ``additional_attrs``
    receives the handler (after the request line and headers are parsed) and
    returns extra attributes, ``success_codes=None`` treats every status below
    400 as success, and ``trace_propagation=False`` ignores incoming
    ``traceparent``/``tracestate`` headers.

    The instance is captured when this function is called: ``bedrock`` if given, else
    ``pyrock.current()`` at that point. If that is the noop, ``current()`` is
    resolved per request instead. Each request runs inside a context with that
    instance active, so handler code on server worker threads is instrumented
    (there is no process-wide default instance).

    ``route`` receives the handler (after the request line and headers are
    parsed); without it, or when it returns ``None``, ``http.route`` is ``"_"``.
    ``route`` and ``additional_attrs`` callbacks that raise are logged once on
    the ``pyrock`` logger and fall back. The final status is the last non-1xx
    status sent; an exception from the handler marks failure (``500`` if no
    status was sent) and is re-raised.
    """
    instrumentation: Instrumentation[BaseHTTPRequestHandler] = Instrumentation(
        additional_labels=additional_labels,
        additional_attrs=additional_attrs,
        success_codes=success_codes,
        trace_propagation=trace_propagation,
        route=route,
        bedrock=bedrock,
    )
    namespace = {
        "__module__": handler_class.__module__,
        "__qualname__": handler_class.__qualname__,
        "__doc__": handler_class.__doc__,
        "_pyrock_instrumentation": instrumentation,
        "_pyrock_operation_name": operation_name,
    }
    traced = type(handler_class.__name__, (_traced_mixin(), handler_class), namespace)
    return cast("HandlerT", traced)
