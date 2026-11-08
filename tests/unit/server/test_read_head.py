"""The deadline-based request-head reader behind the server's Slowloris guard.

``server._read_head`` is a private seam: it reads the request line and headers
from a socket within one total deadline. These tests drive it over a local
socket pair, with real time for short deadlines and a fake clock to show the
deadline is recomputed before every ``recv``.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from pyrock import server

HEAD = b"GET /health HTTP/1.1\r\nHost: x\r\nX-A: 1\r\n\r\n"


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    """``(server_side, client_side)`` connected sockets."""
    a, b = socket.socketpair()
    try:
        yield a, b
    finally:
        a.close()
        b.close()


def test_reads_a_complete_head(pair: tuple[socket.socket, socket.socket]) -> None:
    srv, client = pair
    client.sendall(HEAD)
    head, rest = server._read_head(srv, timeout=2.0, max_bytes=4096)
    assert head == HEAD
    assert rest == b""


def test_bytes_after_the_head_are_returned_as_rest(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, client = pair
    client.sendall(HEAD + b"GET /ready HTTP/1.1\r\n")
    head, rest = server._read_head(srv, timeout=2.0, max_bytes=4096)
    assert head == HEAD
    assert rest == b"GET /ready HTTP/1.1\r\n"


def test_head_split_across_many_sends(pair: tuple[socket.socket, socket.socket]) -> None:
    srv, client = pair

    def send() -> None:
        for i in range(0, len(HEAD), 3):
            client.sendall(HEAD[i : i + 3])

    sender = threading.Thread(target=send)
    sender.start()
    try:
        head, _ = server._read_head(srv, timeout=5.0, max_bytes=4096)
    finally:
        sender.join(5)
    assert head == HEAD


def test_peer_closing_before_any_byte_is_a_clean_end(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, client = pair
    client.close()
    assert server._read_head(srv, timeout=2.0, max_bytes=4096) == (b"", b"")


def test_peer_closing_mid_head_is_a_connection_error(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, client = pair
    client.sendall(b"GET /health HTTP/1.1\r\nHost:")
    client.close()
    with pytest.raises(ConnectionError):
        server._read_head(srv, timeout=2.0, max_bytes=4096)


def test_silent_peer_times_out_at_the_deadline(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, _client = pair
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        server._read_head(srv, timeout=0.2, max_bytes=4096)
    assert 0.15 <= time.monotonic() - started < 2.0


def test_slow_drip_cannot_extend_the_deadline(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, client = pair
    stop = threading.Event()

    def drip() -> None:
        with contextlib.suppress(OSError):
            client.sendall(b"GET /health HTTP/1.1\r\nX-Drip: ")
            while not stop.is_set():
                client.sendall(b"a")
                stop.wait(0.02)  # far below the deadline: a per-recv timeout never fires

    dripper = threading.Thread(target=drip)
    dripper.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            server._read_head(srv, timeout=0.3, max_bytes=1 << 20)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        dripper.join(5)
    assert 0.25 <= elapsed < 2.0


def test_deadline_is_recomputed_from_the_clock_before_each_recv(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, client = pair
    client.sendall(b"GET /health HTTP/1.1\r\n")  # a partial head is already waiting
    readings = iter([100.0])

    def clock() -> float:
        # The first reading fixes the deadline; every later one is past it.
        return next(readings, 200.0)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        server._read_head(srv, timeout=5.0, max_bytes=4096, clock=clock)
    assert time.monotonic() - started < 1.0, "the reader waited on the socket past the deadline"


def test_over_the_limit_without_terminator_is_too_large(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, client = pair
    client.sendall(b"GET /health HTTP/1.1\r\nX-Big: " + b"a" * 300)
    with pytest.raises(server._HeaderTooLarge):
        server._read_head(srv, timeout=2.0, max_bytes=100)


def test_the_limit_counts_the_request_line(pair: tuple[socket.socket, socket.socket]) -> None:
    srv, client = pair
    client.sendall(b"GET /" + b"p" * 200 + b" HTTP/1.1\r\n\r\n")
    with pytest.raises(server._HeaderTooLarge):
        server._read_head(srv, timeout=2.0, max_bytes=100)


def test_head_within_the_limit_is_read(pair: tuple[socket.socket, socket.socket]) -> None:
    srv, client = pair
    client.sendall(HEAD)
    head, _ = server._read_head(srv, timeout=2.0, max_bytes=len(HEAD) + 10)
    assert head == HEAD


def test_too_large_is_reported_without_waiting_for_more(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    srv, client = pair
    client.sendall(b"X" * 500)  # the peer then goes quiet
    started = time.monotonic()
    with pytest.raises(server._HeaderTooLarge):
        server._read_head(srv, timeout=5.0, max_bytes=100)
    assert time.monotonic() - started < 1.0


def test_header_too_large_is_an_exception_type() -> None:
    assert issubclass(server._HeaderTooLarge, Exception)
    assert not issubclass(server._HeaderTooLarge, TimeoutError)
