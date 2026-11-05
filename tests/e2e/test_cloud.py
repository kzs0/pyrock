"""Cloud backend end to end (DESIGN §16, Go ``WithCloud`` / ``wireCloud``).

One cloud ``endpoint`` serves gRPC traces *and* HTTP metrics/profile pushes,
so each test points it at the fake that observes the behaviour under test:
the gRPC collector for traces, the push sink for metrics and profiles.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from tests.e2e._export_helpers import pause, running, start
from tests.support.collectors import GRPCCollector, MetricsPushSink, OTLPHTTPCollector
from tests.support.prom import parse

pytestmark = pytest.mark.usefixtures("clean_env")

API_KEY = "brk_test_0123456789"


def _cloud(endpoint: str, **kw: Any) -> Any:
    import pyrock

    options: dict[str, Any] = {
        "endpoint": endpoint,
        "insecure": True,
        "push_interval": 60.0,
        "profiles": (),
    }
    options.update(kw)
    return pyrock.cloud(API_KEY, **options)


def _fast_retry_cloud(endpoint: str, **kw: Any) -> Any:
    """A CloudConfig with tiny retry back-off so retry tests stay fast."""
    import pyrock

    options: dict[str, Any] = {
        "api_key": API_KEY,
        "endpoint": endpoint,
        "insecure": True,
        "push_interval": 60.0,
        "profiles": (),
        "retry_backoff": (0.01, 0.02, 0.04),
    }
    options.update(kw)
    return pyrock.CloudConfig(**options)


def _grpc_endpoint(collector: GRPCCollector) -> str:
    return f"http://{collector.endpoint}"


# ------------------------------------------------------------------ CloudConfig


def test_cloud_config_repr_hides_api_key() -> None:
    import pyrock

    cfg = pyrock.cloud(API_KEY)
    assert API_KEY not in repr(cfg)
    assert API_KEY not in str(cfg)


def test_cloud_config_equality_ignores_api_key() -> None:
    import pyrock

    assert pyrock.cloud("brk_a") == pyrock.cloud("brk_b")


def test_cloud_config_defaults_match_go() -> None:
    import pyrock
    from pyrock.profile import ProfileType

    cfg = pyrock.cloud(API_KEY)
    assert cfg.endpoint == "https://ingest.bedrock.dev"
    assert cfg.push_interval == 15.0
    assert cfg.profile_interval == 60.0
    assert cfg.profile_cpu_sample_duration == 10.0
    assert cfg.profiles == (ProfileType.CPU, ProfileType.HEAP, ProfileType.THREADS)


def test_cloud_config_is_exported_lazily() -> None:
    import pyrock

    assert isinstance(pyrock.cloud(API_KEY), pyrock.CloudConfig)


def test_cloud_rejects_cpu_window_not_below_interval() -> None:
    import pyrock

    with pytest.raises(ValueError, match="profile_cpu_sample_duration"):
        pyrock.cloud(API_KEY, profile_interval=1.0, profile_cpu_sample_duration=1.0)


def test_cloud_rejects_empty_api_key() -> None:
    import pyrock

    with pytest.raises(ValueError, match="api_key"):
        pyrock.cloud("")


def test_cloud_rejects_api_key_with_newline() -> None:
    import pyrock

    with pytest.raises(ValueError, match="api_key"):
        pyrock.cloud("brk\r\nX-Injected: 1")


def test_retry_backoff_defaults_match_go() -> None:
    import pyrock

    assert pyrock.cloud(API_KEY).retry_backoff == (1.0, 2.0, 4.0)


def test_retry_backoff_rejects_negative_values() -> None:
    import pyrock

    with pytest.raises(ValueError, match="retry_backoff"):
        pyrock.CloudConfig(api_key=API_KEY, retry_backoff=(1.0, -0.5))


def test_retry_backoff_rejects_too_many_entries() -> None:
    import pyrock

    with pytest.raises(ValueError, match="retry_backoff"):
        pyrock.CloudConfig(api_key=API_KEY, retry_backoff=(0.1,) * 11)


def test_retry_backoff_accepts_ten_entries() -> None:
    import pyrock

    cfg = pyrock.CloudConfig(api_key=API_KEY, retry_backoff=[0.1] * 10)
    assert cfg.retry_backoff == (0.1,) * 10


def test_traces_endpoint_defaults_to_none() -> None:
    import pyrock

    assert pyrock.cloud(API_KEY).traces_endpoint is None


# ------------------------------------------------------------------ metrics push


def test_metrics_push_sends_bearer_token(push_sink: MetricsPushSink) -> None:
    with running(cloud=_cloud(push_sink.endpoint, push_interval=0.1)) as b:
        b.counter("pushed_total", "Pushed").inc(4)
        push = push_sink.wait_for_pushes(1)[0]
    assert push.method == "POST"
    assert push.path == "/v1/metrics"
    assert push.header("authorization") == f"Bearer {API_KEY}"


def test_metrics_push_body_is_prometheus_text(push_sink: MetricsPushSink) -> None:
    with running(cloud=_cloud(push_sink.endpoint, push_interval=0.1)) as b:
        b.counter("pushed_total", "Pushed").inc(4)
        push_sink.wait_for_pushes(1)
    push = push_sink.pushes()[-1]
    assert (push.header("content-type") or "").startswith("text/plain")
    assert "version=0.0.4" in (push.header("content-type") or "")
    assert parse(push.text).value("pushed_total") == 4


def test_metrics_push_repeats_at_interval(push_sink: MetricsPushSink) -> None:
    started = time.monotonic()
    with running(cloud=_cloud(push_sink.endpoint, push_interval=0.15)):
        push_sink.wait_for_pushes(3, timeout=5)
        elapsed = time.monotonic() - started
    # Three pushes need at least ~three intervals; well below a 15 s default.
    assert 0.3 <= elapsed < 4.0


def test_metrics_push_honors_long_interval(push_sink: MetricsPushSink) -> None:
    with running(cloud=_cloud(push_sink.endpoint, push_interval=30.0)):
        pause(0.4)
        assert push_sink.pushes() == []


def test_final_flush_on_shutdown(push_sink: MetricsPushSink) -> None:
    b = start(cloud=_cloud(push_sink.endpoint, push_interval=60.0))
    try:
        b.counter("late_total", "Late").inc(7)
    finally:
        b.shutdown(timeout=5)
    pushes = push_sink.pushes()
    assert len(pushes) == 1
    assert parse(pushes[0].text).value("late_total") == 7


def test_final_flush_is_bounded_by_shutdown_timeout(push_sink: MetricsPushSink) -> None:
    push_sink.hang()
    b = start(cloud=_cloud(push_sink.endpoint, push_interval=60.0))
    started = time.monotonic()
    try:
        b.shutdown(timeout=0.5)
        elapsed = time.monotonic() - started
    finally:
        push_sink.release()
    assert elapsed < 3.0


def test_push_skipped_while_previous_in_flight(push_sink: MetricsPushSink) -> None:
    push_sink.hang()  # the first push never completes until released
    b = start(cloud=_cloud(push_sink.endpoint, push_interval=0.05))
    try:
        push_sink.wait_for_pushes(1)
        pause(0.5)  # ~10 intervals elapse while the first push is in flight
        in_flight = len(push_sink.pushes())
    finally:
        push_sink.release()
        b.shutdown(timeout=5)
    assert in_flight == 1


def test_failed_push_does_not_stop_later_pushes(push_sink: MetricsPushSink) -> None:
    push_sink.fail_next(2, status=503, path="/v1/metrics")
    with running(cloud=_cloud(push_sink.endpoint, push_interval=0.05)):
        push_sink.wait_for_pushes(3)


# ------------------------------------------------------------------ cloud traces


@pytest.mark.compat
def test_cloud_traces_carry_api_key_metadata(grpc_collector: GRPCCollector) -> None:
    with running(cloud=_cloud(_grpc_endpoint(grpc_collector))) as b, b.operation("cloudy"):
        pass
    grpc_collector.wait_for_calls(1)
    assert grpc_collector.metadata[0]["x-bedrock-key"] == API_KEY
    assert [s["name"] for s in grpc_collector.spans()] == ["cloudy"]


@pytest.mark.compat
def test_cloud_traces_carry_service_name(grpc_collector: GRPCCollector) -> None:
    with (
        running(service="cloud-svc", cloud=_cloud(_grpc_endpoint(grpc_collector))) as b,
        b.operation("named"),
    ):
        pass
    assert grpc_collector.wait_for_spans(1)[0]["resource"]["service.name"] == "cloud-svc"


@pytest.mark.compat
def test_cloud_trace_export_retries_with_backoff(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(2, code="UNAVAILABLE")
    cloud = _fast_retry_cloud(_grpc_endpoint(grpc_collector), retry_backoff=(0.2, 0.4, 0.8))
    b = start(cloud=cloud)
    with b.operation("retried"):
        pass
    started = time.monotonic()
    b.shutdown(timeout=10)
    elapsed = time.monotonic() - started
    assert len(grpc_collector.requests) == 3
    # Back-off of 0.2 s then 0.4 s between the three calls.
    assert elapsed >= 0.5
    assert b.trace_export_stats().exported == 1


@pytest.mark.compat
def test_cloud_trace_export_gives_up_after_retries(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(10, code="UNAVAILABLE")
    b = start(cloud=_fast_retry_cloud(_grpc_endpoint(grpc_collector)))
    with b.operation("doomed"):
        pass
    b.shutdown(timeout=10)
    assert len(grpc_collector.requests) == 4  # 1 attempt + 3 retries, as in Go
    assert b.trace_export_stats().failed >= 1
    assert b.trace_export_stats().exported == 0


@pytest.mark.compat
def test_retry_backoff_length_sets_retry_count(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(10, code="UNAVAILABLE")
    cloud = _fast_retry_cloud(_grpc_endpoint(grpc_collector), retry_backoff=(0.01,))
    b = start(cloud=cloud)
    with b.operation("once_retried"):
        pass
    b.shutdown(timeout=10)
    assert len(grpc_collector.requests) == 2


@pytest.mark.compat
def test_empty_retry_backoff_disables_retries(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(10, code="UNAVAILABLE")
    b = start(cloud=_fast_retry_cloud(_grpc_endpoint(grpc_collector), retry_backoff=()))
    with b.operation("no_retry"):
        pass
    b.shutdown(timeout=10)
    assert len(grpc_collector.requests) == 1


@pytest.mark.compat
def test_shutdown_interrupts_retry_backoff(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(10, code="UNAVAILABLE")
    b = start(cloud=_cloud(_grpc_endpoint(grpc_collector)))
    with b.operation("interrupted"):
        pass
    started = time.monotonic()
    b.shutdown(timeout=0.5)
    assert time.monotonic() - started < 2.5


@pytest.mark.compat
def test_fan_out_sends_spans_to_local_and_cloud(
    grpc_collector: GRPCCollector, otlp_collector: OTLPHTTPCollector
) -> None:
    with (
        running(
            trace_url=otlp_collector.traces_url,
            cloud=_cloud(_grpc_endpoint(grpc_collector)),
        ) as b,
        b.operation("both"),
    ):
        pass
    cloud_spans = grpc_collector.wait_for_spans(1)
    local_span = otlp_collector.span("both")
    assert [s["name"] for s in cloud_spans] == ["both"]
    assert cloud_spans[0]["trace_id"] == local_span["trace_id"]
    assert cloud_spans[0]["span_id"] == local_span["span_id"]


@pytest.mark.compat
def test_fan_out_local_failure_still_reaches_cloud(
    grpc_collector: GRPCCollector, otlp_collector: OTLPHTTPCollector
) -> None:
    otlp_collector.set_default_status(500)
    with (
        running(
            trace_url=otlp_collector.traces_url,
            cloud=_cloud(_grpc_endpoint(grpc_collector)),
        ) as b,
        b.operation("half"),
    ):
        pass
    assert [s["name"] for s in grpc_collector.wait_for_spans(1)] == ["half"]
    assert otlp_collector.trace_requests()


@pytest.mark.compat
def test_cloud_batch_absorbs_bursts_beyond_default_queue(grpc_collector: GRPCCollector) -> None:
    total = 3000  # above the default 2048 queue, below the cloud processor's larger one
    with running(cloud=_cloud(_grpc_endpoint(grpc_collector))) as b:
        for _ in range(total):
            with b.operation("burst"):
                pass
    assert b.trace_export_stats().dropped == 0
    assert len(grpc_collector.spans()) == total


# ------------------------------------------------------------------ cloud profiles


def test_cloud_profiles_send_bearer_token(push_sink: MetricsPushSink) -> None:
    from pyrock.profile import ProfileType

    cloud = _cloud(
        push_sink.endpoint,
        profile_interval=0.3,
        profile_cpu_sample_duration=0.1,
        profiles=[ProfileType.HEAP],
    )
    with running(cloud=cloud):
        req = push_sink.wait_for_profiles(1)[0]
    assert req.method == "POST"
    assert req.header("authorization") == f"Bearer {API_KEY}"
    assert req.header("content-type") == "application/x-protobuf"
    assert req.header("x-profile-type") == "heap"
    assert req.body


def test_cloud_profiles_only_selected_types(push_sink: MetricsPushSink) -> None:
    from pyrock.profile import ProfileType

    cloud = _cloud(
        push_sink.endpoint,
        profile_interval=0.3,
        profile_cpu_sample_duration=0.1,
        profiles=[ProfileType.THREADS],
    )
    with running(cloud=cloud):
        push_sink.wait_for_profiles(2, timeout=5)
    types = {r.header("x-profile-type") for r in push_sink.profile_requests()}
    assert types == {"goroutine"}


def test_cloud_empty_profiles_disables_profiling(push_sink: MetricsPushSink) -> None:
    cloud = _cloud(
        push_sink.endpoint, profile_interval=0.1, profile_cpu_sample_duration=0.05, profiles=()
    )
    with running(cloud=cloud) as b:
        pause(0.5)
        assert push_sink.profile_requests() == []
        assert b.profile_stats().exported == 0


def test_cloud_shutdown_stops_all_workers(push_sink: MetricsPushSink) -> None:
    from pyrock.profile import ProfileType

    cloud = _cloud(
        push_sink.endpoint,
        push_interval=0.05,
        profile_interval=0.2,
        profile_cpu_sample_duration=0.05,
        profiles=[ProfileType.HEAP],
    )
    b = start(cloud=cloud)
    push_sink.wait_for_pushes(1)
    b.shutdown(timeout=5)
    settled = len(push_sink.requests)
    pause(0.4)
    assert len(push_sink.requests) == settled
    assert b.closed


def test_cloud_workers_are_daemon_threads(push_sink: MetricsPushSink) -> None:
    before = set(threading.enumerate())
    with running(cloud=_cloud(push_sink.endpoint, push_interval=0.05)):
        push_sink.wait_for_pushes(1)
        started = set(threading.enumerate()) - before
        assert started
        assert all(t.daemon for t in started)


@pytest.mark.compat
def test_traces_endpoint_splits_traces_from_pushes(
    grpc_collector: GRPCCollector, push_sink: MetricsPushSink
) -> None:
    from pyrock.profile import ProfileType

    cloud = _fast_retry_cloud(
        push_sink.endpoint,
        traces_endpoint=grpc_collector.endpoint,
        push_interval=0.1,
        profile_interval=0.3,
        profile_cpu_sample_duration=0.1,
        profiles=(ProfileType.HEAP,),
    )
    with running(cloud=cloud) as b:
        b.counter("combined_total", "C").inc()
        with b.operation("combined"):
            pass
        push_sink.wait_for_pushes(1)
        push_sink.wait_for_profiles(1)
    assert [s["name"] for s in grpc_collector.wait_for_spans(1)] == ["combined"]
    assert grpc_collector.metadata[0]["x-bedrock-key"] == API_KEY
    assert push_sink.pushes()[0].header("authorization") == f"Bearer {API_KEY}"
    assert push_sink.profile_requests()[0].header("authorization") == f"Bearer {API_KEY}"
    assert push_sink.trace_requests() == []
