"""Review fixes in ``pyrock.trace.otlp.grpc``: one target parser, lock-free connect, safe errors."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from pyrock.trace import Span, SpanContext, SpanKind, StatusCode
from pyrock.trace.otlp import ExportError
from pyrock.trace.otlp.grpc import (
    DEFAULT_PORT,
    GRPCExporter,
    format_target,
    parse_endpoint,
    parse_target,
)
from tests.unit.trace_otlp._h2server import H2TestServer

pytestmark = pytest.mark.unit


def _span() -> Span:
    span = Span("op", SpanContext(bytes(range(1, 17)), b"\x01" * 8, 1), kind=SpanKind.SERVER)
    span.set_status(StatusCode.OK)
    span.end()
    return span


# ------------------------------------------------------------- parse_target


@pytest.mark.parametrize(
    ("target", "insecure", "expected"),
    [
        ("http://col:4317", False, ("col", 4317, True)),
        ("HTTP://col", False, ("col", DEFAULT_PORT, True)),
        ("https://col:4318/v1/traces?x=1#f", False, ("col", 4318, False)),
        ("https://col", True, ("col", 4317, True)),
        ("col:9000", False, ("col", 9000, False)),
        ("col:9000", True, ("col", 9000, True)),
        ("col", False, ("col", 4317, False)),
        ("[::1]", False, ("::1", 4317, False)),
        ("http://[::1]:55680/", False, ("::1", 55680, True)),
        ("10.0.0.1:1", False, ("10.0.0.1", 1, False)),
    ],
)
def test_parse_target(target: str, insecure: bool, expected: tuple[str, int, bool]) -> None:
    assert parse_target(target, insecure=insecure) == expected


@pytest.mark.parametrize(
    "target",
    ["", ":4317", "host:", "host:0", "host:65536", "a b", "[::1", "[nope]:1", "ftp://host"],
)
def test_parse_target_rejects_malformed(target: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_target(target)


@pytest.mark.parametrize(
    "target", ["http://user:s3cr3t@col:4317", "https://s3cr3t@col", "s3cr3t@col:4317"]
)
def test_parse_target_rejects_credentials_without_echoing_them(target: str) -> None:
    with pytest.raises(ValueError, match="credentials") as info:
        parse_target(target)
    assert "s3cr3t" not in str(info.value)


def test_parse_endpoint_errors_never_echo_credentials() -> None:
    with pytest.raises(ValueError) as info:  # noqa: PT011
        parse_endpoint("https://user:s3cr3t@col:notaport/x?token=s3cr3t")
    assert "s3cr3t" not in str(info.value)


def test_parse_endpoint_still_requires_a_port_for_bare_hosts() -> None:
    with pytest.raises(ValueError, match="no port"):
        parse_endpoint("collector")


@pytest.mark.parametrize(
    ("host", "port", "text"), [("col", 1, "col:1"), ("::1", 4317, "[::1]:4317")]
)
def test_format_target(host: str, port: int, text: str) -> None:
    assert format_target(host, port) == text


# ------------------------------------------------------------ the exporter


@pytest.fixture
def server() -> Iterator[H2TestServer]:
    with H2TestServer() as s:
        yield s


def test_http_url_target_uses_plaintext_without_insecure(server: H2TestServer) -> None:
    exporter = GRPCExporter(f"http://{server.endpoint}", service="svc", timeout=5.0)
    try:
        exporter.export([_span()])
    finally:
        exporter.shutdown()
    assert len(server.requests) == 1


def test_connect_errors_show_a_sanitized_endpoint() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    exporter = GRPCExporter(
        f"http://127.0.0.1:{port}/path?token=s3cr3t", service="svc", timeout=2.0
    )
    try:
        with pytest.raises(ExportError) as info:
            exporter.export([_span()])
    finally:
        exporter.shutdown()
    assert "s3cr3t" not in str(info.value)
    assert f"127.0.0.1:{port}" in str(info.value)


@pytest.fixture
def silent_listener() -> Iterator[int]:
    """Accepts TCP connections and never speaks, so a TLS handshake hangs."""
    listener = socket.create_server(("127.0.0.1", 0))
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            accepted.append(conn)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        listener.close()
        for conn in accepted:
            conn.close()


def test_shutdown_does_not_wait_behind_a_slow_connect(silent_listener: int) -> None:
    exporter = GRPCExporter(f"127.0.0.1:{silent_listener}", service="svc", timeout=3.0)
    errors: list[BaseException] = []

    def export() -> None:
        try:
            exporter.export([_span()])
        except ExportError as exc:
            errors.append(exc)

    thread = threading.Thread(target=export, daemon=True)
    thread.start()
    time.sleep(0.2)  # let the export reach the TLS handshake
    started = time.monotonic()
    exporter.shutdown(timeout=1.0)
    assert time.monotonic() - started < 1.0
    thread.join(10)
    assert not thread.is_alive()
    assert exporter._conn is None  # a connect finishing after shutdown is never published


def test_shutdown_honours_its_timeout_when_close_is_slow(server: H2TestServer) -> None:
    exporter = GRPCExporter(server.endpoint, service="svc", insecure=True, timeout=5.0)
    exporter.export([_span()])
    conn = exporter._conn
    assert conn is not None
    release = threading.Event()
    real_close = conn.close

    def slow_close() -> None:
        release.wait(5)
        real_close()

    conn.close = slow_close  # type: ignore[method-assign]
    started = time.monotonic()
    exporter.shutdown(timeout=0.2)
    assert time.monotonic() - started < 2.0
    release.set()


def test_after_fork_replaces_both_locks(server: H2TestServer) -> None:
    exporter = GRPCExporter(server.endpoint, service="svc", insecure=True)
    lock, connect_lock = exporter._lock, exporter._connect_lock
    exporter._after_fork_in_child()
    assert exporter._lock is not lock
    assert exporter._connect_lock is not connect_lock
    assert exporter._conn is None


def test_replace_resource_is_used_by_later_exports(server: H2TestServer) -> None:
    exporter = GRPCExporter(
        server.endpoint, service="svc", insecure=True, resource={"process.pid": 1}
    )
    exporter._replace_resource({"process.pid": 2})
    try:
        exporter.export([_span()])
    finally:
        exporter.shutdown()
    assert b"process.pid" in server.requests[0].body
    assert exporter._resource == {"process.pid": 2}
