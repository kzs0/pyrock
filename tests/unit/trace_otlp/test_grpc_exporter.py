"""OTLP/gRPC exporter (``pyrock.trace.otlp.grpc``) over the in-house HTTP/2 client.

Component tests run against the scripted h2c server in ``_h2server``; the
``compat`` tests use a real grpcio collector from ``tests/support``.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterator

import pytest

import pyrock
from pyrock.trace import Span, SpanContext, SpanKind, StatusCode
from pyrock.trace.otlp import ExportError, PermanentExportError, _pb
from pyrock.trace.otlp.grpc import EXPORT_PATH, GRPCExporter, frame_message, parse_endpoint
from tests.support.collectors import GRPCCollector
from tests.unit.trace_otlp import _h2server as srv
from tests.unit.trace_otlp._h2server import H2TestServer, Request, ServerConn

TID = bytes(range(1, 17))


def _span(name: str = "op", n: int = 1) -> Span:
    span = Span(
        name,
        SpanContext(TID, bytes([n]) * 8, 1),
        kind=SpanKind.SERVER,
        attrs={"k": "v"},
        start_time_ns=1_000,
    )
    span.set_status(StatusCode.OK)
    span.end(end_time_ns=2_000)
    return span


def _status_handler(grpc_status: str | None, message: str | None = None) -> srv.Handler:
    def handler(conn: ServerConn, request: Request) -> None:
        trailers = b"" if grpc_status is None else srv.lit("grpc-status", grpc_status)
        if message is not None:
            trailers += srv.lit("grpc-message", message)
        conn.respond_grpc(request.stream_id, trailers=trailers)

    return handler


@pytest.fixture
def server() -> Iterator[H2TestServer]:
    with H2TestServer() as s:
        yield s


def _exporter(server: H2TestServer, **kwargs: object) -> GRPCExporter:
    return GRPCExporter(server.endpoint, service="svc", insecure=True, timeout=5.0, **kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------- framing


def test_export_path() -> None:
    assert EXPORT_PATH == "/opentelemetry.proto.collector.trace.v1.TraceService/Export"


def test_frame_message() -> None:
    assert frame_message(b"abc") == b"\x00\x00\x00\x00\x03abc"
    assert frame_message(b"") == b"\x00" * 5
    big = b"x" * 70_000
    assert frame_message(big) == b"\x00" + (70_000).to_bytes(4, "big") + big


# ---------------------------------------------------------------- endpoint


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("localhost:4317", ("localhost", 4317)),
        ("127.0.0.1:1", ("127.0.0.1", 1)),
        ("collector.example.com:443", ("collector.example.com", 443)),
        ("[::1]:4317", ("::1", 4317)),
        ("http://collector:4317", ("collector", 4317)),
        ("https://collector:4318/v1/traces", ("collector", 4318)),
        ("https://collector.example.com", ("collector.example.com", 4317)),
        ("http://[::1]:9000", ("::1", 9000)),
    ],
)
def test_parse_endpoint(endpoint: str, expected: tuple[str, int]) -> None:
    assert parse_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    ["", ":4317", "host:", "host:abc", "host:0", "host:65536", "host:-1", "a b:1", "[::1"],
)
def test_parse_endpoint_rejects_malformed(endpoint: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_endpoint(endpoint)


def test_constructor_rejects_malformed_endpoint() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        GRPCExporter("host:notaport", service="svc", insecure=True)


@pytest.mark.parametrize(
    "headers",
    [
        {"bad name": "x"},
        {"x-key": "a\r\nb"},
        {"x-key": "a\nb"},
        {"": "x"},
        {":path": "/evil"},
        {"content-type": "text/plain"},
        {"te": "gzip"},
        {"x-kéy": "x"},
        {"x-key": "café"},
    ],
)
def test_constructor_rejects_invalid_metadata(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        GRPCExporter("127.0.0.1:4317", service="svc", insecure=True, headers=headers)


def test_endpoint_property() -> None:
    exporter = GRPCExporter("127.0.0.1:4317", service="svc", insecure=True)
    try:
        assert exporter.endpoint == "127.0.0.1:4317"
    finally:
        exporter.shutdown()


# ------------------------------------------------------------------ export


def test_export_sends_unary_grpc_request(server: H2TestServer) -> None:
    spans = [_span("a", 1), _span("b", 2)]
    exporter = _exporter(server, resource={"env": "prod"}, headers={"x-api-key": "secret"})
    try:
        exporter.export(spans)
    finally:
        exporter.shutdown()
    (request,) = server.requests
    assert request.header(":method") == "POST"
    assert request.header(":path") == EXPORT_PATH
    assert request.header(":scheme") == "http"
    assert request.header(":authority") == server.endpoint
    assert request.header("content-type") == "application/grpc"
    assert request.header("te") == "trailers"
    assert request.header("x-api-key") == "secret"
    expected = _pb.encode_export_trace_request(
        spans, service="svc", resource={"env": "prod"}, scope_version=pyrock.__version__
    )
    assert request.body == frame_message(expected)


def test_export_reuses_connection(server: H2TestServer) -> None:
    exporter = _exporter(server)
    try:
        for i in range(3):
            exporter.export([_span(n=i + 1)])
    finally:
        exporter.shutdown()
    assert len(server.connections) == 1
    assert len(server.requests) == 3


def test_export_empty_batch_sends_nothing(server: H2TestServer) -> None:
    exporter = _exporter(server)
    try:
        exporter.export([])
    finally:
        exporter.shutdown()
    assert server.requests == []


def test_export_with_explicit_timeout(server: H2TestServer) -> None:
    exporter = _exporter(server)
    try:
        exporter.export([_span()], timeout=3.0)
    finally:
        exporter.shutdown()
    assert len(server.requests) == 1


@pytest.mark.parametrize("code", ["3", "7", "16"])
def test_permanent_grpc_status_raises_permanent_error(code: str) -> None:
    # INVALID_ARGUMENT, PERMISSION_DENIED, UNAUTHENTICATED cannot be fixed by retrying.
    with H2TestServer(_status_handler(code, "bad request")) as server:
        exporter = _exporter(server)
        try:
            with pytest.raises(PermanentExportError) as info:
                exporter.export([_span()])
        finally:
            exporter.shutdown()
    assert "bad request" in str(info.value)


@pytest.mark.parametrize("code", ["14", "4", "8", "10"])
def test_retryable_grpc_status_raises_export_error(code: str) -> None:
    # UNAVAILABLE, DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED, ABORTED
    with H2TestServer(_status_handler(code, "try later")) as server:
        exporter = _exporter(server)
        try:
            with pytest.raises(ExportError) as info:
                exporter.export([_span()])
        finally:
            exporter.shutdown()
    assert not isinstance(info.value, PermanentExportError)
    assert "try later" in str(info.value)


def test_missing_grpc_status_is_an_error() -> None:
    with H2TestServer(_status_handler(None)) as server:
        exporter = _exporter(server)
        try:
            with pytest.raises(ExportError):
                exporter.export([_span()])
        finally:
            exporter.shutdown()


def test_http_error_status_is_retryable() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        conn.send_headers(request.stream_id, srv.status_block(503), end_stream=True)

    with H2TestServer(handler) as server:
        exporter = _exporter(server)
        try:
            with pytest.raises(ExportError) as info:
                exporter.export([_span()])
        finally:
            exporter.shutdown()
    assert not isinstance(info.value, PermanentExportError)


def test_huffman_grpc_message_is_decoded() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        trailers = srv.lit("grpc-status", "14") + srv.lit_huffman_value(
            "grpc-message", bytes.fromhex("a8eb10649cbf")
        )
        conn.respond_grpc(request.stream_id, trailers=trailers)

    with H2TestServer(handler) as server:
        exporter = _exporter(server)
        try:
            with pytest.raises(ExportError, match="no-cache"):
                exporter.export([_span()])
        finally:
            exporter.shutdown()


def test_connection_refused_is_retryable_export_error() -> None:
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    exporter = GRPCExporter(f"127.0.0.1:{port}", service="svc", insecure=True, timeout=2.0)
    try:
        with pytest.raises(ExportError) as info:
            exporter.export([_span()])
    finally:
        exporter.shutdown()
    assert not isinstance(info.value, PermanentExportError)


def test_timeout_is_export_error() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        return  # never answer

    with H2TestServer(handler) as server:
        exporter = _exporter(server)
        try:
            with pytest.raises(ExportError):
                exporter.export([_span()], timeout=0.3)
        finally:
            exporter.shutdown()


def test_recovers_after_server_drops_connection() -> None:
    calls = {"n": 0}

    def handler(conn: ServerConn, request: Request) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            conn.sock.shutdown(socket.SHUT_RDWR)
            return
        conn.respond_grpc(request.stream_id)

    with H2TestServer(handler) as server:
        exporter = _exporter(server)
        try:
            with pytest.raises(ExportError):
                exporter.export([_span()])
            exporter.export([_span()])
        finally:
            exporter.shutdown()
        assert len(server.connections) == 2


def test_shutdown_closes_connection_and_is_idempotent(server: H2TestServer) -> None:
    exporter = _exporter(server)
    exporter.export([_span()])
    exporter.shutdown()
    exporter.shutdown()
    server.wait_eof()


def test_export_after_shutdown_sends_nothing(server: H2TestServer) -> None:
    exporter = _exporter(server)
    exporter.shutdown()
    with contextlib.suppress(ExportError):
        exporter.export([_span()])
    assert server.requests == []


# ------------------------------------------------------------------ compat


@pytest.mark.compat
def test_export_to_real_grpc_collector(grpc_collector: GRPCCollector) -> None:
    exporter = GRPCExporter(
        grpc_collector.endpoint,
        service="svc",
        resource={"env": "prod"},
        headers={"x-api-key": "secret"},
        insecure=True,
        timeout=5.0,
    )
    try:
        exporter.export([_span("a", 1), _span("b", 2)])
    finally:
        exporter.shutdown()
    spans = grpc_collector.wait_for_spans(2)
    assert sorted(s["name"] for s in spans) == ["a", "b"]
    assert spans[0]["resource"] == {"service.name": "svc", "env": "prod"}
    assert spans[0]["scope_name"] == "pyrock"
    assert spans[0]["scope_version"] == pyrock.__version__
    assert grpc_collector.metadata[0]["x-api-key"] == "secret"


@pytest.mark.compat
def test_real_collector_invalid_argument_is_permanent(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(1, code="INVALID_ARGUMENT", details="nope")
    exporter = GRPCExporter(grpc_collector.endpoint, service="svc", insecure=True, timeout=5.0)
    try:
        with pytest.raises(PermanentExportError, match="nope"):
            exporter.export([_span()])
        exporter.export([_span()])  # the connection is still usable
    finally:
        exporter.shutdown()
    grpc_collector.wait_for_calls(2)


@pytest.mark.compat
def test_real_collector_unavailable_is_retryable(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(1, code="UNAVAILABLE")
    exporter = GRPCExporter(grpc_collector.endpoint, service="svc", insecure=True, timeout=5.0)
    try:
        with pytest.raises(ExportError) as info:
            exporter.export([_span()])
    finally:
        exporter.shutdown()
    assert not isinstance(info.value, PermanentExportError)


@pytest.mark.compat
def test_real_collector_many_sequential_exports(grpc_collector: GRPCCollector) -> None:
    exporter = GRPCExporter(grpc_collector.endpoint, service="svc", insecure=True, timeout=5.0)
    try:
        for i in range(20):
            exporter.export([_span(f"s{i}", i + 1)])
    finally:
        exporter.shutdown()
    assert grpc_collector.wait_for_calls(20) >= 20
