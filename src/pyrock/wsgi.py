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

The request runs in its own copy of the server thread's context with the
instance and the operation active; the response iterable is advanced and
closed inside that same context, so streaming generators see the operation
too. The middleware wraps the response iterable, so a server's
``wsgi.file_wrapper`` fast path is not used for instrumented responses.

This module also holds the request-instrumentation core shared with
:mod:`pyrock.asgi` and :func:`pyrock.http.instrument_http_server`.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Container, Iterable, Iterator, Mapping, Sized
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from pyrock._bedrock import current
from pyrock._types import ExcInfo, StartResponse, WSGIApp, WSGIEnviron
from pyrock.attr import AttrValue
from pyrock.trace import SpanKind
from pyrock.trace.http import HeaderCarrier, HTTPPropagator, WSGIEnvironHeaders

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock
    from pyrock._operation import Operation

__all__ = ["ROUTE_KEY", "Middleware"]

ROUTE_KEY = "pyrock.route"
"""``environ`` key a framework hook may set to the matched route template."""

UNMATCHED_ROUTE = "_"
"""``http.route`` when no template is known (shared by every unmatched path)."""

INTERNAL_ERROR = 500
"""``http.status_code`` recorded when the app fails before sending a response."""

DEFAULT_LABELS = ("http.method", "http.route", "http.status_code")

_PROPAGATOR = HTTPPropagator()

RequestT = TypeVar("RequestT")


@dataclass(frozen=True, slots=True)
class RequestInfo:
    """What a server integration extracted from one incoming request."""

    method: str | None
    """``None`` for requests without one (websocket connections)."""
    scheme: str
    host: str
    user_agent: str | None
    headers: HeaderCarrier


class Instrumentation(Generic[RequestT]):
    """Request instrumentation shared by the WSGI, ASGI and ``http.server`` integrations.

    ``RequestT`` is what user callbacks receive: the WSGI ``environ``, the
    ASGI ``scope`` or the ``BaseHTTPRequestHandler``. Immutable after
    construction, so one instance serves concurrent requests.
    """

    __slots__ = (
        "_additional_attrs",
        "_bedrock",
        "_labels",
        "_route",
        "_success_codes",
        "_trace_propagation",
    )

    def __init__(
        self,
        *,
        additional_labels: Iterable[str],
        additional_attrs: Callable[[RequestT], Mapping[str, AttrValue]] | None,
        success_codes: Container[int] | None,
        trace_propagation: bool,
        route: Callable[[RequestT], str | None] | None,
        bedrock: Bedrock | None,
    ) -> None:
        if bedrock is None:
            active = current()
            bedrock = None if active.is_noop else active
        self._bedrock = bedrock
        self._labels = tuple(dict.fromkeys((*DEFAULT_LABELS, *additional_labels)))
        self._additional_attrs = additional_attrs
        self._success_codes = success_codes
        self._trace_propagation = trace_propagation
        self._route = route

    @property
    def labels(self) -> tuple[str, ...]:
        """Metric labels: the defaults plus ``additional_labels``."""
        return self._labels

    def instance(self) -> Bedrock:
        """The instance captured at construction, else the active one (maybe the noop)."""
        return current() if self._bedrock is None else self._bedrock

    def start(
        self,
        bedrock: Bedrock,
        name: str,
        request: RequestT,
        info: RequestInfo,
        *,
        labels: tuple[str, ...] | None = None,
    ) -> Operation:
        """Start (without activating) the ``SERVER`` operation for one request."""
        attrs: dict[str, AttrValue] = {"http.scheme": info.scheme, "http.host": info.host}
        if info.method is not None:
            attrs["http.method"] = info.method
        agent = info.user_agent
        if agent and bedrock.policy.value("http.user_agent", agent)[1]:
            attrs["http.user_agent"] = agent
        if self._additional_attrs is not None:
            attrs.update(self._additional_attrs(request))
        remote = _PROPAGATOR.extract(info.headers) if self._trace_propagation else None
        return bedrock.start_operation(
            name,
            attrs=attrs,
            metric_labels=self._labels if labels is None else labels,
            remote_parent=remote,
            kind=SpanKind.SERVER,
        )

    def finish(
        self,
        op: Operation,
        request: RequestT,
        *,
        framework_route: str | None,
        status: int | None,
        error: BaseException | None,
    ) -> None:
        """Record the route and outcome, then end ``op``.

        ``status=None`` means the response never started, recorded as ``500``
        whether or not an exception escaped.
        """
        recorded = INTERNAL_ERROR if status is None else status
        op.register(
            {
                "http.route": self.route_of(request, framework_route),
                "http.status_code": recorded,
            }
        )
        if error is None and not self.is_success(recorded):
            op.fail()
        op.end(error=error)

    def finish_connection(
        self,
        op: Operation,
        request: RequestT,
        *,
        framework_route: str | None,
        error: BaseException | None,
    ) -> None:
        """End a status-less operation (a websocket connection): failure only on ``error``."""
        op.register({"http.route": self.route_of(request, framework_route)})
        op.end(error=error)

    def route_of(self, request: RequestT, framework_route: str | None) -> str:
        """The ``route=`` callable's template, else the framework's, else ``"_"``."""
        if self._route is not None:
            template = self._route(request)
            if template is not None:
                return template
        return UNMATCHED_ROUTE if framework_route is None else framework_route

    def is_success(self, status: int) -> bool:
        """Whether ``status`` counts as success (``success_codes``, else below 400)."""
        if self._success_codes is None:
            return status < 400
        return status in self._success_codes


def parse_status(status: str) -> int | None:
    """The numeric code of a WSGI status line such as ``"404 Not Found"``."""
    code = status.partition(" ")[0]
    return int(code) if code.isdigit() else None


class Middleware:
    """Wraps a WSGI app with an operation per request."""

    __slots__ = ("_app", "_instrumentation", "_operation_name")

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
        self._app = app
        self._operation_name = operation_name
        self._instrumentation: Instrumentation[WSGIEnviron] = Instrumentation(
            additional_labels=additional_labels,
            additional_attrs=additional_attrs,
            success_codes=success_codes,
            trace_propagation=trace_propagation,
            route=route,
            bedrock=bedrock,
        )

    def __call__(self, environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        bedrock = self._instrumentation.instance()
        if bedrock.is_noop:
            return self._app(environ, start_response)
        exchange = _Exchange(self._instrumentation, environ, start_response)
        return exchange.context.run(exchange.run, self._app, bedrock, self._operation_name)


def _environ_info(environ: WSGIEnviron) -> RequestInfo:
    host = environ.get("HTTP_HOST")
    if not host:
        host = f"{environ.get('SERVER_NAME', '')}:{environ.get('SERVER_PORT', '')}".strip(":")
    return RequestInfo(
        method=str(environ.get("REQUEST_METHOD", "GET")),
        scheme=str(environ.get("wsgi.url_scheme", "http")),
        host=str(host),
        user_agent=environ.get("HTTP_USER_AGENT"),
        headers=WSGIEnvironHeaders(environ),
    )


class _Exchange:
    """One request: its private context, operation and response state."""

    __slots__ = (
        "_environ",
        "_headers_sent",
        "_instrumentation",
        "_op",
        "_start_response",
        "_status",
        "context",
    )

    def __init__(
        self,
        instrumentation: Instrumentation[WSGIEnviron],
        environ: WSGIEnviron,
        start_response: StartResponse,
    ) -> None:
        self.context = contextvars.copy_context()
        self._instrumentation = instrumentation
        self._environ = environ
        self._start_response = start_response
        self._status: int | None = None
        self._headers_sent = False
        self._op: Operation | None = None

    def run(self, app: WSGIApp, bedrock: Bedrock, name: str) -> Iterable[bytes]:
        """Call ``app`` (inside :attr:`context`) and wrap its response iterable."""
        op = self._instrumentation.start(bedrock, name, self._environ, _environ_info(self._environ))
        self._op = op
        # The context is private to this request, so the activation is never undone.
        op.activate()
        try:
            result = app(self._environ, self._wrapped_start_response)
        except BaseException as exc:
            self.finish(exc)
            raise
        return _ResponseBody(self, result)

    def _wrapped_start_response(
        self, status: str, headers: list[tuple[str, str]], exc_info: ExcInfo | None = None
    ) -> Callable[[bytes], object]:
        if exc_info is None:
            write = self._start_response(status, headers)
        else:
            write = self._start_response(status, headers, exc_info)
        code = parse_status(status)
        if code is not None:
            self._status = code

        def tracked_write(data: bytes) -> object:
            if data:
                self._headers_sent = True
            return write(data)

        return tracked_write

    def mark_body_chunk(self, chunk: bytes) -> None:
        """Note that the server is about to send ``chunk`` (headers go out with the first)."""
        if chunk:
            self._headers_sent = True

    def finish(self, error: BaseException | None) -> None:
        """End the operation (status ``500`` when ``error`` came before any body was sent)."""
        op = self._op
        if op is None:
            return
        # Until the first body bytes go out, the server can still answer 500 itself.
        status = None if error is not None and not self._headers_sent else self._status
        route = self._environ.get(ROUTE_KEY)
        self._instrumentation.finish(
            op,
            self._environ,
            framework_route=route if isinstance(route, str) else None,
            status=status,
            error=error,
        )


class _ResponseBody:
    """The app's response iterable, advanced and closed inside the request's context.

    ``close()`` always closes the wrapped iterable and then ends the operation,
    with the iteration error (if any) as its failure.
    """

    __slots__ = ("_error", "_exchange", "_iterable", "_iterator")

    def __init__(self, exchange: _Exchange, iterable: Iterable[bytes]) -> None:
        self._exchange = exchange
        self._iterable = iterable
        self._iterator: Iterator[bytes] | None = None
        self._error: BaseException | None = None

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        return self._exchange.context.run(self._next)

    def __len__(self) -> int:
        """The wrapped iterable's length (servers use it to set ``Content-Length``)."""
        iterable = self._iterable
        if not isinstance(iterable, Sized):
            raise TypeError("the response iterable has no length")
        return len(iterable)

    def close(self) -> None:
        self._exchange.context.run(self._close)

    def _next(self) -> bytes:
        try:
            if self._iterator is None:
                self._iterator = iter(self._iterable)
            chunk = next(self._iterator)
        except BaseException as exc:
            if not isinstance(exc, StopIteration):
                self._error = exc
            raise
        self._exchange.mark_body_chunk(chunk)
        return chunk

    def _close(self) -> None:
        error = self._error
        try:
            close = getattr(self._iterable, "close", None)
            if close is not None:
                close()
        except BaseException as exc:
            error = error or exc
            raise
        finally:
            self._exchange.finish(error)
