"""End-to-end: HTTP integration hardening (Phase 4 review fixes P0-2, P1-2, P1-3, P1-13).

* websocket and HTTP operations sharing one ``operation_name`` share one
  metric schema (``"_"`` for method and status on connections);
* ``asgi.Middleware`` tracks the running loop once per loop;
* raising ``route=`` / ``additional_attrs=`` callbacks are logged once and
  fall back, and the operation always ends;
* ``instrument_http_server`` accepts the middleware options.
"""

from __future__ import annotations

import asyncio
import http.client
import http.server
import json
import subprocess
import sys
from typing import Any

import pytest

import pyrock
import pyrock.http
from pyrock.asgi import Middleware as ASGIMiddleware
from pyrock.testing import capture
from pyrock.wsgi import Middleware as WSGIMiddleware
from tests.e2e._http_helpers import TRACE_ID, TRACEPARENT, default_headers, serve_handler
from tests.support.asgi_driver import call_asgi, call_lifespan, call_websocket, run_asgi
from tests.support.wsgi_driver import call_wsgi


@pytest.fixture(autouse=True)
def _env(clean_env: pytest.MonkeyPatch) -> None:
    return None


def _wsgi_ok(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


async def _asgi_ok(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _echo_ws(scope: Any, receive: Any, send: Any) -> None:
    await receive()
    await send({"type": "websocket.accept"})
    while (await receive())["type"] != "websocket.disconnect":
        pass


def _boom(_: object) -> Any:
    raise RuntimeError("secret-request-data")


def _warnings(t: Any) -> list[dict[str, Any]]:
    return [r for r in t.logs.find_all() if "callback raised" in str(r.get("msg", ""))]


# ------------------------------------------------------------ P0-2 websocket schema


def test_websocket_and_http_share_one_metric_schema_under_one_name() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            await _echo_ws(scope, receive, send)
        else:
            await _asgi_ok(scope, receive, send)

    middleware = ASGIMiddleware(app, operation_name="api", websocket=True)

    async def both() -> None:
        await call_asgi(middleware, "GET", "/", headers=default_headers())
        await call_websocket(middleware, incoming=["x"])
        await call_asgi(middleware, "GET", "/", headers=default_headers())

    with capture() as t:
        asyncio.run(both())
        assert t.metrics.value("api_count") == 3
        assert t.metrics.value("api_count", http_method="_", http_status_code="_") == 1
        assert t.metrics.value("api_successes", http_method="GET") == 2
    assert t.spans.names().count("api") == 3


def test_default_websocket_name_records_placeholder_labels() -> None:
    with capture() as t:
        asyncio.run(call_websocket(ASGIMiddleware(_echo_ws, websocket=True)))
        assert (
            t.metrics.value(
                "http_websocket_count", http_method="_", http_route="_", http_status_code="_"
            )
            == 1
        )


# ------------------------------------------------------------ P1-2 track_loop


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, bedrock: pyrock.Bedrock) -> None:
        self.loops: list[asyncio.AbstractEventLoop] = []
        original = type(bedrock).track_loop
        spy = self

        def track_loop(self: Any, loop: Any = None, /) -> None:
            spy.loops.append(loop)
            original(self, loop)

        monkeypatch.setattr(type(bedrock), "track_loop", track_loop)


def test_asgi_tracks_the_running_loop_once_per_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    with capture() as t:
        spy = _Spy(monkeypatch, t.bedrock)
        middleware = ASGIMiddleware(_asgi_ok)
        seen: list[asyncio.AbstractEventLoop] = []

        async def three() -> None:
            seen.append(asyncio.get_running_loop())
            for _ in range(3):
                await call_asgi(middleware, "GET", "/", headers=default_headers())

        asyncio.run(three())
        asyncio.run(three())
    assert spy.loops == seen
    assert len(spy.loops) == 2


def test_asgi_tracks_the_loop_on_lifespan_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    with capture() as t:
        spy = _Spy(monkeypatch, t.bedrock)
        loops: list[asyncio.AbstractEventLoop] = []

        async def run() -> Any:
            loops.append(asyncio.get_running_loop())
            return await call_lifespan(ASGIMiddleware(app))

        result = asyncio.run(run())
    assert result.types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert spy.loops == loops
    assert len(t.spans) == 0


def test_asgi_loop_tracking_feeds_asyncio_task_metrics() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        values.append(t.metrics.value("python_asyncio_tasks"))
        await _asgi_ok(scope, receive, send)

    values: list[float | None] = []
    with capture(runtime_metrics=True) as t:
        run_asgi(ASGIMiddleware(app), "GET", "/", headers=default_headers())
    assert values
    assert values[0] is not None
    assert values[0] >= 1


# ------------------------------------------------------------ P1-3 guarded callbacks


def test_wsgi_raising_route_callback_falls_back_and_logs_once() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        environ["pyrock.route"] = "/users/{id}"
        return _wsgi_ok(environ, start_response)

    middleware = WSGIMiddleware(app, route=_boom)
    with capture() as t:
        for _ in range(3):
            assert (
                call_wsgi(middleware, "GET", "/users/7", headers=default_headers()).status
                == "200 OK"
            )
    spans = list(t.spans)
    assert len(spans) == 3
    assert {s.attrs["http.route"] for s in spans} == {"/users/{id}"}
    (warning,) = _warnings(t)
    assert "route" in warning["msg"]
    assert "RuntimeError" in warning["msg"]
    assert "secret-request-data" not in json.dumps(list(t.logs.find_all()))


def test_wsgi_raising_additional_attrs_does_not_fail_the_request() -> None:
    middleware = WSGIMiddleware(_wsgi_ok, additional_attrs=_boom)
    with capture() as t:
        for _ in range(2):
            assert call_wsgi(middleware, "GET", "/", headers=default_headers()).status == "200 OK"
        assert t.metrics.value("http_request_successes") == 2
    (warning,) = _warnings(t)
    assert "additional_attrs" in warning["msg"]


def test_asgi_raising_callbacks_fall_back() -> None:
    middleware = ASGIMiddleware(_asgi_ok, route=_boom, additional_attrs=_boom)
    with capture() as t:
        response = run_asgi(middleware, "GET", "/", headers=default_headers())
    assert response.status == 200
    span = t.spans.one(name="http.request")
    assert span.attrs["http.route"] == "_"
    assert span.attrs["http.status_code"] == 200
    assert len(_warnings(t)) == 2


def test_asgi_websocket_raising_route_still_ends_the_connection() -> None:
    with capture() as t:
        asyncio.run(call_websocket(ASGIMiddleware(_echo_ws, websocket=True, route=_boom)))
    assert t.spans.one(name="http.websocket").attrs["http.route"] == "_"


def test_route_callback_returning_a_non_string_falls_back() -> None:
    middleware = WSGIMiddleware(_wsgi_ok, route=lambda environ: 42)  # type: ignore[arg-type,return-value]
    with capture() as t:
        call_wsgi(middleware, "GET", "/", headers=default_headers())
    assert t.spans.one(name="http.request").attrs["http.route"] == "_"
    assert len(_warnings(t)) == 1


def test_operation_ends_when_success_codes_lookup_raises() -> None:
    class Broken:
        def __contains__(self, code: object) -> bool:
            raise RuntimeError("broken container")

    middleware = WSGIMiddleware(_wsgi_ok, success_codes=Broken())
    with capture() as t, pytest.raises(RuntimeError, match="broken container"):
        call_wsgi(middleware, "GET", "/", headers=default_headers())
    # The operation ended (span exported) even though recording the outcome failed.
    assert t.spans.names() == ["http.request"]


# ------------------------------------------------------------ P1-13 http.server parity


class _Hello(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        status = 201 if self.path.startswith("/created") else 200
        if self.path.startswith("/missing"):
            status = 404
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"hi")


def _get(base: str, path: str, headers: dict[str, str] | None = None) -> int:
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def test_instrument_http_server_additional_labels_and_attrs() -> None:
    handler = pyrock.http.instrument_http_server(
        _Hello,
        additional_labels=["user_type"],
        additional_attrs=lambda h: {"user_type": h.headers.get("X-User-Type", "anon")},
    )
    with capture() as t, serve_handler(handler) as base:
        assert _get(base, "/", {"X-User-Type": "admin"}) == 200
        assert t.metrics.value("http_request_count", user_type="admin") == 1
    assert t.spans.one(name="http.request").attrs["user_type"] == "admin"


def test_instrument_http_server_success_codes() -> None:
    handler = pyrock.http.instrument_http_server(_Hello, success_codes={201, 404})
    with capture() as t:
        with serve_handler(handler) as base:
            _get(base, "/created")
            _get(base, "/missing")
            _get(base, "/")
        # The handler ends its operation after writing the response, so read the
        # metrics only once the server has shut down and finished the request.
        assert t.metrics.value("http_request_successes") == 2
        assert t.metrics.value("http_request_failures", http_status_code="200") == 1


def test_instrument_http_server_trace_propagation_off() -> None:
    handler = pyrock.http.instrument_http_server(_Hello, trace_propagation=False)
    with capture() as t, serve_handler(handler) as base:
        _get(base, "/", {"traceparent": TRACEPARENT})
    span = t.spans.one(name="http.request")
    assert span.trace_id != TRACE_ID
    assert span.parent_span_id is None


def test_instrument_http_server_raising_callbacks_fall_back() -> None:
    handler = pyrock.http.instrument_http_server(_Hello, route=_boom, additional_attrs=_boom)
    with capture() as t, serve_handler(handler) as base:
        assert _get(base, "/") == 200
        assert _get(base, "/") == 200
    assert [s.attrs["http.route"] for s in t.spans] == ["_", "_"]
    assert len(_warnings(t)) == 2


# ------------------------------------------------------------ P2-12 lazy imports


def _loaded_after(code: str) -> set[str]:
    script = f"import json, sys\n{code}\nprint(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True, timeout=60
    )
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_import_pyrock_does_not_load_the_http_core() -> None:
    assert "pyrock._httpcore" not in _loaded_after("import pyrock")


def test_import_pyrock_http_does_not_load_http_server() -> None:
    loaded = _loaded_after("import pyrock.http")
    assert "pyrock._httpcore" in loaded
    assert "http.server" not in loaded
    assert "pyrock.wsgi" not in loaded


def test_asgi_does_not_import_wsgi() -> None:
    assert "pyrock.wsgi" not in _loaded_after("import pyrock.asgi")
