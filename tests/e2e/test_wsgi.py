"""End-to-end: ``pyrock.wsgi.Middleware`` driven like a WSGI server would (DESIGN §9.1).

The shared WSGI/ASGI contract (attrs, labels, status, routes, exceptions,
trace context) runs via :class:`MiddlewareContract`; this module adds the
WSGI-specific behaviours: last ``start_response`` wins, ``write()``, streaming
until ``close()``, and the iterable's ``close()`` always running.
"""

from __future__ import annotations

import sys
import time
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

import pyrock
import pyrock.http
from pyrock.testing import capture
from pyrock.trace import SpanKind
from pyrock.wsgi import ROUTE_KEY, Middleware
from tests.e2e._http_helpers import (
    TRACE_ID,
    TRACEPARENT,
    MiddlewareContract,
    WSGIFlavor,
    default_headers,
    require_param,
    serve_threaded,
    serve_wsgi,
)
from tests.support.wsgi_driver import call_wsgi


class TestWSGIMiddlewareContract(MiddlewareContract):
    flavor = WSGIFlavor()


@pytest.fixture(autouse=True)
def _env(clean_env: pytest.MonkeyPatch) -> None:
    return None


class ClosingIterable:
    """A response iterable that records ``close()`` and can fail mid-iteration."""

    def __init__(self, chunks: list[bytes], *, fail_after: int | None = None) -> None:
        self.chunks = chunks
        self.fail_after = fail_after
        self.closed = 0

    def __iter__(self) -> Iterator[bytes]:
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise ValueError("stream broke")
            yield chunk

    def close(self) -> None:
        self.closed += 1


def test_route_key_constant() -> None:
    assert ROUTE_KEY == "pyrock.route"


def test_final_status_is_the_last_start_response_call() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [("Content-Type", "text/plain")])
        try:
            raise LookupError("late failure")
        except LookupError:
            start_response("503 Service Unavailable", [], sys.exc_info())
        return [b"unavailable"]

    with capture() as t:
        resp = call_wsgi(Middleware(app), "GET", "/", headers=default_headers())
    assert resp.status_code == 503
    span = t.spans.one(name="http.request")
    assert span.attrs["http.status_code"] == 503
    assert span.failed


def test_write_callable_body_reaches_the_client() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        write = start_response("200 OK", [("Content-Type", "text/plain")])
        write(b"written-")
        return [b"iterated"]

    with capture() as t:
        resp = call_wsgi(Middleware(app), "GET", "/", headers=default_headers())
    assert resp.body == b"written-iterated"
    assert t.spans.one(name="http.request").attrs["http.status_code"] == 200


def test_response_is_streamed_not_buffered() -> None:
    produced: list[int] = []

    def app(environ: dict[str, Any], start_response: Any) -> Iterator[bytes]:
        start_response("200 OK", [])
        for i in range(3):
            produced.append(i)
            yield b"chunk%d" % i

    with capture():
        resp = call_wsgi(Middleware(app), "GET", "/", headers=default_headers(), consume=False)
        assert resp.next_chunk() == b"chunk0"
        assert produced == [0]
        resp.consume()
        resp.close()
    assert resp.body == b"chunk0chunk1chunk2"


def test_streaming_duration_lasts_until_close() -> None:
    ops: list[Any] = []

    def app(environ: dict[str, Any], start_response: Any) -> Iterator[bytes]:
        ops.append(pyrock.current_operation())
        start_response("200 OK", [])
        yield b"first"
        yield b"second"

    with capture() as t:
        resp = call_wsgi(Middleware(app), "GET", "/", headers=default_headers(), consume=False)
        resp.consume()
        (op,) = ops
        assert op is not None
        assert not op.ended, "operation ended before the iterable was closed"
        marker = time.time_ns()
        resp.close()
        assert op.ended
    span = t.spans.one(name="http.request")
    assert span.end_time_ns >= marker


def test_iterable_close_is_called_after_success() -> None:
    body = ClosingIterable([b"a", b"b"])

    def app(environ: dict[str, Any], start_response: Any) -> ClosingIterable:
        start_response("200 OK", [])
        return body

    with capture():
        resp = call_wsgi(Middleware(app), "GET", "/", headers=default_headers())
    assert resp.body == b"ab"
    assert body.closed == 1


def test_iterable_close_is_called_when_iteration_raises() -> None:
    body = ClosingIterable([b"a", b"b"], fail_after=1)

    def app(environ: dict[str, Any], start_response: Any) -> ClosingIterable:
        start_response("200 OK", [])
        return body

    with capture() as t, pytest.raises(ValueError, match="stream broke"):
        call_wsgi(Middleware(app), "GET", "/", headers=default_headers())
    assert body.closed == 1
    span = t.spans.one(name="http.request")
    assert span.failed
    assert span.attrs["http.status_code"] == 200  # the status already sent


def test_iterable_close_is_called_when_client_disconnects_early() -> None:
    body = ClosingIterable([b"a", b"b", b"c"])

    def app(environ: dict[str, Any], start_response: Any) -> ClosingIterable:
        start_response("200 OK", [])
        return body

    with capture() as t:
        resp = call_wsgi(Middleware(app), "GET", "/", headers=default_headers(), consume=False)
        resp.next_chunk()
        resp.close()
    assert body.closed == 1
    assert t.spans.one(name="http.request").attrs["http.status_code"] == 200


def test_exception_before_start_response_reports_500() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        raise KeyError("missing")

    with capture() as t, pytest.raises(KeyError):
        call_wsgi(Middleware(app), "GET", "/", headers=default_headers())
    span = t.spans.one(name="http.request")
    assert span.failed
    assert span.attrs["http.status_code"] == 500


def test_route_key_set_by_framework_hook_in_environ() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        environ["pyrock.route"] = "/items/{item_id}"
        start_response("200 OK", [])
        return [b""]

    with capture() as t:
        call_wsgi(Middleware(app), "GET", "/items/77", headers=default_headers())
    assert t.spans.one(name="http.request").attrs["http.route"] == "/items/{item_id}"


def test_route_callable_receives_the_environ() -> None:
    seen: list[str] = []

    def route(environ: dict[str, Any]) -> str:
        seen.append(environ["PATH_INFO"])
        return "/r/{x}"

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b""]

    with capture():
        call_wsgi(Middleware(app, route=route), "GET", "/r/5", headers=default_headers())
    assert seen == ["/r/5"]


def test_https_scheme_from_environ() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b""]

    with capture() as t:
        call_wsgi(Middleware(app), "GET", "/", headers=default_headers(), scheme="https")
    assert t.spans.one(name="http.request").attrs["http.scheme"] == "https"


def test_under_a_real_wsgi_server() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"served"]

    with capture() as t, serve_wsgi(Middleware(app)) as base:
        with pyrock.operation("caller"):
            resp = pyrock.http.get(base + "/")
        assert resp.status == 200
        assert resp.read() == b"served"
    server = t.spans.one(name="http.request")
    assert server.kind is SpanKind.SERVER
    assert server.attrs["http.status_code"] == 200
    assert server.trace_id == t.spans.one(name="caller").trace_id


def test_real_server_continues_incoming_traceparent() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b""]

    with capture() as t, serve_wsgi(Middleware(app)) as base:
        req = urllib.request.Request(base + "/", headers={"TraceParent": TRACEPARENT})
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    assert t.spans.one(name="http.request").trace_id == TRACE_ID


def test_threaded_server_requests_are_instrumented_without_context_copy() -> None:
    seen: list[Any] = []

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        seen.append(pyrock.current_operation())
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"threaded"]

    with capture() as t:
        middleware = Middleware(app)  # captures the active instance
        with serve_threaded(middleware) as base:
            for _ in range(2):
                with urllib.request.urlopen(base + "/", timeout=5) as resp:
                    assert resp.read() == b"threaded"
    spans = t.spans.find(name="http.request")
    assert len(spans) == 2
    assert all(s.kind is SpanKind.SERVER for s in spans)
    assert [op is not None for op in seen] == [True, True]
    assert sorted(op.span_id for op in seen) == sorted(s.span_id for s in spans)


def test_threaded_server_uses_explicit_bedrock() -> None:
    require_param(Middleware, "bedrock")

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b""]

    with capture() as outer:
        with capture() as inner:
            middleware = Middleware(app, bedrock=outer.bedrock)
            with serve_threaded(middleware) as base:
                urllib.request.urlopen(base + "/", timeout=5).close()
        assert len(inner.spans) == 0
    assert len(outer.spans.find(name="http.request")) == 1
