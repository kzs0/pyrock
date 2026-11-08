"""Phase 4 review fixes in ``pyrock.server``.

* P0-1: the write deadline starts after the handler, so a handler slower than
  ``write_timeout`` (a pprof CPU profile) still gets its response out.
* P1-12: ``server.Config`` raises ``ConfigError`` (a ``ValueError``).
* ``Expect: 100-continue`` gets an interim ``100 Continue`` before the body.
* A second ``start_response`` without ``exc_info`` is an application error.
"""

from __future__ import annotations

import gzip
import http.client
import socket
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest

from pyrock import ConfigError, server
from pyrock._types import StartResponse, WSGIApp, WSGIEnviron
from pyrock.metric import Registry
from pyrock.profile import debug
from tests.unit.profile._proto import decode_pprof


@pytest.fixture
def serve() -> Iterator[Callable[..., tuple[str, int]]]:
    started: list[server.Server] = []

    def start(**overrides: Any) -> tuple[str, int]:
        cfg = server.Config(addr="127.0.0.1:0", read_header_timeout=2.0, **overrides)
        srv = server.Server(Registry(), cfg)
        srv.start()
        started.append(srv)
        address = srv.address
        assert address is not None
        return address

    yield start
    for srv in started:
        srv.shutdown(timeout=5)


def get(addr: tuple[str, int], path: str, *, timeout: float = 10.0) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(*addr, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# ---------------------------------------------------------------- P0-1


def test_a_handler_slower_than_write_timeout_still_answers(
    serve: Callable[..., tuple[str, int]],
) -> None:
    def slow(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        threading.Event().wait(0.6)  # longer than write_timeout: the work itself takes time
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"done"]

    addr = serve(pprof=True, profile_handler=slow, write_timeout=0.3)
    assert get(addr, "/debug/pprof/profile") == (200, b"done")


def test_cpu_profile_longer_than_write_timeout_is_delivered(
    serve: Callable[..., tuple[str, int]],
) -> None:
    """The ``go tool pprof`` default shape: ``seconds`` == ``max_seconds`` > ``write_timeout``."""
    addr = serve(pprof=True, profile_handler=debug.app(max_seconds=0.6), write_timeout=0.3)
    status, body = get(addr, "/debug/pprof/profile")  # default seconds = max_seconds
    assert status == 200
    profile = decode_pprof(gzip.decompress(body))
    assert profile.sample_types == [("samples", "count"), ("cpu", "nanoseconds")]


# ---------------------------------------------------------------- P1-12


@pytest.mark.parametrize(
    "overrides",
    [
        {"read_timeout": 0},
        {"max_header_bytes": 1.5},
        {"max_connections": True},
        {"addr": 9090},
    ],
)
def test_config_raises_config_error(overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigError) as info:
        server.Config(**overrides)
    assert isinstance(info.value, ValueError)
    assert len(info.value.problems) == 1
    assert info.value.problems[0].startswith("server.Config: ")


def test_config_error_lists_every_problem() -> None:
    with pytest.raises(ConfigError) as info:
        server.Config(read_timeout=-1, max_connections=0, addr="nope")
    assert len(info.value.problems) == 3


# ---------------------------------------------------------------- 100 Continue


def _read_response_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def test_expect_100_continue_gets_an_interim_response(
    serve: Callable[..., tuple[str, int]],
) -> None:
    addr = serve()
    with socket.create_connection(addr, timeout=5) as sock:
        sock.sendall(
            b"POST /health HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n"
            b"Expect: 100-continue\r\nConnection: close\r\n\r\n"
        )
        interim = _read_response_head(sock)  # the server must answer before the body arrives
        assert interim.startswith(b"HTTP/1.1 100 Continue\r\n\r\n")
        rest = interim[len(b"HTTP/1.1 100 Continue\r\n\r\n") :]
        sock.sendall(b"body")
        final = rest + _read_response_head(sock)
    assert final.startswith(b"HTTP/1.1 200 ")


def test_expect_100_continue_is_not_sent_for_an_oversized_body(
    serve: Callable[..., tuple[str, int]],
) -> None:
    addr = serve()
    with socket.create_connection(addr, timeout=5) as sock:
        sock.sendall(
            b"POST /health HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        assert _read_response_head(sock).startswith(b"HTTP/1.1 413 ")


@pytest.mark.parametrize(
    ("version", "expect", "expected"),
    [
        ("HTTP/1.1", "100-continue", True),
        ("HTTP/1.1", " 100-Continue ", True),
        ("HTTP/1.0", "100-continue", False),
        ("HTTP/1.1", None, False),
        ("HTTP/1.1", "something-else", False),
    ],
)
def test_expects_continue(version: str, expect: str | None, expected: bool) -> None:
    headers = [("host", "x")] + ([("expect", expect)] if expect is not None else [])
    request = server._Request("POST", "/", "", version, headers)
    assert request.expects_continue is expected


# ---------------------------------------------------------------- start_response


def _run(app: WSGIApp) -> server._Response:
    environ: WSGIEnviron = {"REQUEST_METHOD": "GET", "PATH_INFO": "/"}
    return server._run_app(app, environ)


def test_second_start_response_without_exc_info_fails() -> None:
    def app(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [])
        start_response("201 Created", [])
        return [b""]

    with pytest.raises(RuntimeError, match="start_response"):
        _run(app)


def test_second_start_response_with_exc_info_replaces_the_first() -> None:
    def app(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        write = start_response("200 OK", [("X-A", "1")])
        write(b"partial")
        try:
            raise KeyError("boom")
        except KeyError:
            import sys

            start_response("503 Service Unavailable", [("X-B", "2")], sys.exc_info())
        return [b"error page"]

    response = _run(app)
    assert (response.status, response.headers, response.body) == (
        "503 Service Unavailable",
        [("X-B", "2")],
        b"error page",
    )


def test_double_start_response_is_answered_500(serve: Callable[..., tuple[str, int]]) -> None:
    def app(environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        start_response("200 OK", [])
        start_response("200 OK", [])
        return [b"never"]

    addr = serve(pprof=True, profile_handler=app)
    assert get(addr, "/debug/pprof/x")[0] == 500
