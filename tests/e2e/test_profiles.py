"""Profiling end to end (DESIGN §12): OTLP profile export and the pprof debug handler.

Uploads are observed at the fake collector. Without the compat group the
protobuf is checked structurally (function names travel as raw UTF-8 strings
in the string table); with it, bodies are decoded with the official schema.
"""

from __future__ import annotations

import gzip
import threading
import time
import tracemalloc
from collections.abc import Iterator
from typing import Any

import pytest

from tests.e2e._export_helpers import body, pause, running, start
from tests.support.collectors import OTLPHTTPCollector
from tests.support.otlp_decode import decode_profiles_request_pb
from tests.support.wsgi_driver import call_wsgi

pytestmark = pytest.mark.usefixtures("clean_env")


def _config(collector: OTLPHTTPCollector, **kw: Any) -> Any:
    from pyrock.profile import ProfileExportConfig

    options: dict[str, Any] = {
        "endpoint": collector.profiles_url,
        "allow_http": True,
        "interval": 0.3,
        "cpu_sample_duration": 0.15,
        "retries": 0,
        "timeout": 2.0,
    }
    options.update(kw)
    return ProfileExportConfig(**options)


def _of_type(collector: OTLPHTTPCollector, kind: str) -> list[Any]:
    return [r for r in collector.profile_requests() if r.header("x-profile-type") == kind]


def _wait_for_type(collector: OTLPHTTPCollector, kind: str, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        found = _of_type(collector, kind)
        if found:
            return found[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            seen = [r.header("x-profile-type") for r in collector.profile_requests()]
            raise AssertionError(f"no {kind!r} profile within {timeout}s; got {seen}")
        try:
            collector.wait_for_profiles(len(collector.profile_requests()) + 1, timeout=remaining)
        except AssertionError:
            continue


# --------------------------------------------------------------- workloads


def _busy_spin_for_cpu_profile(stop: threading.Event) -> None:
    n = 0
    while not stop.is_set():
        for i in range(2000):
            n += i * i
    del n


def _parked_worker_for_threads_profile(release: threading.Event) -> None:
    release.wait(10)


def _contend_on_instrumented_lock(lock: Any, ready: threading.Event) -> None:
    ready.set()
    with lock:
        pass


@pytest.fixture
def busy_thread() -> Iterator[threading.Thread]:
    stop = threading.Event()
    thread = threading.Thread(target=_busy_spin_for_cpu_profile, args=(stop,), daemon=True)
    thread.start()
    yield thread
    stop.set()
    thread.join(5)


@pytest.fixture
def parked_thread() -> Iterator[threading.Thread]:
    release = threading.Event()
    thread = threading.Thread(
        target=_parked_worker_for_threads_profile,
        args=(release,),
        name="parked-for-profile",
        daemon=True,
    )
    thread.start()
    yield thread
    release.set()
    thread.join(5)


# ---------------------------------------------------------- config validation


def test_cpu_sample_duration_must_be_below_interval(otlp_collector: OTLPHTTPCollector) -> None:
    with pytest.raises(ValueError, match="cpu_sample_duration"):
        _config(otlp_collector, interval=1.0, cpu_sample_duration=1.0)


def test_cpu_sample_duration_must_be_positive(otlp_collector: OTLPHTTPCollector) -> None:
    with pytest.raises(ValueError, match="cpu_sample_duration"):
        _config(otlp_collector, interval=1.0, cpu_sample_duration=0)


def test_http_endpoint_requires_allow_http(otlp_collector: OTLPHTTPCollector) -> None:
    with pytest.raises(ValueError, match="https"):
        _config(otlp_collector, allow_http=False)


def test_unknown_profile_type_is_rejected(otlp_collector: OTLPHTTPCollector) -> None:
    with pytest.raises(ValueError, match="profile type"):
        _config(otlp_collector, types=["nope"])


def test_profile_config_repr_hides_headers(otlp_collector: OTLPHTTPCollector) -> None:
    cfg = _config(otlp_collector, headers={"Authorization": "Bearer s3cr3t-token"})
    assert "s3cr3t-token" not in repr(cfg)


def test_goroutine_is_alias_of_threads() -> None:
    from pyrock.profile import ProfileType

    assert ProfileType.GOROUTINE is ProfileType.THREADS
    assert ProfileType.THREADS.value == "goroutine"


# ----------------------------------------------------------------- OTLP export


def test_profiles_are_posted_as_protobuf(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    with running(profiles=_config(otlp_collector, types=[ProfileType.HEAP])):
        req = otlp_collector.wait_for_profiles(1)[0]
    assert req.method == "POST"
    assert req.header("content-type") == "application/x-protobuf"
    assert req.header("x-profile-type") == "heap"
    assert body(req)


def test_profile_headers_are_sent(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    cfg = _config(
        otlp_collector, types=[ProfileType.HEAP], headers={"Authorization": "Bearer tok-1"}
    )
    with running(profiles=cfg):
        req = otlp_collector.wait_for_profiles(1)[0]
    assert req.header("authorization") == "Bearer tok-1"


def test_profile_carries_service_name(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    with running(
        service="profiled-svc", profiles=_config(otlp_collector, types=[ProfileType.HEAP])
    ):
        req = otlp_collector.wait_for_profiles(1)[0]
    assert b"profiled-svc" in body(req)


def test_profile_types_selection(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    cfg = _config(otlp_collector, types=[ProfileType.HEAP, ProfileType.THREADS])
    with running(profiles=cfg):
        _wait_for_type(otlp_collector, "heap")
        _wait_for_type(otlp_collector, "goroutine")
    kinds = {r.header("x-profile-type") for r in otlp_collector.profile_requests()}
    assert kinds == {"heap", "goroutine"}


def test_empty_types_disable_collection(otlp_collector: OTLPHTTPCollector) -> None:
    with running(
        profiles=_config(otlp_collector, types=[], interval=0.1, cpu_sample_duration=0.05)
    ):
        pause(0.5)
    assert otlp_collector.profile_requests() == []


def test_profile_stats_count_exports(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    with running(profiles=_config(otlp_collector, types=[ProfileType.HEAP])) as b:
        otlp_collector.wait_for_profiles(2)
        stats = b.profile_stats()
    assert stats.exported >= 2
    assert stats.failed == 0


def test_profile_stats_count_failures(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    otlp_collector.set_default_status(500)
    with running(profiles=_config(otlp_collector, types=[ProfileType.HEAP])) as b:
        otlp_collector.wait_for_profiles(2)
        pause(0.1)
        stats = b.profile_stats()
    assert stats.failed >= 1
    assert stats.exported == 0


def test_profile_retries_on_server_error(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    otlp_collector.fail_next(1, status=503, path="/v1/profiles")
    cfg = _config(otlp_collector, types=[ProfileType.HEAP], retries=2)
    with running(profiles=cfg) as b:
        otlp_collector.wait_for_profiles(2)
        pause(0.1)
        stats = b.profile_stats()
    assert stats.failed == 0
    assert stats.exported >= 1


def test_profile_redirects_are_not_followed(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    with OTLPHTTPCollector() as elsewhere:
        otlp_collector.set_default_status(200)
        otlp_collector.fail_next(
            5, status=307, headers=[("Location", elsewhere.profiles_url)], path="/v1/profiles"
        )
        with running(profiles=_config(otlp_collector, types=[ProfileType.HEAP])) as b:
            otlp_collector.wait_for_profiles(1)
            pause(0.3)
            stats = b.profile_stats()
        assert elsewhere.requests == []
    assert stats.failed >= 1


def test_start_profile_export_is_stopped_by_shutdown(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    b = start()
    try:
        exporter = b.start_profile_export(_config(otlp_collector, types=[ProfileType.HEAP]))
        assert exporter.running
        otlp_collector.wait_for_profiles(1)
    finally:
        b.shutdown(timeout=5)
    assert not exporter.running
    settled = len(otlp_collector.profile_requests())
    pause(0.5)
    assert len(otlp_collector.profile_requests()) == settled


def test_start_profile_export_counts_in_profile_stats(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    with running() as b:
        b.start_profile_export(_config(otlp_collector, types=[ProfileType.HEAP]))
        otlp_collector.wait_for_profiles(1)
        pause(0.1)
        assert b.profile_stats().exported >= 1


def test_stopped_exporter_stays_stopped(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    with running() as b:
        exporter = b.start_profile_export(_config(otlp_collector, types=[ProfileType.HEAP]))
        exporter.stop(timeout=5)
        exporter.stop(timeout=5)  # idempotent
        assert not exporter.running


# ----------------------------------------------------------------- content


def test_cpu_profile_captures_busy_function(
    otlp_collector: OTLPHTTPCollector, busy_thread: threading.Thread
) -> None:
    from pyrock.profile import ProfileType

    with running(profiles=_config(otlp_collector, types=[ProfileType.CPU])):
        req = _wait_for_type(otlp_collector, "cpu")
    assert b"_busy_spin_for_cpu_profile" in body(req)


def test_wall_profile_captures_parked_thread(
    otlp_collector: OTLPHTTPCollector, parked_thread: threading.Thread
) -> None:
    from pyrock.profile import ProfileType

    with running(profiles=_config(otlp_collector, types=[ProfileType.WALL])):
        req = _wait_for_type(otlp_collector, "wall")
    assert b"_parked_worker_for_threads_profile" in body(req)


def test_threads_profile_shows_thread_stacks(
    otlp_collector: OTLPHTTPCollector, parked_thread: threading.Thread
) -> None:
    from pyrock.profile import ProfileType

    with running(profiles=_config(otlp_collector, types=[ProfileType.THREADS])):
        req = _wait_for_type(otlp_collector, "goroutine")
    assert b"_parked_worker_for_threads_profile" in body(req)


def test_heap_profile_starts_and_stops_tracemalloc(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already tracing in this process")
    with running(profiles=_config(otlp_collector, types=[ProfileType.HEAP])):
        otlp_collector.wait_for_profiles(1)
        assert tracemalloc.is_tracing()
    assert not tracemalloc.is_tracing()


def test_heap_profile_keeps_pre_started_tracemalloc(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    was_tracing = tracemalloc.is_tracing()
    tracemalloc.start()
    try:
        with running(profiles=_config(otlp_collector, types=[ProfileType.HEAP])):
            otlp_collector.wait_for_profiles(1)
        assert tracemalloc.is_tracing()
    finally:
        if not was_tracing:
            tracemalloc.stop()


def test_tracemalloc_not_started_without_heap(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already tracing in this process")
    with running(profiles=_config(otlp_collector, types=[ProfileType.THREADS])):
        otlp_collector.wait_for_profiles(1)
        assert not tracemalloc.is_tracing()


def test_mutex_profile_records_instrumented_lock_contention(
    otlp_collector: OTLPHTTPCollector,
) -> None:
    from pyrock.profile import Lock, ProfileType

    cfg = _config(otlp_collector, types=[ProfileType.MUTEX], mutex_sample_fraction=1)
    with running(profiles=cfg):
        lock = Lock()
        ready = threading.Event()
        lock.acquire()
        waiter = threading.Thread(target=_contend_on_instrumented_lock, args=(lock, ready))
        waiter.start()
        ready.wait(5)
        pause(0.1)  # the waiter is now blocked inside acquire()
        lock.release()
        waiter.join(5)
        req = _wait_for_type(otlp_collector, "mutex")
    assert b"_contend_on_instrumented_lock" in body(req)


def test_instrumented_lock_behaves_like_threading_lock() -> None:
    from pyrock.profile import Lock

    lock = Lock()
    assert lock.acquire()
    assert lock.locked()
    assert not lock.acquire(blocking=False)
    lock.release()
    assert not lock.locked()
    with lock:
        assert lock.locked()


@pytest.mark.compat
def test_profile_decodes_with_official_schema(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.profile import ProfileType

    with running(service="decoded-svc", profiles=_config(otlp_collector, types=[ProfileType.HEAP])):
        req = otlp_collector.wait_for_profiles(1)[0]
    message = decode_profiles_request_pb(body(req))
    assert len(message.resource_profiles) == 1
    resource = {
        kv.key: kv.value.string_value for kv in message.resource_profiles[0].resource.attributes
    }
    assert resource["service.name"] == "decoded-svc"
    profiles = [p for sp in message.resource_profiles[0].scope_profiles for p in sp.profiles]
    assert profiles
    assert "bytes" in set(message.dictionary.string_table)


@pytest.mark.compat
def test_cpu_profile_decodes_busy_function_name(
    otlp_collector: OTLPHTTPCollector, busy_thread: threading.Thread
) -> None:
    from pyrock.profile import ProfileType

    with running(profiles=_config(otlp_collector, types=[ProfileType.CPU])):
        req = _wait_for_type(otlp_collector, "cpu")
    message = decode_profiles_request_pb(body(req))
    strings = list(message.dictionary.string_table)
    names = {strings[f.name_strindex] for f in message.dictionary.function_table}
    assert any("_busy_spin_for_cpu_profile" in n for n in names)
    assert "nanoseconds" in strings


# --------------------------------------------------------------- debug handler


def _debug_app(**kw: Any) -> Any:
    from pyrock.profile import debug

    return debug.app(**kw)


def test_debug_cpu_profile_returns_gzipped_pprof(busy_thread: threading.Thread) -> None:
    resp = call_wsgi(_debug_app(), "GET", "/debug/pprof/profile?seconds=1")
    assert resp.status_code == 200
    assert resp.body[:2] == b"\x1f\x8b"
    raw = gzip.decompress(resp.body)
    assert raw
    assert b"nanoseconds" in raw
    assert b"_busy_spin_for_cpu_profile" in raw


def test_debug_profile_seconds_is_capped() -> None:
    started = time.monotonic()
    resp = call_wsgi(_debug_app(max_seconds=0.3), "GET", "/debug/pprof/profile?seconds=10")
    elapsed = time.monotonic() - started
    # Either clamped to the cap (200) or rejected outright like Go (400); never 10 s.
    assert resp.status_code in {200, 400}
    assert elapsed < 3.0


def test_debug_handler_rejects_post() -> None:
    resp = call_wsgi(_debug_app(), "POST", "/debug/pprof/heap", body=b"")
    assert resp.status_code == 405


def test_debug_heap_returns_gzipped_pprof() -> None:
    resp = call_wsgi(_debug_app(), "GET", "/debug/pprof/heap")
    assert resp.status_code == 200
    assert gzip.decompress(resp.body)


def test_debug_goroutine_text_lists_thread_stacks(parked_thread: threading.Thread) -> None:
    resp = call_wsgi(_debug_app(), "GET", "/debug/pprof/goroutine?debug=1")
    assert resp.status_code == 200
    assert b"_parked_worker_for_threads_profile" in resp.body


def test_debug_goroutine_returns_gzipped_pprof(parked_thread: threading.Thread) -> None:
    resp = call_wsgi(_debug_app(), "GET", "/debug/pprof/goroutine")
    assert resp.status_code == 200
    assert b"_parked_worker_for_threads_profile" in gzip.decompress(resp.body)


def test_debug_handler_mounted_on_observability_server() -> None:
    import urllib.request

    with running(
        server_enabled=True,
        server_addr="127.0.0.1:0",
        server_pprof=True,
        server_profile_handler=_debug_app(),
    ) as b:
        host, port = b.server_address
        url = f"http://{host}:{port}/debug/pprof/heap"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            assert gzip.decompress(resp.read())


def test_pprof_without_handler_serves_nothing() -> None:
    import urllib.error
    import urllib.request

    with running(server_enabled=True, server_addr="127.0.0.1:0", server_pprof=True) as b:
        host, port = b.server_address
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(f"http://{host}:{port}/debug/pprof/heap", timeout=5)
    assert info.value.code == 404
