"""End-to-end: ``pyrock.asgi.Middleware`` driven by the ASGI 3 test driver (DESIGN §9.2).

The shared WSGI/ASGI contract runs via :class:`MiddlewareContract`; this
module adds the ASGI-specific behaviours: 1xx responses skipped for the final
status, streaming until the last ``more_body=False``, ``scope["route"]``
templates, raw-cased header names, request bodies, and lifespan / websocket
passthrough (and opt-in websocket instrumentation).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import pyrock
from pyrock.asgi import ROUTE_KEY, Middleware
from pyrock.testing import capture
from pyrock.trace import SpanKind
from tests.e2e._http_helpers import (
    PARENT_SPAN_ID,
    TRACE_ID,
    TRACEPARENT,
    ASGIFlavor,
    MiddlewareContract,
    default_headers,
)
from tests.support.asgi_driver import call_asgi, call_lifespan, call_websocket, run_asgi


class TestASGIMiddlewareContract(MiddlewareContract):
    flavor = ASGIFlavor()


@pytest.fixture(autouse=True)
def _env(clean_env: pytest.MonkeyPatch) -> None:
    return None


async def _drain(receive: Any) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            return body


def test_route_key_constant() -> None:
    assert ROUTE_KEY == "pyrock.route"


def test_informational_responses_are_skipped_for_final_status() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _drain(receive)
        await send({"type": "http.response.start", "status": 103, "headers": []})
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    with capture() as t:
        resp = run_asgi(Middleware(app), "GET", "/", headers=default_headers())
    assert resp.informational == [103]
    assert resp.status == 202
    assert t.spans.one(name="http.request").attrs["http.status_code"] == 202


def test_streaming_duration_lasts_until_final_body() -> None:
    ops: list[Any] = []
    observed: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _drain(receive)
        ops.append(pyrock.current_operation())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"one", "more_body": True})
        observed["ended_mid_stream"] = ops[0].ended
        observed["marker"] = time.time_ns()
        await send({"type": "http.response.body", "body": b"two", "more_body": False})

    with capture() as t:
        resp = run_asgi(Middleware(app), "GET", "/", headers=default_headers())
    assert resp.body == b"onetwo"
    assert observed["ended_mid_stream"] is False
    assert ops[0].ended
    assert t.spans.one(name="http.request").end_time_ns >= observed["marker"]


def test_request_body_chunks_pass_through() -> None:
    received: list[bytes] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        received.append(await _drain(receive))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with capture():
        run_asgi(Middleware(app), "POST", "/", headers=default_headers(), body=[b"ab", b"cd"])
    assert received == [b"abcd"]


def test_starlette_style_scope_route_path_format() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _drain(receive)
        scope["route"] = SimpleNamespace(path_format="/users/{user_id}", path="/users/{user_id}")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with capture() as t:
        run_asgi(Middleware(app), "GET", "/users/12345", headers=default_headers())
    assert t.spans.one(name="http.request").attrs["http.route"] == "/users/{user_id}"


def test_mixed_case_raw_header_names_are_matched() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _drain(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    raw = [
        (b"host", b"api.example.test"),
        (b"TraceParent", TRACEPARENT.encode()),
    ]
    with capture() as t:
        run_asgi(Middleware(app), "GET", "/", scope={"headers": raw})
    span = t.spans.one(name="http.request")
    assert span.trace_id == TRACE_ID
    assert span.parent_span_id == PARENT_SPAN_ID


def test_exception_after_response_start_is_failure_and_reraised() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _drain(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part", "more_body": True})
        raise ConnectionAbortedError("upstream went away")

    with capture() as t, pytest.raises(ConnectionAbortedError) as info:
        run_asgi(Middleware(app), "GET", "/", headers=default_headers())
    assert info.value.asgi_response.chunks == [b"part"]  # type: ignore[attr-defined]
    assert t.spans.one(name="http.request").failed


def test_https_scheme_from_scope() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _drain(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with capture() as t:
        run_asgi(Middleware(app), "GET", "/", headers=default_headers(), scheme="https")
    assert t.spans.one(name="http.request").attrs["http.scheme"] == "https"


def test_concurrent_requests_get_separate_operations() -> None:
    seen: list[str | None] = []

    async def scenario() -> None:
        both_inside = asyncio.Event()
        count = 0

        async def app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal count
            await _drain(receive)
            count += 1
            if count == 2:
                both_inside.set()
            await both_inside.wait()
            op = pyrock.current_operation()
            seen.append(op.span_id if op else None)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = Middleware(app)
        await asyncio.gather(
            call_asgi(mw, "GET", "/a", headers=default_headers()),
            call_asgi(mw, "GET", "/b", headers=default_headers()),
        )

    with capture() as t:
        asyncio.run(scenario())
    spans = t.spans.find(name="http.request")
    assert len(spans) == 2
    assert sorted(s.span_id for s in spans) == sorted(x for x in seen if x)
    assert spans[0].trace_id != spans[1].trace_id


# ---------------------------------------------------------------- lifespan / websocket


def test_lifespan_scope_passes_through_untraced() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "lifespan"
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    with capture() as t:
        result = asyncio.run(call_lifespan(Middleware(app)))
    assert result.types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert len(t.spans) == 0


async def _echo_ws(scope: Any, receive: Any, send: Any) -> None:
    assert scope["type"] == "websocket"
    message = await receive()
    assert message["type"] == "websocket.connect"
    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        await send({"type": "websocket.send", "text": "echo:" + message.get("text", "")})


def test_websocket_passes_through_untraced_by_default() -> None:
    with capture() as t:
        result = asyncio.run(call_websocket(Middleware(_echo_ws), incoming=["hi", "there"]))
    assert result.accepted
    assert result.sent == ["echo:hi", "echo:there"]
    assert len(t.spans) == 0


def test_websocket_instrumentation_covers_the_whole_connection() -> None:
    ops: list[Any] = []
    states: list[bool] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        ops.append(pyrock.current_operation())
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if ops[0] is not None:
                states.append(ops[0].ended)
            if message["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send", "text": message.get("text", "")})

    with capture() as t:
        result = asyncio.run(call_websocket(Middleware(app, websocket=True), incoming=["a", "b"]))
    assert result.sent == ["a", "b"]
    assert ops[0] is not None
    assert states == [False, False, False]
    assert ops[0].ended
    (span,) = list(t.spans)
    assert span.kind is SpanKind.SERVER
