"""``H2Connection`` against a scripted in-process h2c server (component tests).

The server (``_h2server``) has its own codec, records every frame, enforces
flow control on what it receives and honours the client's windows when it
sends. Every socket is on 127.0.0.1:0.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from pyrock.trace.otlp._h2 import H2Connection, H2Error, H2Response, StreamReset
from tests.unit.trace_otlp import _h2server as srv
from tests.unit.trace_otlp._h2server import H2TestServer, Request, ServerConn

GRPC_HEADERS = [("content-type", "application/grpc"), ("te", "trailers")]
PATH = "/test.Service/Method"
EMPTY_GRPC = b"\x00\x00\x00\x00\x00"
TIMEOUT = 5.0


@pytest.fixture
def server() -> Iterator[H2TestServer]:
    with H2TestServer() as s:
        yield s


def _connect(server: H2TestServer) -> H2Connection:
    return H2Connection(server.host, server.port)


def _call(conn: H2Connection, body: bytes = b"\x00\x00\x00\x00\x03abc") -> H2Response:
    return conn.request("POST", PATH, GRPC_HEADERS, body, timeout=TIMEOUT)


def _with_server(handler: srv.Handler, **kwargs: object) -> H2TestServer:
    return H2TestServer(handler, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------- basic call


def test_unary_request_round_trip(server: H2TestServer) -> None:
    conn = _connect(server)
    try:
        response = _call(conn)
    finally:
        conn.close()
    assert response.status == 200
    assert response.header("content-type") == "application/grpc"
    assert response.body == EMPTY_GRPC
    assert ("grpc-status", "0") in list(response.trailers)
    assert response.header("grpc-status") == "0"

    (request,) = server.requests
    assert request.stream_id == 1
    assert request.body == b"\x00\x00\x00\x00\x03abc"
    pseudo = [(k, v) for k, v in request.headers if k.startswith(":")]
    assert dict(pseudo) == {
        ":method": "POST",
        ":scheme": "http",
        ":path": PATH,
        ":authority": f"127.0.0.1:{server.port}",
    }
    names = [k for k, _ in request.headers]
    assert names[: len(pseudo)] == [k for k, _ in pseudo], "pseudo-headers must come first"
    assert request.header("content-type") == "application/grpc"
    assert request.header("te") == "trailers"


def test_connection_starts_with_preface_and_settings(server: H2TestServer) -> None:
    conn = _connect(server)
    try:
        _call(conn)
        server.wait(lambda: server.conn().settings_acked)
    finally:
        conn.close()
    first_type, first_flags, first_stream, _ = server.conn().frames[0]
    assert (first_type, first_flags & srv.ACK, first_stream) == (srv.SETTINGS, 0, 0)
    assert server.conn().settings_acked is True
    assert server.errors == []


def test_header_names_sent_lowercase(server: H2TestServer) -> None:
    conn = _connect(server)
    try:
        conn.request("POST", PATH, [("content-type", "application/grpc"), ("x-api-key", "k")], b"")
    finally:
        conn.close()
    (request,) = server.requests
    assert all(name == name.lower() for name, _ in request.headers)
    assert request.header("x-api-key") == "k"


def test_empty_body_ends_stream(server: H2TestServer) -> None:
    conn = _connect(server)
    try:
        response = _call(conn, b"")
    finally:
        conn.close()
    assert response.status == 200
    (request,) = server.requests
    assert request.body == b""
    assert request.complete is True


def test_sequential_requests_reuse_connection_and_hpack_state(server: H2TestServer) -> None:
    conn = _connect(server)
    try:
        responses = [_call(conn) for _ in range(3)]
    finally:
        conn.close()
    assert len(server.connections) == 1
    assert [r.stream_id for r in server.requests] == [1, 3, 5]
    # The 2nd and 3rd responses refer to content-type through the dynamic table.
    assert [r.header("content-type") for r in responses] == ["application/grpc"] * 3


def test_concurrent_requests_on_one_connection(server: H2TestServer) -> None:
    conn = _connect(server)
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def work() -> None:
        barrier.wait()
        for _ in range(5):
            try:
                status = _call(conn).status
            except BaseException as exc:
                errors.append(exc)
                return
            with lock:
                results.append(status)

    threads = [threading.Thread(target=work) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        conn.close()
    assert errors == []
    assert results == [200] * 20
    ids = [r.stream_id for r in server.requests]
    assert len(set(ids)) == 20
    assert all(i % 2 == 1 for i in ids)


def test_non_200_status_is_returned_not_raised() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        conn.send_headers(request.stream_id, srv.status_block(503), end_stream=True)

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            response = _call(conn)
        finally:
            conn.close()
    assert response.status == 503
    assert response.body == b""


# ----------------------------------------------------- headers / padding


def test_response_headers_split_across_continuation_frames() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        conn.send_headers(request.stream_id, conn.response_headers(), split=3)
        conn.send_data(request.stream_id, EMPTY_GRPC)
        trailers = srv.lit("grpc-status", "14") + srv.lit_huffman_value(
            "grpc-message", bytes.fromhex("a8eb10649cbf")
        )
        conn.send_headers(request.stream_id, trailers, end_stream=True, split=4)

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            response = _call(conn)
        finally:
            conn.close()
    assert response.header("content-type") == "application/grpc"
    assert list(response.trailers) == [("grpc-status", "14"), ("grpc-message", "no-cache")]


def test_padded_data_and_headers_with_priority() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        conn.send_headers(request.stream_id, conn.response_headers(), pad=7, priority=True)
        conn.send_data(request.stream_id, b"\x00\x00\x00\x00\x02hi", pad=9)
        conn.send_headers(request.stream_id, srv.lit("grpc-status", "0"), end_stream=True, pad=1)

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            response = _call(conn)
        finally:
            conn.close()
    assert response.status == 200
    assert response.body == b"\x00\x00\x00\x00\x02hi"
    assert response.header("grpc-status") == "0"


def test_request_header_block_uses_continuation_when_large(server: H2TestServer) -> None:
    big = "v" * 40_000
    conn = _connect(server)
    try:
        conn.request("POST", PATH, [*GRPC_HEADERS, ("x-big", big)], EMPTY_GRPC, timeout=TIMEOUT)
    finally:
        conn.close()
    (request,) = server.requests
    assert request.header("x-big") == big
    types = [t for t, _, _ in request.header_frames]
    assert types[0] == srv.HEADERS
    assert set(types[1:]) == {srv.CONTINUATION}
    assert all(length <= 16384 for _, _, length in request.header_frames)
    flags = [f for _, f, _ in request.header_frames]
    assert all(not f & srv.END_HEADERS for f in flags[:-1])
    assert flags[-1] & srv.END_HEADERS


def test_unknown_frame_types_are_ignored() -> None:
    def on_connect(conn: ServerConn) -> None:
        conn.send(0xEE, 0, 0, b"ignore me")
        conn.send(0xEF, 0, 1, b"")

    with H2TestServer(on_connect=on_connect) as server:
        conn = _connect(server)
        try:
            assert _call(conn).status == 200
        finally:
            conn.close()


# --------------------------------------------------------- flow control


def test_client_respects_small_server_stream_window() -> None:
    with H2TestServer(settings=[(srv.S_INITIAL_WINDOW_SIZE, 10)]) as server:
        conn = _connect(server)
        try:
            conn.ping(timeout=TIMEOUT)  # the client has now applied the server's SETTINGS
            body = bytes(range(100))
            response = _call(conn, body)
        finally:
            conn.close()
        (request,) = server.requests
    assert response.status == 200
    assert server.conn().flow_violations == []
    assert request.body == body
    assert max(request.data_frames) <= 10
    assert len(request.data_frames) >= 10


def test_client_waits_for_window_update_before_sending_more() -> None:
    with H2TestServer(
        settings=[(srv.S_INITIAL_WINDOW_SIZE, 0)], auto_window_update=False
    ) as server:
        conn = _connect(server)
        try:
            conn.ping(timeout=TIMEOUT)
            outcome: list[object] = []
            thread = threading.Thread(target=lambda: outcome.append(_call(conn, b"x" * 30)))
            thread.start()
            server.wait(lambda: len(server.requests) == 1 and bool(server.requests[0].headers))
            live = server.connections[0]
            assert server.requests[0].data_frames == []
            # Grant the stream 30 bytes; connection window is still the default.
            live.stream_recv_windows[1] += 30
            live.send(srv.WINDOW_UPDATE, 0, 1, (30).to_bytes(4, "big"))
            thread.join(TIMEOUT)
            assert not thread.is_alive()
        finally:
            conn.close()
    (response,) = outcome
    assert isinstance(response, H2Response)
    assert server.requests[0].body == b"x" * 30
    assert server.conn().flow_violations == []


def test_large_request_respects_connection_window_and_frame_size(server: H2TestServer) -> None:
    body = bytes(i % 251 for i in range(200_000))
    conn = _connect(server)
    try:
        _call(conn, body)
    finally:
        conn.close()
    (request,) = server.requests
    assert request.body == body
    assert server.conn().flow_violations == []
    assert max(request.data_frames) <= 16384


def test_large_response_requires_client_window_updates() -> None:
    payload = bytes(i % 253 for i in range(300_000))

    def handler(conn: ServerConn, request: Request) -> None:
        conn.send_headers(request.stream_id, conn.response_headers())
        conn.send_data(request.stream_id, payload)
        conn.send_headers(request.stream_id, srv.lit("grpc-status", "0"), end_stream=True)

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            response = _call(conn)
        finally:
            conn.close()
    assert response.body == payload
    increments = server.conn().window_updates
    assert any(stream == 0 for stream, _ in increments)


# ------------------------------------------------------------ ping/goaway


def test_ping_round_trip(server: H2TestServer) -> None:
    conn = _connect(server)
    try:
        conn.ping(timeout=TIMEOUT)
    finally:
        conn.close()
    (payload,) = server.conn().pings
    assert len(payload) == 8


def test_client_acks_server_ping() -> None:
    def on_connect(conn: ServerConn) -> None:
        conn.send(srv.PING, 0, 0, b"pyrock!!")

    with H2TestServer(on_connect=on_connect) as server:
        conn = _connect(server)
        try:
            _call(conn)
            server.wait(lambda: server.conn().ping_acks == [b"pyrock!!"])
        finally:
            conn.close()


def test_close_sends_goaway_and_is_idempotent(server: H2TestServer) -> None:
    conn = _connect(server)
    _call(conn)
    assert conn.closed is False
    conn.close()
    assert conn.closed is True
    conn.close()
    server.wait_eof()
    goaway = server.conn().goaway
    assert goaway is not None
    assert goaway[1] == 0  # NO_ERROR


def test_request_after_close_raises(server: H2TestServer) -> None:
    conn = _connect(server)
    conn.close()
    with pytest.raises(H2Error):
        _call(conn)


def test_server_goaway_fails_request() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        conn.send(srv.GOAWAY, 0, 0, (0).to_bytes(4, "big") + (2).to_bytes(4, "big") + b"bye")

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            with pytest.raises(H2Error):
                _call(conn)
        finally:
            conn.close()


def test_rst_stream_raises_stream_reset_and_connection_survives() -> None:
    calls = {"n": 0}

    def handler(conn: ServerConn, request: Request) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            conn.send(srv.RST_STREAM, 0, request.stream_id, (7).to_bytes(4, "big"))
        else:
            conn.respond_grpc(request.stream_id)

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            with pytest.raises(StreamReset) as info:
                _call(conn)
            assert info.value.error_code == 7
            assert _call(conn).status == 200
        finally:
            conn.close()


def test_server_disconnect_mid_request_raises() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        conn.send_headers(request.stream_id, conn.response_headers())
        conn.sock.shutdown(socket.SHUT_RDWR)

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            with pytest.raises(H2Error):
                _call(conn)
        finally:
            conn.close()


def test_request_timeout_raises() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        return  # never respond

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            with pytest.raises(H2Error):
                conn.request("POST", PATH, GRPC_HEADERS, EMPTY_GRPC, timeout=0.3)
        finally:
            conn.close()


def test_connection_refused_raises_h2_error() -> None:
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    def call() -> None:
        conn = H2Connection("127.0.0.1", port, connect_timeout=2.0)
        try:
            conn.request("POST", PATH, GRPC_HEADERS, EMPTY_GRPC, timeout=2.0)
        finally:
            conn.close()

    with pytest.raises(H2Error):
        call()


def test_garbage_from_server_is_an_error() -> None:
    def handler(conn: ServerConn, request: Request) -> None:
        # A SETTINGS frame on a non-zero stream is a connection error.
        conn.send(srv.SETTINGS, 0, request.stream_id, b"")

    with _with_server(handler) as server:
        conn = _connect(server)
        try:
            with pytest.raises(H2Error):
                _call(conn)
        finally:
            conn.close()
