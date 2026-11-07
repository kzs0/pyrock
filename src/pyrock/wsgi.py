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

``route=`` and ``additional_attrs=`` callbacks that raise are logged once
on the ``pyrock`` logger and fall back (to ``environ[ROUTE_KEY]`` / no extra
attributes); the request itself is never failed by them.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Container, Iterable, Iterator, Mapping, Sized
from typing import TYPE_CHECKING

from pyrock._httpcore import ROUTE_KEY, Instrumentation, RequestInfo, parse_status
from pyrock._types import ExcInfo, StartResponse, WSGIApp, WSGIEnviron
from pyrock.trace.http import WSGIEnvironHeaders

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock
    from pyrock._operation import Operation
    from pyrock.attr import AttrValue

__all__ = ["ROUTE_KEY", "Middleware"]


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
