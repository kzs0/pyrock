"""``pyrock.server.Server`` over real loopback sockets.

Complements ``tests/e2e/test_server.py`` with finer-grained contracts: the
request/connection lifecycle (keep-alive, idle timeout, malformed requests),
the pprof hand-off environ, connection-cap responses and shutdown edges.
Every server binds ``127.0.0.1:0`` with short timeouts.
"""

from __future__ import annotations

import contextlib
import http.client
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest

from pyrock import server
from pyrock.metric import Bucket, MetricFamily, MetricType, Registry, Sample
from tests.support.collectors import wait_until
from tests.support.prom import parse

# ---------------------------------------------------------------- helpers


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def request(
    addr: tuple[str, int],
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> Response:
    conn = http.client.HTTPConnection(*addr, timeout=timeout)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
    finally:
        conn.close()


def read_until_closed(sock: socket.socket, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except (ConnectionResetError, BrokenPipeError):
        pass
    except TimeoutError:
        raise AssertionError(f"server kept the connection open; got {data!r}") from None
    return data


def status_of(raw: bytes) -> int | None:
    if not raw.startswith(b"HTTP/"):
        return None
    return int(raw.split(b" ", 2)[1])


class ResponseReader:
    """Reads consecutive Content-Length responses from one kept-alive socket."""

    def __init__(self, sock: socket.socket, timeout: float = 5.0) -> None:
        sock.settimeout(timeout)
        self._file = sock.makefile("rb")

    def read(self) -> tuple[int, bytes]:
        status_line = self._file.readline()
        if not status_line:
            raise AssertionError("connection closed before a response")
        status = int(status_line.split(b" ", 2)[1])
        length = 0
        while (line := self._file.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        return status, self._file.read(length)

    def close(self) -> None:
        self._file.close()


def read_one_response(sock: socket.socket, timeout: float = 5.0) -> tuple[int, bytes]:
    """Read exactly one response (for sockets that see one response at a time)."""
    reader = ResponseReader(sock, timeout)
    try:
        return reader.read()
    finally:
        reader.close()


@contextlib.contextmanager
def running(registry: Registry | None = None, **config: Any) -> Iterator[server.Server]:
    srv = server.Server(
        registry if registry is not None else Registry(),
        server.Config(addr="127.0.0.1:0", **config),
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.shutdown(timeout=5)


def address(srv: server.Server) -> tuple[str, int]:
    addr = srv.address
    assert addr is not None, "server is not listening"
    return addr


class Blocking:
    """A WSGI profile handler that blocks until released."""

    def __init__(self) -> None:
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        self.entered.release()
        self.release.wait(10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"done"]


class BadRegistry(Registry):
    """A registry whose snapshot is not valid exposition (decreasing histogram buckets)."""

    def collect(self) -> list[MetricFamily]:
        bad = Sample(labels={}, buckets=(Bucket(1.0, 5), Bucket(0.5, 3)), count=2, sum=1.0)
        good = Sample(labels={}, value=1.0)
        return [
            MetricFamily("a_ok_total", "fine", MetricType.COUNTER, (good,)),
            MetricFamily("bad_hist", "broken", MetricType.HISTOGRAM, (bad,)),
        ]


# ---------------------------------------------------------------- lifecycle


def test_address_is_none_until_started() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0"))
    assert srv.address is None
    assert srv.config == server.Config(addr="127.0.0.1:0")
    srv.start()
    try:
        host, port = address(srv)
    finally:
        srv.shutdown(timeout=5)
    assert host == "127.0.0.1"
    assert port > 0


def test_default_config_when_none_given() -> None:
    assert server.Server(Registry()).config == server.Config()


def test_start_on_a_busy_port_raises_oserror() -> None:
    with running() as first:
        _, port = address(first)
        second = server.Server(Registry(), server.Config(addr=f"127.0.0.1:{port}"))
        with pytest.raises(OSError):  # noqa: PT011 - any bind failure
            second.start()
        second.shutdown(timeout=1)


def test_shutdown_before_start_is_harmless() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0"))
    srv.shutdown(timeout=1)
    srv.shutdown(timeout=1)
    assert srv.address is None


def test_shutdown_closes_the_listener_and_is_idempotent() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0"))
    srv.start()
    addr = address(srv)
    srv.shutdown(timeout=5)
    srv.shutdown(timeout=5)
    # Windows retries a refused SYN for about two seconds before reporting
    # ConnectionRefusedError, so the connect timeout must outlast that.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(addr, timeout=5).close()


def test_shutdown_without_in_flight_requests_is_quick() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0", shutdown_timeout=10))
    srv.start()
    assert request(address(srv), "/health").status == 200
    started = time.monotonic()
    srv.shutdown()
    assert time.monotonic() - started < 2.0


def test_shutdown_default_timeout_comes_from_config() -> None:
    blocker = Blocking()
    srv = server.Server(
        Registry(),
        server.Config(
            addr="127.0.0.1:0", pprof=True, profile_handler=blocker, shutdown_timeout=0.3
        ),
    )
    srv.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pending = pool.submit(request, address(srv), "/debug/pprof/heap")
            assert blocker.entered.acquire(timeout=5)
            started = time.monotonic()
            srv.shutdown()
            elapsed = time.monotonic() - started
        finally:
            blocker.release.set()
        with contextlib.suppress(Exception):
            pending.result(timeout=5)
    assert 0.2 <= elapsed < 2.0


def test_serving_threads_do_not_leak() -> None:
    before = {t for t in threading.enumerate() if t.is_alive()}
    with running() as srv:
        for _ in range(3):
            assert request(address(srv), "/health").status == 200
    wait_until(lambda: not ({t for t in threading.enumerate() if t.is_alive()} - before))


# ---------------------------------------------------------------- routing


def test_health_ready_and_metrics() -> None:
    registry = Registry()
    registry.counter("unit_hits_total", "Hits").inc(2)
    with running(registry) as srv:
        addr = address(srv)
        for path in ("/health", "/ready"):
            resp = request(addr, path)
            assert resp.status == 200
            assert resp.body.strip() == b"ok"
        metrics = request(addr, "/metrics")
    assert metrics.status == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    assert parse(metrics.body).value("unit_hits_total") == 2
    assert int(metrics.headers["content-length"]) == len(metrics.body)


def test_query_strings_do_not_change_routing() -> None:
    with running() as srv:
        addr = address(srv)
        assert request(addr, "/health?verbose=1").status == 200
        assert request(addr, "/metrics?name[]=x").status == 200


@pytest.mark.parametrize(
    "path", ["/", "/healthz", "/health/", "/metrics/x", "/debug/pprof/heap", "/READY"]
)
def test_unknown_paths_are_404(path: str) -> None:
    with running() as srv:
        assert request(address(srv), path).status == 404


def test_metrics_disabled_is_404() -> None:
    with running(metrics=False) as srv:
        assert request(address(srv), "/metrics").status == 404
        assert request(address(srv), "/health").status == 200


def test_metrics_exposition_error_is_500_with_no_partial_body() -> None:
    with running(BadRegistry()) as srv:
        resp = request(address(srv), "/metrics")
        assert request(address(srv), "/health").status == 200  # still serving
    assert resp.status == 500
    assert b"a_ok_total" not in resp.body
    assert b"bad_hist" not in resp.body


def test_metrics_reflect_the_registry_at_request_time() -> None:
    registry = Registry()
    gauge = registry.gauge("unit_depth", "Depth")
    with running(registry) as srv:
        gauge.set(1)
        first = parse(request(address(srv), "/metrics").body).value("unit_depth")
        gauge.set(5)
        second = parse(request(address(srv), "/metrics").body).value("unit_depth")
    assert (first, second) == (1, 5)


# ---------------------------------------------------------------- pprof hand-off


def test_pprof_handler_receives_a_wsgi_environ() -> None:
    seen: list[dict[str, Any]] = []

    def handler(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        seen.append(dict(environ))
        start_response("200 OK", [("Content-Type", "text/plain"), ("X-From", "handler")])
        return [b"from-", b"handler"]

    with running(pprof=True, profile_handler=handler) as srv:
        resp = request(address(srv), "/debug/pprof/profile?seconds=1")
    assert resp.status == 200
    assert resp.body == b"from-handler"
    assert resp.headers["x-from"] == "handler"
    environ = seen[0]
    assert environ["REQUEST_METHOD"] == "GET"
    assert environ["PATH_INFO"] == "/debug/pprof/profile"
    assert environ["QUERY_STRING"] == "seconds=1"
    assert environ.get("SCRIPT_NAME", "") == ""
    assert "wsgi.input" in environ
    assert environ["SERVER_PROTOCOL"].startswith("HTTP/")


def test_pprof_handler_status_and_method_pass_through() -> None:
    def handler(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        if environ["REQUEST_METHOD"] != "GET":
            start_response("405 Method Not Allowed", [("Allow", "GET")])
            return [b""]
        start_response("200 OK", [])
        return [b"ok"]

    with running(pprof=True, profile_handler=handler) as srv:
        resp = request(address(srv), "/debug/pprof/heap", method="POST")
    assert resp.status == 405
    assert resp.headers.get("allow") == "GET"


def test_pprof_handler_exception_is_500_and_server_keeps_serving() -> None:
    def handler(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        raise RuntimeError("handler exploded")

    with running(pprof=True, profile_handler=handler) as srv:
        addr = address(srv)
        resp = request(addr, "/debug/pprof/heap")
        assert request(addr, "/health").status == 200
    assert resp.status == 500
    assert b"exploded" not in resp.body


@pytest.mark.parametrize("path", ["/debug/pprofx", "/debug/pprof", "/debug"])
def test_only_the_pprof_subtree_reaches_the_handler(path: str) -> None:
    calls: list[str] = []

    def handler(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        calls.append(environ["PATH_INFO"])
        start_response("200 OK", [])
        return [b""]

    with running(pprof=True, profile_handler=handler) as srv:
        assert request(address(srv), path).status == 404
    assert calls == []


# ---------------------------------------------------------------- connections


def test_keep_alive_serves_several_requests_on_one_connection() -> None:
    with running() as srv, socket.create_connection(address(srv), timeout=5) as sock:
        for path in ("/health", "/ready", "/health"):
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            status, body = read_one_response(sock)
            assert status == 200
            assert body.strip() == b"ok"


def test_pipelined_requests_are_answered_in_order() -> None:
    with running() as srv, socket.create_connection(address(srv), timeout=5) as sock:
        sock.sendall(
            b"GET /health HTTP/1.1\r\nHost: x\r\n\r\nGET /nope HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        reader = ResponseReader(sock)
        try:
            assert reader.read()[0] == 200
            assert reader.read()[0] == 404
        finally:
            reader.close()


def test_connection_close_is_honoured() -> None:
    with running() as srv, socket.create_connection(address(srv), timeout=5) as sock:
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = read_until_closed(sock, timeout=3)
    assert status_of(data) == 200


def test_idle_connection_is_closed_after_idle_timeout() -> None:
    with (
        running(idle_timeout=0.3) as srv,
        socket.create_connection(address(srv), timeout=5) as sock,
    ):
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        assert read_one_response(sock)[0] == 200
        started = time.monotonic()
        assert read_until_closed(sock, timeout=3) == b""
        elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 2.0


def test_request_within_idle_timeout_is_served() -> None:
    with (
        running(idle_timeout=2.0) as srv,
        socket.create_connection(address(srv), timeout=5) as sock,
    ):
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        assert read_one_response(sock)[0] == 200
        threading.Event().wait(0.2)
        sock.sendall(b"GET /ready HTTP/1.1\r\nHost: x\r\n\r\n")
        assert read_one_response(sock)[0] == 200


def test_silent_connection_is_cut_at_the_header_deadline() -> None:
    with (
        running(read_header_timeout=0.3) as srv,
        socket.create_connection(address(srv), timeout=5) as sock,
    ):
        sock.sendall(b"GET /health HTTP/1.1\r\n")  # never finishes the head
        started = time.monotonic()
        data = read_until_closed(sock, timeout=4)
        elapsed = time.monotonic() - started
    assert status_of(data) != 200
    assert 0.2 <= elapsed < 3.0


def test_request_line_drip_is_cut_at_the_header_deadline() -> None:
    with running(read_header_timeout=0.4) as srv:
        sock = socket.create_connection(address(srv), timeout=5)
        stop = threading.Event()

        def drip() -> None:
            with contextlib.suppress(OSError):
                for byte in b"GET /health-" + b"x" * 1000:
                    if stop.is_set():
                        return
                    sock.sendall(bytes([byte]))
                    stop.wait(0.03)

        dripper = threading.Thread(target=drip, daemon=True)
        started = time.monotonic()
        dripper.start()
        try:
            data = read_until_closed(sock, timeout=4)
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            sock.close()
        dripper.join(2)
    assert status_of(data) != 200
    assert 0.3 <= elapsed < 3.0


def test_header_over_limit_is_431_then_closed() -> None:
    with (
        running(max_header_bytes=512) as srv,
        socket.create_connection(address(srv), timeout=5) as sock,
    ):
        with contextlib.suppress(OSError):
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nX-Big: " + b"a" * 4096 + b"\r\n\r\n")
        data = read_until_closed(sock, timeout=4)
    assert status_of(data) == 431


def test_many_small_headers_over_limit_are_431() -> None:
    headers = "".join(f"X-H{i}: {'v' * 20}\r\n" for i in range(100))
    with (
        running(max_header_bytes=1024) as srv,
        socket.create_connection(address(srv), timeout=5) as sock,
    ):
        with contextlib.suppress(OSError):
            sock.sendall(f"GET /health HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode())
        data = read_until_closed(sock, timeout=4)
    assert status_of(data) == 431


def test_headers_under_limit_are_served() -> None:
    with running(max_header_bytes=1024) as srv:
        assert request(address(srv), "/health", headers={"X-Small": "a" * 300}).status == 200


@pytest.mark.parametrize(
    "raw",
    [b"NONSENSE\r\n\r\n", b"GET\r\n\r\n", b"GET /health HTTP/9.9\r\n\r\n", b"\x00\x01\x02\r\n\r\n"],
    ids=["no-target", "method-only", "bad-version", "binary"],
)
def test_malformed_requests_get_4xx_or_5xx_and_close(raw: bytes) -> None:
    with running() as srv, socket.create_connection(address(srv), timeout=5) as sock:
        sock.sendall(raw)
        data = read_until_closed(sock, timeout=4)
        assert request(address(srv), "/health").status == 200
    status = status_of(data)
    assert status is not None
    assert status >= 400


def test_connection_cap_rejects_with_503_and_close() -> None:
    blocker = Blocking()
    with running(max_connections=1, pprof=True, profile_handler=blocker) as srv:
        addr = address(srv)
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                held = pool.submit(request, addr, "/debug/pprof/heap")
                assert blocker.entered.acquire(timeout=5)
                with socket.create_connection(addr, timeout=5) as sock:
                    with contextlib.suppress(OSError):
                        sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
                    rejected = read_until_closed(sock, timeout=3)
            finally:
                blocker.release.set()
            assert held.result(timeout=5).status == 200
        wait_until(lambda: request(addr, "/health").status == 200)
    assert status_of(rejected) == 503
    head = rejected.split(b"\r\n\r\n", 1)[0].lower()
    assert b"connection: close" in head


def test_idle_keep_alive_connections_count_against_the_cap() -> None:
    with running(max_connections=1, idle_timeout=5.0) as srv:
        addr = address(srv)
        with socket.create_connection(addr, timeout=5) as idle:
            idle.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            assert read_one_response(idle)[0] == 200
            # ``idle`` is still open (keep-alive), so the cap is full.
            assert request(addr, "/health").status == 503
        wait_until(lambda: request(addr, "/health").status == 200)


def test_capacity_is_released_after_each_connection() -> None:
    with running(max_connections=1) as srv:
        addr = address(srv)
        for _ in range(5):
            wait_until(lambda: request(addr, "/health").status == 200)


def test_concurrent_requests_below_the_cap_are_all_served() -> None:
    with running(max_connections=8) as srv:
        addr = address(srv)
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: request(addr, "/health").status, range(16)))
    assert statuses == [200] * 16


# ---------------------------------------------------------------- graceful shutdown


def test_shutdown_waits_for_in_flight_requests() -> None:
    blocker = Blocking()
    srv = server.Server(
        Registry(), server.Config(addr="127.0.0.1:0", pprof=True, profile_handler=blocker)
    )
    srv.start()
    addr = address(srv)
    done = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            in_flight = pool.submit(request, addr, "/debug/pprof/heap")
            assert blocker.entered.acquire(timeout=5)
            stopper = pool.submit(lambda: (srv.shutdown(timeout=5), done.set()))
            assert not done.wait(0.2), "shutdown returned while a request was in flight"
            # New connections are refused while draining.
            with pytest.raises(OSError):  # noqa: PT011 - refused or reset
                request(addr, "/health", timeout=1)
        finally:
            blocker.release.set()
        assert in_flight.result(timeout=5).status == 200
        stopper.result(timeout=5)
    assert done.is_set()


def test_shutdown_timeout_bounds_the_wait() -> None:
    blocker = Blocking()
    srv = server.Server(
        Registry(), server.Config(addr="127.0.0.1:0", pprof=True, profile_handler=blocker)
    )
    srv.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pending = pool.submit(request, address(srv), "/debug/pprof/heap")
            assert blocker.entered.acquire(timeout=5)
            started = time.monotonic()
            srv.shutdown(timeout=0.3)
            elapsed = time.monotonic() - started
        finally:
            blocker.release.set()
        with contextlib.suppress(Exception):
            pending.result(timeout=5)
    assert 0.2 <= elapsed < 2.0


def test_shutdown_closes_idle_keep_alive_connections() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0", idle_timeout=60))
    srv.start()
    with socket.create_connection(address(srv), timeout=5) as sock:
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        assert read_one_response(sock)[0] == 200
        started = time.monotonic()
        srv.shutdown(timeout=5)
        assert time.monotonic() - started < 2.0, "shutdown waited on an idle connection"
        assert read_until_closed(sock, timeout=3) == b""


def test_serve_forever_returns_after_shutdown() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0"))
    returned = threading.Event()

    def serve() -> None:
        srv.serve_forever()
        returned.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        wait_until(lambda: srv.address is not None)
        assert request(address(srv), "/health").status == 200
        assert not returned.is_set()
    finally:
        srv.shutdown(timeout=5)
    assert returned.wait(5)
    thread.join(5)
