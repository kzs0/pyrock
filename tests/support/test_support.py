"""Self-tests proving the shared fakes and helpers work (independent of pyrock)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from tests.support import otlp_decode, prom
from tests.support.asgi_driver import call_asgi, call_lifespan, call_websocket, run_asgi
from tests.support.collectors import (
    FakeMetadataServer,
    GRPCCollector,
    MetricsPushSink,
    OTLPHTTPCollector,
    wait_until,
)
from tests.support.logs import LogCapture, parse_json_lines
from tests.support.wsgi_driver import call_wsgi, make_environ

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
PARENT_ID = "00f067aa0ba902b7"


def _otlp_payload(name: str = "op", parent: str | None = PARENT_ID) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "name": name,
        "kind": 2,
        "startTimeUnixNano": "1000",
        "endTimeUnixNano": "2000",
        "attributes": [
            {"key": "s", "value": {"stringValue": "v"}},
            {"key": "i", "value": {"intValue": "42"}},
            {"key": "d", "value": {"doubleValue": 1.5}},
            {"key": "b", "value": {"boolValue": True}},
            {"key": "raw", "value": {"bytesValue": base64.b64encode(b"\x00\x01").decode()}},
            {"key": "arr", "value": {"arrayValue": {"values": [{"intValue": "1"}]}}},
        ],
        "events": [
            {
                "timeUnixNano": "1500",
                "name": "exception",
                "attributes": [{"key": "error.type", "value": {"stringValue": "builtins.E"}}],
            }
        ],
        "status": {"code": 2, "message": "boom"},
    }
    if parent:
        span["parentSpanId"] = parent
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                },
                "scopeSpans": [{"scope": {"name": "pyrock", "version": "0.1"}, "spans": [span]}],
            }
        ]
    }


def _post(
    url: str, body: bytes, headers: dict[str, str] | None = None, timeout: float = 5.0
) -> int:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _get(
    url: str, headers: dict[str, str] | None = None, method: str = "GET", timeout: float = 5.0
) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(), dict(exc.headers.items())


# ---------------------------------------------------------------- OTLP/HTTP


def test_otlp_collector_decodes_json_spans(otlp_collector: OTLPHTTPCollector) -> None:
    body = json.dumps(_otlp_payload()).encode()
    status = _post(
        otlp_collector.traces_url,
        body,
        {"Content-Type": "application/json", "X-Api-Key": "k"},
    )
    assert status == 200
    [span] = otlp_collector.wait_for_spans(1)
    assert span["name"] == "op"
    assert span["trace_id"] == TRACE_ID
    assert span["parent_span_id"] == PARENT_ID
    assert span["kind"] == 2
    assert span["start_time_unix_nano"] == 1000
    assert span["attributes"] == {
        "s": "v",
        "i": 42,
        "d": 1.5,
        "b": True,
        "raw": b"\x00\x01",
        "arr": [1],
    }
    assert span["events"][0]["attributes"] == {"error.type": "builtins.E"}
    assert span["status_code"] == 2
    assert span["status_message"] == "boom"
    assert span["resource"] == {"service.name": "svc"}
    assert span["scope_name"] == "pyrock"
    [req] = otlp_collector.trace_requests()
    assert req.header("x-api-key") == "k"
    assert otlp_collector.span("op") is not None


def test_otlp_collector_root_span_has_no_parent(otlp_collector: OTLPHTTPCollector) -> None:
    _post(
        otlp_collector.traces_url,
        json.dumps(_otlp_payload(parent=None)).encode(),
        {"Content-Type": "application/json"},
    )
    assert otlp_collector.wait_for_spans(1)[0]["parent_span_id"] is None


def test_wait_for_spans_wakes_on_background_post(otlp_collector: OTLPHTTPCollector) -> None:
    body = json.dumps(_otlp_payload()).encode()
    t = threading.Thread(
        target=_post,
        args=(otlp_collector.traces_url, body, {"Content-Type": "application/json"}),
    )
    t.start()
    assert len(otlp_collector.wait_for_spans(1, timeout=5)) == 1
    t.join()


def test_wait_for_spans_times_out(otlp_collector: OTLPHTTPCollector) -> None:
    with pytest.raises(AssertionError, match="waiting for 1 span"):
        otlp_collector.wait_for_spans(1, timeout=0.05)


def test_fail_next_and_default_status(otlp_collector: OTLPHTTPCollector) -> None:
    url = otlp_collector.traces_url
    otlp_collector.fail_next(2, status=503)
    assert _post(url, b"{}", {"Content-Type": "application/json"}) == 503
    assert _post(url, b"{}", {"Content-Type": "application/json"}) == 503
    assert _post(url, b"{}", {"Content-Type": "application/json"}) == 200
    otlp_collector.fail_next(1, status=500, path="/v1/profiles")
    assert _post(url, b"{}", {"Content-Type": "application/json"}) == 200
    assert _post(otlp_collector.profiles_url, b"\x0a\x00") == 500
    otlp_collector.set_default_status(429)
    assert _post(url, b"{}") == 429
    assert len(otlp_collector.requests) == 6


def test_hang_causes_client_timeout(otlp_collector: OTLPHTTPCollector) -> None:
    otlp_collector.hang()
    with pytest.raises((TimeoutError, socket.timeout, urllib.error.URLError)):
        _post(otlp_collector.traces_url, b"{}", timeout=0.2)
    otlp_collector.release()
    otlp_collector.wait_for_requests(1)
    assert _post(otlp_collector.traces_url, b"{}", {"Content-Type": "application/json"}) == 200


def test_profiles_recorded_as_raw_bytes(otlp_collector: OTLPHTTPCollector) -> None:
    payload = b"\x0a\x03abc"
    assert (
        _post(
            otlp_collector.profiles_url,
            payload,
            {"Content-Type": "application/x-protobuf", "x-profile-type": "cpu"},
        )
        == 200
    )
    [req] = otlp_collector.wait_for_profiles(1)
    assert req.body == payload
    assert req.header("X-Profile-Type") == "cpu"


def test_get_on_collector_is_405(otlp_collector: OTLPHTTPCollector) -> None:
    assert _get(otlp_collector.traces_url)[0] == 405


def test_push_sink_records_text(push_sink: MetricsPushSink) -> None:
    text = "# TYPE x counter\nx 1\n"
    status = _post(
        push_sink.endpoint + "/v1/metrics",
        text.encode(),
        {"Authorization": "Bearer k", "Content-Type": "text/plain; version=0.0.4"},
    )
    assert status == 200
    [req] = push_sink.wait_for_pushes(1)
    assert req.header("authorization") == "Bearer k"
    assert push_sink.texts() == [text]
    assert prom.parse(push_sink.texts()[0]).value("x") == 1


# ---------------------------------------------------------------- metadata


def test_aws_imdsv2_flow(metadata_server: FakeMetadataServer) -> None:
    base = metadata_server.aws_base
    status, _, _ = _get(base + "/latest/meta-data/instance-id")
    assert status == 401
    status, token, _ = _get(
        base + "/latest/api/token",
        {"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        method="PUT",
    )
    assert status == 200
    hdr = {"X-aws-ec2-metadata-token": token.decode()}
    assert _get(base + "/latest/meta-data/instance-id", hdr)[1] == b"i-0123456789abcdef0"
    assert _get(base + "/latest/meta-data/placement/region", hdr)[1] == b"us-west-2"
    assert _get(base + "/computeMetadata/v1/instance/id", {"Metadata-Flavor": "Google"})[0] == 404
    assert metadata_server.environ()["PYROCK_GCP_METADATA_ENDPOINT"] == ""


def test_gcp_metadata_requires_flavor_header() -> None:
    with FakeMetadataServer(provider="gcp") as server:
        url = server.gcp_base + "/computeMetadata/v1/instance/zone"
        assert _get(url)[0] == 403
        status, body, headers = _get(url, {"Metadata-Flavor": "Google"})
        assert status == 200
        assert body == b"projects/123456/zones/us-central1-a"
        assert headers.get("Metadata-Flavor") == "Google"
        assert server.environ()["PYROCK_AWS_METADATA_ENDPOINT"] == ""


def test_metadata_hang_triggers_timeout(metadata_server: FakeMetadataServer) -> None:
    metadata_server.hang()
    with pytest.raises((TimeoutError, socket.timeout, urllib.error.URLError)):
        _get(metadata_server.url + "/latest/meta-data/instance-id", timeout=0.2)


# ---------------------------------------------------------------- gRPC


@pytest.mark.compat
def test_grpc_collector_records_spans_and_metadata(grpc_collector: GRPCCollector) -> None:
    import grpc  # type: ignore[import-not-found]
    from opentelemetry.proto.collector.trace.v1 import (  # type: ignore[import-not-found]
        trace_service_pb2,
        trace_service_pb2_grpc,
    )
    from opentelemetry.proto.trace.v1 import trace_pb2  # type: ignore[import-not-found]

    request = trace_service_pb2.ExportTraceServiceRequest()
    rs = request.resource_spans.add()
    span = rs.scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex(TRACE_ID)
    span.span_id = bytes.fromhex(SPAN_ID)
    span.name = "grpc-op"
    span.kind = trace_pb2.Span.SPAN_KIND_CLIENT
    with grpc.insecure_channel(grpc_collector.endpoint) as channel:
        stub = trace_service_pb2_grpc.TraceServiceStub(channel)
        stub.Export(request, metadata=[("x-bedrock-key", "k")], timeout=5)
        grpc_collector.fail_next(1, code="UNAVAILABLE")
        with pytest.raises(grpc.RpcError) as info:
            stub.Export(request, timeout=5)
        assert info.value.code() == grpc.StatusCode.UNAVAILABLE
    spans = grpc_collector.wait_for_spans(2)
    assert spans[0]["name"] == "grpc-op"
    assert spans[0]["trace_id"] == TRACE_ID
    assert spans[0]["kind"] == 3
    assert grpc_collector.metadata[0]["x-bedrock-key"] == "k"


# ---------------------------------------------------------------- WSGI


def test_wsgi_driver_basic_and_environ() -> None:
    seen: dict[str, Any] = {}

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        seen.update(environ)
        body = environ["wsgi.input"].read()
        start_response("201 Created", [("Content-Type", "text/plain")])
        return [b"got:", body]

    resp = call_wsgi(
        app,
        "POST",
        "/items?x=1",
        headers=[("X-Multi", "a"), ("X-Multi", "b"), ("Content-Type", "text/plain")],
        body=b"payload",
    )
    assert resp.status_code == 201
    assert resp.body == b"got:payload"
    assert resp.header("content-type") == "text/plain"
    assert seen["PATH_INFO"] == "/items"
    assert seen["QUERY_STRING"] == "x=1"
    assert seen["HTTP_X_MULTI"] == "a,b"
    assert seen["CONTENT_TYPE"] == "text/plain"
    assert seen["HTTP_HOST"] == "testserver"
    assert resp.closed


def test_wsgi_driver_streaming_and_close() -> None:
    events: list[str] = []

    def app(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])

        def gen() -> Any:
            try:
                events.append("first")
                yield b"a"
                events.append("second")
                yield b"b"
            finally:
                events.append("closed")

        return gen()

    resp = call_wsgi(app, consume=False)
    assert resp.next_chunk() == b"a"
    assert events == ["first"]
    resp.close()
    assert events == ["first", "closed"]
    assert resp.next_chunk() is None


def test_wsgi_driver_write_and_restart_with_exc_info() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        write = start_response("200 OK", [])
        write(b"early:")
        try:
            raise ValueError("late")
        except ValueError:
            start_response("500 Internal Server Error", [], sys.exc_info())
        return [b"x"]

    resp = call_wsgi(app)
    assert resp.status_code == 500
    assert resp.body == b"early:x"
    assert len(resp.start_response_calls) == 2


def test_wsgi_driver_propagates_exceptions_and_closes() -> None:
    closed: list[bool] = []

    class Body:
        def __iter__(self) -> Any:
            yield b"x"
            raise RuntimeError("mid-stream")

        def close(self) -> None:
            closed.append(True)

    def app(environ: dict[str, Any], start_response: Any) -> Body:
        start_response("200 OK", [])
        return Body()

    def broken(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        raise KeyError("boom")

    with pytest.raises(RuntimeError, match="mid-stream"):
        call_wsgi(app)
    assert closed == [True]
    with pytest.raises(KeyError):
        call_wsgi(broken)


def test_make_environ_https_and_port() -> None:
    env = make_environ("get", "/", scheme="https", host="h", port=8443)
    assert env["REQUEST_METHOD"] == "GET"
    assert env["wsgi.url_scheme"] == "https"
    assert env["HTTP_HOST"] == "h:8443"


# ---------------------------------------------------------------- ASGI


async def _echo_app(scope: Any, receive: Any, send: Any) -> None:
    assert scope["type"] == "http"
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 103, "headers": [(b"link", b"</a>")]})
    await send({"type": "http.response.start", "status": 200, "headers": [(b"x-a", b"1")]})
    await send({"type": "http.response.body", "body": b"echo:", "more_body": True})
    await send({"type": "http.response.body", "body": body})
    assert (await receive())["type"] == "http.disconnect"


def test_asgi_driver_http_with_1xx_and_chunks() -> None:
    resp = run_asgi(_echo_app, "POST", "/e?q=1", body=[b"a", b"b"], headers={"X-Y": "z"})
    assert resp.informational == [103]
    assert resp.status == 200
    assert resp.header("x-a") == "1"
    assert resp.body == b"echo:ab"
    assert resp.complete
    assert resp.scope["query_string"] == b"q=1"
    assert (b"x-y", b"z") in resp.scope["headers"]


def test_asgi_driver_propagates_exception_with_partial_response() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom") as info:
        run_asgi(app)
    assert info.value.asgi_response.status == 200  # type: ignore[attr-defined]


def test_asgi_driver_on_message_hook() -> None:
    seen: list[str] = []

    async def hook(msg: dict[str, Any]) -> None:
        seen.append(msg["type"])

    asyncio.run(call_asgi(_echo_app, on_message=hook))
    assert seen[-1] == "http.response.body"


def test_asgi_lifespan_and_websocket() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        assert scope["type"] == "websocket"
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        while True:
            msg = await receive()
            if msg["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send", "text": "echo:" + msg["text"]})

    life = asyncio.run(call_lifespan(app))
    assert life.types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    ws = asyncio.run(call_websocket(app, incoming=["a", "b"]))
    assert ws.accepted
    assert ws.sent == ["echo:a", "echo:b"]


# ---------------------------------------------------------------- helpers


def test_prom_parser() -> None:
    text = (
        "# HELP op_count Total ops\n"
        "# TYPE op_count counter\n"
        'op_count{status="active",msg="a \\"q\\" \\\\ \\n"} 3\n'
        'op_count{status="_",msg=""} 1\n'
        "# TYPE lat histogram\n"
        'lat_bucket{le="+Inf"} 2\n'
        "lat_sum 1.5e3 1700000000\n"
        "lat_count 2\n"
    )
    metrics = prom.parse(text)
    assert metrics.value("op_count", status="active") == 3
    assert metrics.find("op_count", status="active")[0].labels["msg"] == 'a "q" \\ \n'
    assert metrics.type("op_count") == "counter"
    assert metrics.help("op_count") == "Total ops"
    assert metrics.value("lat_bucket", {"le": "+Inf"}) == 2
    assert metrics.value("lat_sum") == 1500
    assert metrics.find("lat_sum")[0].timestamp == 1700000000
    assert metrics.value("missing") is None
    with pytest.raises(AssertionError, match="2 samples"):
        metrics.value("op_count")
    with pytest.raises(ValueError, match="malformed"):
        prom.parse("bad line here {")


def test_log_capture() -> None:
    cap = LogCapture()
    cap.stream.write('{"msg":"a","level":"INFO","user.id":"1"}\n')
    cap.stream.write('{"msg":"b","level":"ERROR"}\n')
    assert cap.find(msg="b")["level"] == "ERROR"
    assert cap.find({"user.id": "1"})["msg"] == "a"
    assert cap.messages() == ["a", "b"]
    with pytest.raises(AssertionError, match="no log record"):
        cap.find(msg="zzz")
    cap.stream.write("not json\n")
    with pytest.raises(AssertionError, match="not JSON"):
        cap.records()
    assert len(cap.records(strict=False)) == 2
    cap.clear()
    assert cap.records() == []
    assert parse_json_lines('{"a": 1}\n\n') == [{"a": 1}]


def test_otlp_decode_any_values() -> None:
    assert otlp_decode.decode_any_value({"intValue": "-9"}) == -9
    assert otlp_decode.decode_any_value({"kvlistValue": {"values": []}}) == {}
    assert otlp_decode.decode_any_value(None) is None
    with pytest.raises(ValueError, match="unknown AnyValue"):
        otlp_decode.decode_any_value({"weird": 1})
    spans = otlp_decode.flatten_spans(_otlp_payload())
    assert spans[0]["attributes"]["i"] == 42


def test_require_proto_skips_or_returns() -> None:
    if otlp_decode.proto_available():
        assert otlp_decode.require_proto() is not None
    else:
        with pytest.raises(pytest.skip.Exception):
            otlp_decode.require_proto()


def test_grpc_collector_skips_without_compat_deps() -> None:
    try:
        import grpc  # type: ignore[import-not-found]  # noqa: F401
        import opentelemetry.proto  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        with pytest.raises(pytest.skip.Exception):
            GRPCCollector()
    else:
        pytest.skip("compat deps installed; covered by the compat test")


def test_clean_env_strips_bedrock_vars(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("BEDROCK_SERVICE", "x")
    monkeypatch.setenv("PYROCK_PROFILER_OWNER", "y")
    request.getfixturevalue("clean_env")
    assert "BEDROCK_SERVICE" not in os.environ
    assert "PYROCK_PROFILER_OWNER" not in os.environ
    assert os.environ["PYROCK_AWS_METADATA_ENDPOINT"] == ""


def test_wait_until() -> None:
    flag = threading.Event()
    threading.Timer(0.01, flag.set).start()
    wait_until(flag.is_set, timeout=5)
    with pytest.raises(AssertionError):
        wait_until(lambda: False, timeout=0.02)
