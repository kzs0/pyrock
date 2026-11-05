"""Private helpers for the HTTP end-to-end tests (WSGI, ASGI, client, server).

Everything here uses only pyrock's public API plus ``tests.support``. It holds:

* W3C constants for incoming trace context;
* real local servers (``wsgiref`` and ``http.server``) bound to ``127.0.0.1:0``
  whose serving thread runs in a *copy of the test's context*, so the active
  ``Bedrock`` (from ``pyrock.testing.capture()``) is visible to handlers;
* raw-socket / ``http.client`` helpers for the observability server;
* :class:`MiddlewareContract`, the shared WSGI/ASGI middleware expectations.
  ``test_wsgi.py`` and ``test_asgi.py`` subclass it with their flavor, so every
  shared behaviour runs once per middleware (the base class is not collected:
  it lives in a non-``test_`` module and its name has no ``Test`` prefix).
"""

from __future__ import annotations

import contextlib
import contextvars
import http.client
import http.server
import socket
import threading
import wsgiref.simple_server
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

import pyrock
from pyrock import privacy
from pyrock.testing import capture
from pyrock.trace import SpanKind
from tests.support.asgi_driver import run_asgi
from tests.support.prom import parse
from tests.support.wsgi_driver import call_wsgi

# ---------------------------------------------------------------- W3C inputs

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"

INVALID_TRACEPARENTS = [
    pytest.param("00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01", id="uppercase"),
    pytest.param("00-00000000000000000000000000000000-b7ad6b7169203331-01", id="zero-trace"),
    pytest.param("00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01", id="zero-span"),
    pytest.param("ff-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", id="version-ff"),
    pytest.param("not-a-traceparent", id="garbage"),
]

HOST = "api.example.test"
USER_AGENT = "pyrock-e2e/1.0"
DEFAULT_LABELS = {"http_method", "http_route", "http_status_code"}


def default_headers(extra: Iterable[tuple[str, str]] = ()) -> list[tuple[str, str]]:
    return [("Host", HOST), ("User-Agent", USER_AGENT), *extra]


def attr_strings(span: Any) -> list[str]:
    """Every attribute value of a captured span rendered as text (for leak checks)."""
    return [str(v) for v in span.attrs.values()]


# ---------------------------------------------------------------- real servers


def _run_in_context(target: Callable[..., object], **kwargs: Any) -> threading.Thread:
    """Start a daemon thread running ``target`` in a copy of the caller's context."""
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=ctx.run, args=(target,), kwargs=kwargs, daemon=True)
    thread.start()
    return thread


class _QuietWSGIHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextlib.contextmanager
def serve_wsgi(app: Callable[..., Iterable[bytes]]) -> Iterator[str]:
    """Serve ``app`` with ``wsgiref`` on ``127.0.0.1:0``; yields the base URL.

    The server is single-threaded and serves in a copy of the caller's context.
    Leaving the block waits for the request in progress (and the response
    iterable's ``close()``) before returning, so spans are finished.
    """
    httpd = wsgiref.simple_server.make_server("127.0.0.1", 0, app, handler_class=_QuietWSGIHandler)
    thread = _run_in_context(httpd.serve_forever, poll_interval=0.05)
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


@contextlib.contextmanager
def serve_handler(handler_class: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    """Serve a ``BaseHTTPRequestHandler`` subclass on ``127.0.0.1:0`` (single-threaded)."""
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
    thread = _run_in_context(httpd.serve_forever, poll_interval=0.05)
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def free_port() -> int:
    """A port that nothing listens on (bound then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------- raw HTTP


@dataclass
class RawResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def http_get(
    address: tuple[str, int],
    path: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> RawResponse:
    host, port = address
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, headers=dict(headers or {}))
        resp = conn.getresponse()
        body = resp.read()
        return RawResponse(resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
    finally:
        conn.close()


def read_until_closed(sock: socket.socket, *, timeout: float = 5.0) -> bytes:
    """Read until the peer closes (or resets) the connection; ``AssertionError`` on timeout."""
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return data
            data += chunk
    except (ConnectionResetError, BrokenPipeError):
        return data
    except TimeoutError:
        raise AssertionError(f"server kept the connection open; got {data!r}") from None


def status_of(raw: bytes) -> int | None:
    """The status code of a raw HTTP response, ``None`` when there is no status line."""
    if not raw.startswith(b"HTTP/"):
        return None
    return int(raw.split(b" ", 2)[1])


# ---------------------------------------------------------------- middleware flavors


@dataclass
class Reply:
    """What the inner (wrapped) app does for one request."""

    status: int = 200
    body: bytes = b"ok"
    headers: tuple[tuple[str, str], ...] = (("Content-Type", "text/plain"),)
    route: str | None = None
    """Set the framework route key (``pyrock.route``) to this template while handling."""
    on_call: Callable[[Any], None] | None = None
    """Called inside the app with the environ / scope (e.g. to read the current operation)."""
    raise_exc: BaseException | None = None


@dataclass
class Result:
    status: int
    body: bytes
    headers: dict[str, str]


class WSGIFlavor:
    name = "wsgi"

    def middleware(self, app: Any, **kwargs: Any) -> Any:
        from pyrock.wsgi import Middleware

        return Middleware(app, **kwargs)

    def app(self, reply: Reply) -> Any:
        from pyrock.wsgi import ROUTE_KEY

        def app(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
            if reply.route is not None:
                environ[ROUTE_KEY] = reply.route
            if reply.on_call is not None:
                reply.on_call(environ)
            if reply.raise_exc is not None:
                raise reply.raise_exc
            start_response(f"{reply.status} STATUS", list(reply.headers))
            return [reply.body]

        return app

    def call(
        self,
        app: Any,
        method: str = "GET",
        path: str = "/",
        headers: Iterable[tuple[str, str]] = (),
    ) -> Result:
        resp = call_wsgi(app, method, path, headers=list(headers))
        return Result(resp.status_code, resp.body, {k.lower(): v for k, v in resp.headers})

    def request_header(self, request: Any, name: str) -> str:
        """Read a request header from the environ handed to ``additional_attrs``."""
        return str(request.get("HTTP_" + name.upper().replace("-", "_"), ""))


class ASGIFlavor:
    name = "asgi"

    def middleware(self, app: Any, **kwargs: Any) -> Any:
        from pyrock.asgi import Middleware

        return Middleware(app, **kwargs)

    def app(self, reply: Reply) -> Any:
        from pyrock.asgi import ROUTE_KEY

        async def app(scope: Any, receive: Any, send: Any) -> None:
            assert scope["type"] == "http"
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    break
            if reply.route is not None:
                scope[ROUTE_KEY] = reply.route
            if reply.on_call is not None:
                reply.on_call(scope)
            if reply.raise_exc is not None:
                raise reply.raise_exc
            await send(
                {
                    "type": "http.response.start",
                    "status": reply.status,
                    "headers": [(k.lower().encode(), v.encode()) for k, v in reply.headers],
                }
            )
            await send({"type": "http.response.body", "body": reply.body})

        return app

    def call(
        self,
        app: Any,
        method: str = "GET",
        path: str = "/",
        headers: Iterable[tuple[str, str]] = (),
    ) -> Result:
        resp = run_asgi(app, method, path, headers=list(headers))
        return Result(
            resp.status,
            resp.body,
            {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in resp.headers},
        )

    def request_header(self, request: Any, name: str) -> str:
        """Read a request header from the scope handed to ``additional_attrs``."""
        wanted = name.lower().encode("latin-1")
        for key, value in request.get("headers", []):
            if bytes(key).lower() == wanted:
                return bytes(value).decode("latin-1")
        return ""


# ---------------------------------------------------------------- shared contract


class MiddlewareContract:
    """Expectations shared by ``pyrock.wsgi.Middleware`` and ``pyrock.asgi.Middleware``.

    Subclasses set ``flavor``. Spans are read after the ``capture()`` block
    (which flushes); metrics text is read inside it.
    """

    flavor: WSGIFlavor | ASGIFlavor

    @pytest.fixture(autouse=True)
    def _clean(self, clean_env: pytest.MonkeyPatch) -> None:
        return None

    def _serve(
        self,
        reply: Reply | None = None,
        *,
        method: str = "GET",
        path: str = "/",
        headers: Iterable[tuple[str, str]] = (),
        **mw: Any,
    ) -> Result:
        app = self.flavor.middleware(self.flavor.app(reply or Reply()), **mw)
        return self.flavor.call(app, method, path, default_headers(headers))

    # -- spans and attributes ------------------------------------------

    def test_opens_one_server_span_named_http_request(self) -> None:
        with capture() as t:
            self._serve()
        span = t.spans.one(name="http.request")
        assert span.kind is SpanKind.SERVER

    def test_operation_name_option_names_the_span(self) -> None:
        with capture() as t:
            self._serve(operation_name="api.call")
        assert t.spans.names() == ["api.call"]

    def test_default_attributes(self) -> None:
        with capture() as t:
            self._serve(method="POST", path="/things")
        attrs = t.spans.one(name="http.request").attrs
        assert attrs["http.method"] == "POST"
        assert attrs["http.route"] == "_"
        assert attrs["http.scheme"] == "http"
        assert attrs["http.host"] == HOST
        assert attrs["http.status_code"] == 200

    def test_user_agent_absent_by_default(self) -> None:
        with capture() as t:
            self._serve()
        attrs = t.spans.one(name="http.request").attrs
        assert "http.user_agent" not in attrs
        assert USER_AGENT not in attr_strings(t.spans.one(name="http.request"))

    def test_user_agent_present_when_policy_allows_it(self) -> None:
        # Assumed opt-in: an attribute allowlist naming the http.* group.
        policy = privacy.Policy(attribute_keys=["http.*"])
        with capture(data_policy=policy) as t:
            self._serve()
        assert t.spans.one(name="http.request").attrs["http.user_agent"] == USER_AGENT

    def test_handler_sees_current_operation(self) -> None:
        seen: list[Any] = []
        with capture() as t:
            self._serve(Reply(on_call=lambda _req: seen.append(pyrock.current_operation())))
        span = t.spans.one(name="http.request")
        assert len(seen) == 1
        op = seen[0]
        assert op is not None
        assert op.name == "http.request"
        assert op.trace_id == span.trace_id
        assert op.span_id == span.span_id

    def test_response_passes_through_unchanged(self) -> None:
        reply = Reply(status=201, body=b"created", headers=(("X-App", "1"),))
        with capture():
            result = self._serve(reply)
        assert result.status == 201
        assert result.body == b"created"
        assert result.headers["x-app"] == "1"

    def test_without_active_bedrock_the_app_still_works(self) -> None:
        result = self._serve(Reply(body=b"plain"))
        assert result.status == 200
        assert result.body == b"plain"

    # -- metrics -------------------------------------------------------

    def test_default_metric_labels(self) -> None:
        with capture() as t:
            self._serve()
            prom = parse(t.metrics.text())
        (labels,) = prom.label_sets("http_request_count")
        assert labels.keys() >= DEFAULT_LABELS
        assert labels["http_method"] == "GET"
        assert labels["http_route"] == "_"
        assert labels["http_status_code"] == "200"

    def test_success_records_count_and_success(self) -> None:
        with capture() as t:
            self._serve()
            prom = parse(t.metrics.text())
        assert prom.value("http_request_count") == 1
        assert prom.value("http_request_successes") == 1
        assert prom.value("http_request_failures") in (None, 0)
        assert prom.value("http_request_duration_ms_count") == 1

    def test_additional_labels_and_attrs(self) -> None:
        flavor = self.flavor
        with capture() as t:
            self._serve(
                headers=[("X-User-Type", "admin")],
                additional_labels=["user_type"],
                additional_attrs=lambda req: {
                    "user_type": flavor.request_header(req, "X-User-Type")
                },
            )
            prom = parse(t.metrics.text())
        assert t.spans.one(name="http.request").attrs["user_type"] == "admin"
        (labels,) = prom.label_sets("http_request_count")
        assert labels["user_type"] == "admin"
        assert labels.keys() >= DEFAULT_LABELS

    def test_additional_attrs_are_not_labels_unless_declared(self) -> None:
        with capture() as t:
            self._serve(additional_attrs=lambda _req: {"request.id": "req-123"})
            prom = parse(t.metrics.text())
        assert t.spans.one(name="http.request").attrs["request.id"] == "req-123"
        (labels,) = prom.label_sets("http_request_count")
        assert "req-123" not in labels.values()
        assert "request_id" not in labels

    def test_declared_label_without_value_is_underscore(self) -> None:
        with capture() as t:
            self._serve(additional_labels=["tenant"])
            prom = parse(t.metrics.text())
        (labels,) = prom.label_sets("http_request_count")
        assert labels["tenant"] == "_"

    # -- status and failures -------------------------------------------

    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    def test_error_status_marks_failure_without_raising(self, status: int) -> None:
        with capture() as t:
            result = self._serve(Reply(status=status))
            prom = parse(t.metrics.text())
        assert result.status == status
        span = t.spans.one(name="http.request")
        assert span.failed
        assert span.attrs["http.status_code"] == status
        assert prom.value("http_request_failures", http_status_code=str(status)) == 1

    @pytest.mark.parametrize("status", [200, 201, 204, 301, 302, 399])
    def test_status_below_400_is_success_by_default(self, status: int) -> None:
        with capture() as t:
            self._serve(Reply(status=status, body=b""))
        assert not t.spans.one(name="http.request").failed

    def test_success_codes_can_accept_404(self) -> None:
        with capture() as t:
            self._serve(Reply(status=404), success_codes={200, 404})
            prom = parse(t.metrics.text())
        assert not t.spans.one(name="http.request").failed
        assert prom.value("http_request_successes", http_status_code="404") == 1

    def test_success_codes_exclude_unlisted_2xx(self) -> None:
        with capture() as t:
            self._serve(Reply(status=201), success_codes=range(200, 201))
        assert t.spans.one(name="http.request").failed

    def test_exception_marks_failure_with_500_and_reraises(self) -> None:
        boom = RuntimeError("boom")
        with capture() as t:
            with pytest.raises(RuntimeError, match="boom") as info:
                self._serve(Reply(raise_exc=boom))
            prom = parse(t.metrics.text())
        assert info.value is boom
        span = t.spans.one(name="http.request")
        assert span.failed
        assert span.attrs["http.status_code"] == 500
        assert prom.value("http_request_failures", http_status_code="500") == 1

    # -- trace context -------------------------------------------------

    def test_without_traceparent_starts_a_root_span(self) -> None:
        with capture() as t:
            self._serve()
        span = t.spans.one(name="http.request")
        assert span.parent_span_id is None
        assert span.trace_id != TRACE_ID

    def test_valid_traceparent_continues_the_trace(self) -> None:
        with capture() as t:
            self._serve(headers=[("traceparent", TRACEPARENT)])
        span = t.spans.one(name="http.request")
        assert span.trace_id == TRACE_ID
        assert span.parent_span_id == PARENT_SPAN_ID

    def test_tracestate_is_passed_through_to_outgoing_context(self) -> None:
        seen: list[dict[str, str]] = []
        with capture() as t:
            self._serve(
                Reply(on_call=lambda _req: seen.append(pyrock.inject_headers({}))),
                headers=[("traceparent", TRACEPARENT), ("tracestate", "vendor=abc,other=xyz")],
            )
        span = t.spans.one(name="http.request")
        (out,) = seen
        assert out["traceparent"] == f"00-{TRACE_ID}-{span.span_id}-01"
        assert out["tracestate"] == "vendor=abc,other=xyz"

    def test_repeated_tracestate_headers_are_joined(self) -> None:
        seen: list[dict[str, str]] = []
        with capture():
            self._serve(
                Reply(on_call=lambda _req: seen.append(pyrock.inject_headers({}))),
                headers=[
                    ("traceparent", TRACEPARENT),
                    ("tracestate", "first=1"),
                    ("tracestate", "second=2"),
                ],
            )
        (out,) = seen
        assert out["tracestate"] == "first=1,second=2"

    @pytest.mark.parametrize("bad", INVALID_TRACEPARENTS)
    def test_invalid_traceparent_starts_new_trace_and_ignores_tracestate(self, bad: str) -> None:
        seen: list[dict[str, str]] = []
        with capture() as t:
            self._serve(
                Reply(on_call=lambda _req: seen.append(pyrock.inject_headers({}))),
                headers=[("traceparent", bad), ("tracestate", "vendor=abc")],
            )
        span = t.spans.one(name="http.request")
        assert span.trace_id != TRACE_ID
        assert span.parent_span_id is None
        (out,) = seen
        assert "tracestate" not in out

    def test_trace_propagation_disabled_ignores_incoming_context(self) -> None:
        with capture() as t:
            self._serve(headers=[("traceparent", TRACEPARENT)], trace_propagation=False)
        span = t.spans.one(name="http.request")
        assert span.trace_id != TRACE_ID
        assert span.parent_span_id is None

    # -- routes and cardinality ----------------------------------------

    def test_route_callable_supplies_the_template(self) -> None:
        with capture() as t:
            self._serve(path="/users/42", route=lambda _req: "/users/{id}")
            prom = parse(t.metrics.text())
        assert t.spans.one(name="http.request").attrs["http.route"] == "/users/{id}"
        assert prom.value("http_request_count", http_route="/users/{id}") == 1

    def test_framework_route_key_supplies_the_template(self) -> None:
        with capture() as t:
            self._serve(Reply(route="/orders/{order_id}"), path="/orders/981")
            prom = parse(t.metrics.text())
        assert t.spans.one(name="http.request").attrs["http.route"] == "/orders/{order_id}"
        assert prom.value("http_request_count", http_route="/orders/{order_id}") == 1

    def test_route_callable_wins_over_framework_key(self) -> None:
        with capture() as t:
            self._serve(Reply(route="/from/framework"), route=lambda _req: "/from/callable")
        assert t.spans.one(name="http.request").attrs["http.route"] == "/from/callable"

    def test_unmatched_paths_share_one_series_and_never_become_labels(self) -> None:
        raw_paths = ["/users/1001", "/users/1002?x=1", "/zz/unmatched/path"]
        with capture() as t:
            app = self.flavor.middleware(self.flavor.app(Reply()))
            for path in raw_paths:
                self.flavor.call(app, "GET", path, default_headers())
            prom = parse(t.metrics.text())
        assert len(prom.label_sets("http_request_count")) == 1
        assert prom.value("http_request_count", http_route="_") == len(raw_paths)
        for sample in prom.samples:
            for value in sample.labels.values():
                assert not any(value == p.split("?")[0] for p in raw_paths), sample
        for span in t.spans:
            assert span.attrs.get("http.route") == "_"
