"""End-to-end: the observability server (DESIGN §11) over real sockets.

Endpoints (``/metrics``, ``/health``, ``/ready``, 404 otherwise, pprof gating)
and hardening (deadline-based header reads, header byte limit, idle timeout,
connection cap, graceful shutdown). Every server binds ``127.0.0.1:0`` and
uses short timeouts; waits use events, not sleeps.
"""

from __future__ import annotations

import contextlib
import dataclasses
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import pyrock
from pyrock import server
from pyrock.metric import Bucket, MetricFamily, MetricType, Registry, Sample
from tests.e2e._http_helpers import http_get, read_until_closed, status_of
from tests.support.collectors import wait_until
from tests.support.prom import parse
from tests.support.wsgi_driver import call_wsgi


@pytest.fixture(autouse=True)
def _env(clean_env: pytest.MonkeyPatch) -> None:
    return None


@contextlib.contextmanager
def running(**overrides: Any) -> Iterator[pyrock.Bedrock]:
    """An initialised (not activated) Bedrock whose server listens on a free loopback port."""
    settings: dict[str, Any] = {
        "service": "server-e2e",
        "server_addr": "127.0.0.1:0",
        "disable_environment_detection": True,
        "log_install": "none",
        "runtime_metrics": False,
    }
    settings.update(overrides)
    bedrock = pyrock.init(activate=False, **settings)
    try:
        yield bedrock
    finally:
        bedrock.shutdown(timeout=5)


@contextlib.contextmanager
def manual(registry: Registry | None = None, **config: Any) -> Iterator[server.Server]:
    srv = server.Server(
        registry if registry is not None else Registry(),
        server.Config(addr="127.0.0.1:0", **config),
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.shutdown(timeout=5)


def _address(obj: pyrock.Bedrock | server.Server) -> tuple[str, int]:
    addr = obj.server_address if isinstance(obj, pyrock.Bedrock) else obj.address
    assert addr is not None, "server is not listening"
    return addr


class BlockingProfiler:
    """A WSGI profile handler that blocks until released (an in-flight request)."""

    def __init__(self) -> None:
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        self.entered.release()
        self.release.wait(10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"pprof-ok"]


def _pprof_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"pprof-ok"]


class BadRegistry(Registry):
    """A registry whose snapshot is not valid exposition (decreasing histogram buckets)."""

    def collect(self) -> list[MetricFamily]:
        bad = Sample(
            labels={},
            buckets=(Bucket(1.0, 5), Bucket(0.5, 3)),
            count=2,
            sum=1.0,
        )
        good = Sample(labels={}, value=1.0)
        return [
            MetricFamily("a_ok_total", "fine", MetricType.COUNTER, (good,)),
            MetricFamily("bad_hist", "broken", MetricType.HISTOGRAM, (bad,)),
        ]


# ---------------------------------------------------------------- defaults


def test_default_address_is_loopback() -> None:
    assert pyrock.Config().server_addr == "127.0.0.1:9090"
    assert server.Config().addr == server.DEFAULT_ADDR == "127.0.0.1:9090"


def test_pprof_is_off_by_default() -> None:
    assert pyrock.Config().server_pprof is False
    assert server.Config().pprof is False


def test_server_config_rejects_non_positive_timeouts() -> None:
    with pytest.raises(ValueError, match="read_header_timeout"):
        server.Config(read_header_timeout=0)


def test_init_binds_loopback() -> None:
    with running() as bedrock:
        host, port = _address(bedrock)
    assert host == "127.0.0.1"
    assert port > 0


def test_server_disabled_starts_nothing() -> None:
    with running(server_enabled=False) as bedrock:
        assert bedrock.server_address is None


def test_shutdown_stops_the_server() -> None:
    with running() as bedrock:
        addr = _address(bedrock)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(addr, timeout=1).close()


# ---------------------------------------------------------------- endpoints


def test_metrics_endpoint_serves_prometheus_text() -> None:
    with running() as bedrock:
        bedrock.counter("jobs_total", "Jobs processed").inc(3)
        resp = http_get(_address(bedrock), "/metrics")
    assert resp.status == 200
    assert resp.headers["content-type"].startswith("text/plain")
    prom = parse(resp.body)
    assert prom.value("jobs_total") == 3
    assert prom.type("jobs_total") == "counter"


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_health_and_ready(path: str) -> None:
    with running() as bedrock:
        resp = http_get(_address(bedrock), path)
    assert resp.status == 200
    assert resp.body.strip() == b"ok"


@pytest.mark.parametrize("path", ["/", "/nope", "/metricsx", "/debug/pprof/"])
def test_unknown_paths_are_404(path: str) -> None:
    with running() as bedrock:
        assert http_get(_address(bedrock), path).status == 404


def test_server_metrics_false_disables_metrics_only() -> None:
    with running(server_metrics=False) as bedrock:
        addr = _address(bedrock)
        assert http_get(addr, "/metrics").status == 404
        assert http_get(addr, "/health").status == 200


def test_pprof_flag_without_handler_enables_nothing() -> None:
    with running(server_pprof=True) as bedrock:
        assert http_get(_address(bedrock), "/debug/pprof/heap").status == 404


def test_pprof_handler_without_flag_enables_nothing() -> None:
    with running(server_profile_handler=_pprof_app) as bedrock:
        assert http_get(_address(bedrock), "/debug/pprof/heap").status == 404


def test_pprof_requires_flag_and_handler() -> None:
    with running(server_pprof=True, server_profile_handler=_pprof_app) as bedrock:
        resp = http_get(_address(bedrock), "/debug/pprof/heap")
    assert resp.status == 200
    assert resp.body == b"pprof-ok"


def test_metrics_invalid_exposition_returns_500_without_body() -> None:
    with manual(BadRegistry()) as srv:
        resp = http_get(_address(srv), "/metrics")
    assert resp.status == 500
    assert b"a_ok_total" not in resp.body
    assert b"bad_hist" not in resp.body


# ---------------------------------------------------------------- hardening


def test_slow_header_drip_is_disconnected_at_the_deadline() -> None:
    with running(server_read_header_timeout=0.5) as bedrock:
        sock = socket.create_connection(_address(bedrock), timeout=5)
        stop = threading.Event()

        def drip() -> None:
            # One byte every 50 ms: a per-recv idle timeout of 0.5 s never fires.
            with contextlib.suppress(OSError):
                while not stop.is_set():
                    sock.sendall(b"a")
                    stop.wait(0.05)

        start = time.monotonic()
        try:
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nX-Drip: ")
            dripper = threading.Thread(target=drip, daemon=True)
            dripper.start()
            data = read_until_closed(sock, timeout=4)
            elapsed = time.monotonic() - start
        finally:
            stop.set()
            sock.close()
        dripper.join(2)
    assert status_of(data) != 200
    assert 0.3 <= elapsed < 3.0


def test_header_over_limit_gets_431_and_close() -> None:
    request = b"GET /health HTTP/1.1\r\nHost: x\r\nX-Big: " + b"a" * 8192 + b"\r\n\r\n"
    with (
        running(server_max_header_bytes=1024) as bedrock,
        socket.create_connection(_address(bedrock), timeout=5) as sock,
    ):
        with contextlib.suppress(OSError):
            sock.sendall(request)
        data = read_until_closed(sock, timeout=4)
    assert status_of(data) == 431


def test_header_under_limit_is_served() -> None:
    with running(server_max_header_bytes=1024) as bedrock:
        resp = http_get(_address(bedrock), "/health", headers={"X-Small": "a" * 200})
    assert resp.status == 200


def test_idle_keep_alive_connection_is_closed() -> None:
    with (
        manual(idle_timeout=0.5) as srv,
        socket.create_connection(_address(srv), timeout=5) as sock,
    ):
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        start = time.monotonic()
        data = read_until_closed(sock, timeout=4)
        elapsed = time.monotonic() - start
    assert status_of(data) == 200
    assert elapsed < 3.0


def test_connection_cap_rejects_beyond_limit() -> None:
    blocker = BlockingProfiler()
    with manual(max_connections=2, pprof=True, profile_handler=blocker) as srv:
        addr = _address(srv)
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                held = [pool.submit(http_get, addr, "/debug/pprof/profile") for _ in range(2)]
                for _ in held:
                    assert blocker.entered.acquire(timeout=5), "in-flight request never arrived"
                with socket.create_connection(addr, timeout=5) as sock:
                    with contextlib.suppress(OSError):
                        sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
                    rejected = read_until_closed(sock, timeout=3)
            finally:
                blocker.release.set()
            assert [f.result(timeout=5).status for f in held] == [200, 200]
        assert status_of(rejected) == 503
        # Capacity frees up once the in-flight requests finish.
        wait_until(lambda: http_get(addr, "/health").status == 200)


def test_init_server_max_connections_maps_to_the_server() -> None:
    assert "server_max_connections" in {f.name for f in dataclasses.fields(pyrock.Config)}
    assert pyrock.Config().server_max_connections == 64
    blocker = BlockingProfiler()
    with (
        running(
            server_max_connections=1, server_pprof=True, server_profile_handler=blocker
        ) as bedrock,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        addr = _address(bedrock)
        try:
            held = pool.submit(http_get, addr, "/debug/pprof/profile")
            assert blocker.entered.acquire(timeout=5)
            assert http_get(addr, "/health").status == 503
        finally:
            blocker.release.set()
        assert held.result(timeout=5).status == 200


def test_profile_handler_sees_unchanged_path_info() -> None:
    paths: list[str] = []

    def handler(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        paths.append(environ["PATH_INFO"])
        start_response("200 OK", [])
        return [b""]

    with running(server_pprof=True, server_profile_handler=handler) as bedrock:
        http_get(_address(bedrock), "/debug/pprof/goroutine?debug=1")
    assert paths == ["/debug/pprof/goroutine"]


def test_graceful_shutdown_waits_for_in_flight_requests() -> None:
    blocker = BlockingProfiler()
    srv = server.Server(
        Registry(),
        server.Config(addr="127.0.0.1:0", pprof=True, profile_handler=blocker),
    )
    srv.start()
    addr = _address(srv)
    done = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            in_flight = pool.submit(http_get, addr, "/debug/pprof/profile")
            assert blocker.entered.acquire(timeout=5)
            stopper = pool.submit(lambda: (srv.shutdown(timeout=5), done.set()))
            assert not done.wait(0.2), "shutdown returned while a request was in flight"
        finally:
            blocker.release.set()
        assert in_flight.result(timeout=5).status == 200
        stopper.result(timeout=5)
    assert done.is_set()
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(addr, timeout=1).close()


def test_shutdown_timeout_bounds_the_wait() -> None:
    blocker = BlockingProfiler()
    srv = server.Server(
        Registry(),
        server.Config(addr="127.0.0.1:0", pprof=True, profile_handler=blocker),
    )
    srv.start()
    addr = _address(srv)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            in_flight = pool.submit(http_get, addr, "/debug/pprof/profile")
            assert blocker.entered.acquire(timeout=5)
            start = time.monotonic()
            srv.shutdown(timeout=0.3)
            elapsed = time.monotonic() - start
        finally:
            blocker.release.set()
        with contextlib.suppress(Exception):
            in_flight.result(timeout=5)
    assert elapsed < 2.0


# ---------------------------------------------------------------- manual server and apps


def test_manual_server_address_only_after_start() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0"))
    assert srv.address is None
    assert srv.config.addr == "127.0.0.1:0"
    srv.start()
    try:
        host, port = _address(srv)
        assert host == "127.0.0.1"
        assert http_get((host, port), "/health").status == 200
    finally:
        srv.shutdown(timeout=5)


def test_manual_server_shutdown_is_idempotent() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0"))
    srv.start()
    srv.shutdown(timeout=5)
    srv.shutdown(timeout=5)


def test_manual_server_serves_its_registry() -> None:
    registry = Registry()
    registry.gauge("queue_depth", "Depth").set(7)
    with manual(registry) as srv:
        resp = http_get(_address(srv), "/metrics")
    assert parse(resp.body).value("queue_depth") == 7


def test_serve_forever_blocks_until_shutdown() -> None:
    srv = server.Server(Registry(), server.Config(addr="127.0.0.1:0"))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        wait_until(lambda: srv.address is not None)
        assert http_get(_address(srv), "/ready").status == 200
        assert thread.is_alive()
    finally:
        srv.shutdown(timeout=5)
    thread.join(5)
    assert not thread.is_alive()


def test_metrics_app_is_a_plain_wsgi_app() -> None:
    registry = Registry()
    registry.counter("hits_total", "Hits").inc()
    resp = call_wsgi(server.metrics_app(registry), "GET", "/metrics")
    assert resp.status_code == 200
    assert (resp.header("Content-Type") or "").startswith("text/plain")
    assert parse(resp.body).value("hits_total") == 1


def test_metrics_app_invalid_exposition_is_500_with_no_partial_body() -> None:
    resp = call_wsgi(server.metrics_app(BadRegistry()), "GET", "/metrics")
    assert resp.status_code == 500
    assert resp.body == b""


def test_health_app() -> None:
    resp = call_wsgi(server.health_app(), "GET", "/health")
    assert resp.status_code == 200
    assert resp.body.strip() == b"ok"
