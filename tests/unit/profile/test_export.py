"""Profile export: ``ProfileExportConfig``, ``start_export`` and ``ProfileExporter``.

Uploads land on the shared OTLP fake collector and are decoded with the
test-side reader. Intervals are short; waits poll exporter stats or wait on
collector events.
"""

from __future__ import annotations

import dataclasses
import datetime
import gzip
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from pyrock.profile import (
    DEFAULT_PROFILE_TYPES,
    ProfileExportConfig,
    ProfileExporter,
    ProfileExportStats,
    ProfileType,
    start_export,
)
from pyrock.profile._sampler import SamplingProfiler, acquire_ownership, release_ownership
from tests.support.collectors import OTLPHTTPCollector, RecordedRequest, wait_until
from tests.unit.profile._helpers import live_threads, new_threads_settled
from tests.unit.profile._proto import OtlpRequest, decode_otlp_profiles

pytestmark = pytest.mark.usefixtures("clean_env")

PROFILES_PATH = "/v1/profiles"


def config(collector: OTLPHTTPCollector, **kw: Any) -> ProfileExportConfig:
    options: dict[str, Any] = {
        "endpoint": collector.profiles_url,
        "allow_http": True,
        "types": (ProfileType.HEAP,),
        "interval": 0.3,
        "cpu_sample_duration": 0.1,
        "retries": 0,
        "timeout": 2.0,
    }
    options.update(kw)
    return ProfileExportConfig(**options)


@pytest.fixture
def exporters() -> Iterator[list[ProfileExporter]]:
    """Exporters started by a test; all are stopped afterwards."""
    started: list[ProfileExporter] = []
    yield started
    for exporter in started:
        exporter.stop(timeout=5)


def export(
    exporters: list[ProfileExporter], cfg: ProfileExportConfig, **kw: Any
) -> ProfileExporter:
    options: dict[str, Any] = {"service": "unit-profile-svc"}
    options.update(kw)
    exporter = start_export(cfg, **options)
    exporters.append(exporter)
    return exporter


def body(req: RecordedRequest) -> bytes:
    data = req.body
    if (req.header("content-encoding") or "").lower() == "gzip":
        return gzip.decompress(data)
    return data


def decoded(req: RecordedRequest) -> OtlpRequest:
    return decode_otlp_profiles(body(req))


def of_type(collector: OTLPHTTPCollector, kind: str) -> list[RecordedRequest]:
    return [r for r in collector.profile_requests() if r.header("x-profile-type") == kind]


def wait_for_type(collector: OTLPHTTPCollector, kind: str, timeout: float = 5.0) -> RecordedRequest:
    wait_until(lambda: bool(of_type(collector, kind)), timeout=timeout)
    return of_type(collector, kind)[0]


# ---------------------------------------------------------------- ProfileType


def test_profile_type_values_match_go() -> None:
    assert {t.name: t.value for t in ProfileType} == {
        "CPU": "cpu",
        "WALL": "wall",
        "HEAP": "heap",
        "THREADS": "goroutine",
        "MUTEX": "mutex",
        "BLOCK": "block",
    }
    assert ProfileType.GOROUTINE is ProfileType.THREADS


def test_default_types() -> None:
    assert DEFAULT_PROFILE_TYPES == (ProfileType.CPU, ProfileType.HEAP, ProfileType.THREADS)


# ---------------------------------------------------------------- config


def test_config_defaults() -> None:
    cfg = ProfileExportConfig(endpoint="https://collector.example/v1/profiles")
    assert cfg.types == DEFAULT_PROFILE_TYPES
    assert cfg.interval == 60.0
    assert cfg.cpu_sample_duration == 10.0
    assert cfg.sample_hz == 100
    assert cfg.timeout == 10.0
    assert cfg.max_bytes == 8 << 20
    assert cfg.retries == 3
    assert cfg.allow_http is False
    assert cfg.ssl_context is None
    assert cfg.mutex_sample_fraction == 0
    assert cfg.block_sample_rate == 0.0
    assert cfg.headers == {}


def test_config_is_frozen() -> None:
    cfg = ProfileExportConfig(endpoint="https://collector.example/v1/profiles")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.interval = 1.0  # type: ignore[misc]


def test_config_accepts_timedeltas() -> None:
    cfg = ProfileExportConfig(
        endpoint="https://c.example/v1/profiles",
        interval=datetime.timedelta(seconds=30),
        cpu_sample_duration=datetime.timedelta(seconds=5),
        timeout=datetime.timedelta(milliseconds=1500),
    )
    assert (cfg.interval, cfg.cpu_sample_duration, cfg.timeout) == (30.0, 5.0, 1.5)


def test_config_types_are_coerced_and_deduplicated() -> None:
    cfg = ProfileExportConfig(
        endpoint="https://c.example/v1/profiles", types=["heap", ProfileType.HEAP, "goroutine"]
    )
    assert cfg.types == (ProfileType.HEAP, ProfileType.THREADS)


def test_empty_types_are_allowed() -> None:
    assert ProfileExportConfig(endpoint="https://c.example/v1/profiles", types=()).types == ()


def test_headers_are_copied_and_hidden_from_repr() -> None:
    headers = {"Authorization": "Bearer s3cr3t"}
    cfg = ProfileExportConfig(endpoint="https://c.example/v1/profiles", headers=headers)
    headers["Authorization"] = "changed"
    assert cfg.headers == {"Authorization": "Bearer s3cr3t"}
    assert "s3cr3t" not in repr(cfg)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"interval": 0}, "interval"),
        ({"interval": -1}, "interval"),
        ({"interval": 10, "cpu_sample_duration": 10}, "cpu_sample_duration"),
        ({"interval": 10, "cpu_sample_duration": 11}, "cpu_sample_duration"),
        ({"cpu_sample_duration": 0}, "cpu_sample_duration"),
        ({"sample_hz": 0}, "sample_hz"),
        ({"sample_hz": 1001}, "sample_hz"),
        ({"timeout": 0}, "timeout"),
        ({"max_bytes": 0}, "max_bytes"),
        ({"retries": -1}, "retries"),
        ({"mutex_sample_fraction": -1}, "mutex_sample_fraction"),
        ({"block_sample_rate": -0.5}, "block_sample_rate"),
        ({"headers": {"X-A": "a\r\nb"}}, "control"),
        ({"headers": {"": "a"}}, "headers"),
        ({"types": "cpu"}, "types"),
        ({"types": ["bogus"]}, "profile type"),
    ],
)
def test_config_validation(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ProfileExportConfig(endpoint="https://c.example/v1/profiles", **overrides)


@pytest.mark.parametrize(
    "endpoint", ["c.example/v1/profiles", "/v1/profiles", "ftp://c.example/x", "", "https://"]
)
def test_endpoint_must_be_absolute_http_url(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        ProfileExportConfig(endpoint=endpoint)


def test_plain_http_requires_allow_http() -> None:
    with pytest.raises(ValueError, match="https"):
        ProfileExportConfig(endpoint="http://c.example/v1/profiles")
    assert ProfileExportConfig(endpoint="http://c.example/v1/profiles", allow_http=True)


def test_validation_lists_every_problem() -> None:
    with pytest.raises(ValueError, match="interval") as info:
        ProfileExportConfig(endpoint="https://c.example/v1/profiles", interval=0, retries=-1)
    assert "retries" in str(info.value)


def test_stats_default_to_zero() -> None:
    assert ProfileExportStats() == ProfileExportStats(exported=0, failed=0, skipped=0)


# ---------------------------------------------------------------- uploads


def test_upload_is_a_protobuf_post_to_the_endpoint(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    export(exporters, config(otlp_collector, headers={"Authorization": "Bearer tok-u"}))
    req = otlp_collector.wait_for_profiles(1)[0]
    assert req.method == "POST"
    assert req.path == PROFILES_PATH
    assert req.header("content-type") == "application/x-protobuf"
    assert req.header("x-profile-type") == "heap"
    assert req.header("authorization") == "Bearer tok-u"


@pytest.mark.parametrize(
    ("kind", "overrides"),
    [
        (ProfileType.HEAP, {}),
        (ProfileType.THREADS, {}),
        (ProfileType.WALL, {}),
        (ProfileType.CPU, {}),
        (ProfileType.MUTEX, {"mutex_sample_fraction": 1}),
        (ProfileType.BLOCK, {"block_sample_rate": 1e-9}),
    ],
    ids=["heap", "goroutine", "wall", "cpu", "mutex", "block"],
)
def test_x_profile_type_header_and_profile_type_attribute(
    otlp_collector: OTLPHTTPCollector,
    exporters: list[ProfileExporter],
    kind: ProfileType,
    overrides: dict[str, Any],
) -> None:
    export(exporters, config(otlp_collector, types=(kind,), **overrides))
    req = wait_for_type(otlp_collector, kind.value)
    request = decoded(req)
    assert request.profiles
    assert {p.attributes.get("profile.type") for p in request.profiles} == {kind.value}


def test_resource_has_service_name_and_resource_attrs(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    export(
        exporters,
        config(otlp_collector),
        service="profiled-unit-svc",
        resource={"deployment.environment": "staging"},
    )
    request = decoded(otlp_collector.wait_for_profiles(1)[0])
    assert request.resource["service.name"] == "profiled-unit-svc"
    assert request.resource["deployment.environment"] == "staging"


def test_heap_upload_has_heap_sample_types(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    export(exporters, config(otlp_collector))
    request = decoded(otlp_collector.wait_for_profiles(1)[0])
    assert {p.sample_type for p in request.profiles} == {
        ("alloc_space", "bytes"),
        ("inuse_space", "bytes"),
        ("alloc_objects", "count"),
    }


def test_labels_are_dropped_by_default(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    export(exporters, config(otlp_collector, types=(ProfileType.THREADS,)))
    request = decoded(wait_for_type(otlp_collector, "goroutine"))
    for profile in request.profiles:
        for sample in profile.samples:
            assert sample.attribute_indices == []


def test_multiple_types_each_upload_separately(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    export(exporters, config(otlp_collector, types=(ProfileType.HEAP, ProfileType.THREADS)))
    wait_for_type(otlp_collector, "heap")
    wait_for_type(otlp_collector, "goroutine")
    assert {r.header("x-profile-type") for r in otlp_collector.profile_requests()} == {
        "heap",
        "goroutine",
    }


# ---------------------------------------------------------------- stats, retries, limits


def test_successful_uploads_are_counted(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(exporters, config(otlp_collector))
    wait_until(lambda: exporter.stats().exported >= 2)
    stats = exporter.stats()
    assert stats.failed == 0
    assert isinstance(stats, ProfileExportStats)


def test_server_errors_are_retried_up_to_retries(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    otlp_collector.fail_next(2, status=503, path=PROFILES_PATH)
    exporter = export(exporters, config(otlp_collector, retries=2, interval=2.0))
    wait_until(lambda: exporter.stats().exported >= 1)
    assert exporter.stats().failed == 0
    assert len(otlp_collector.profile_requests()) >= 3


def test_exhausted_retries_count_one_failure(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    otlp_collector.fail_next(10, status=503, path=PROFILES_PATH)
    exporter = export(exporters, config(otlp_collector, retries=2, interval=2.0))
    wait_until(lambda: exporter.stats().failed >= 1, timeout=8)
    # One initial attempt plus two retries for the first collection.
    assert len(otlp_collector.profile_requests()) == 3
    assert exporter.stats().exported == 0


def test_too_many_requests_is_retried(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    otlp_collector.fail_next(1, status=429, path=PROFILES_PATH)
    exporter = export(exporters, config(otlp_collector, retries=1, interval=2.0))
    wait_until(lambda: exporter.stats().exported >= 1)
    assert exporter.stats().failed == 0


def test_client_errors_are_not_retried(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    otlp_collector.fail_next(1, status=400, path=PROFILES_PATH)
    exporter = export(exporters, config(otlp_collector, retries=3, interval=2.0))
    wait_until(lambda: exporter.stats().failed >= 1, timeout=8)
    assert len(otlp_collector.profile_requests()) == 1


def test_no_retries_when_retries_is_zero(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    otlp_collector.fail_next(1, status=500, path=PROFILES_PATH)
    exporter = export(exporters, config(otlp_collector, retries=0, interval=2.0))
    wait_until(lambda: exporter.stats().failed >= 1, timeout=8)
    assert len(otlp_collector.profile_requests()) == 1


def test_oversized_profile_is_never_sent(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(exporters, config(otlp_collector, max_bytes=32))
    wait_until(lambda: exporter.stats().failed >= 1, timeout=8)
    assert otlp_collector.profile_requests() == []
    assert exporter.stats().exported == 0


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_not_followed(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter], status: int
) -> None:
    with OTLPHTTPCollector() as elsewhere:
        otlp_collector.fail_next(
            1, status=status, headers=[("Location", elsewhere.profiles_url)], path=PROFILES_PATH
        )
        exporter = export(exporters, config(otlp_collector, interval=2.0))
        wait_until(lambda: exporter.stats().failed >= 1, timeout=8)
        exporter.stop(timeout=5)
        assert elsewhere.requests == []
    assert exporter.stats().exported == 0


def test_request_timeout_counts_a_failure(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    otlp_collector.hang()
    try:
        exporter = export(exporters, config(otlp_collector, timeout=0.3, interval=2.0))
        wait_until(lambda: exporter.stats().failed >= 1, timeout=8)
    finally:
        otlp_collector.release()


def test_mutex_without_sampling_is_skipped(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(
        exporters, config(otlp_collector, types=(ProfileType.MUTEX,), mutex_sample_fraction=0)
    )
    wait_until(lambda: exporter.stats().skipped >= 1, timeout=8)
    assert of_type(otlp_collector, "mutex") == []


def test_block_without_sampling_is_skipped(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(
        exporters, config(otlp_collector, types=(ProfileType.BLOCK,), block_sample_rate=0.0)
    )
    wait_until(lambda: exporter.stats().skipped >= 1, timeout=8)
    assert of_type(otlp_collector, "block") == []


@pytest.mark.usefixtures("no_owner_marker")
def test_cpu_is_skipped_while_another_sampler_owns_the_process(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    assert acquire_ownership()
    try:
        exporter = export(exporters, config(otlp_collector, types=(ProfileType.CPU,)))
        wait_until(lambda: exporter.stats().skipped >= 1, timeout=8)
        assert of_type(otlp_collector, "cpu") == []
        exporter.stop(timeout=5)
    finally:
        release_ownership()


@pytest.mark.usefixtures("no_owner_marker")
def test_stop_never_stops_a_sampler_someone_else_owns(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    mine = SamplingProfiler(hz=50, cpu=False)
    assert mine.start()
    try:
        exporter = export(exporters, config(otlp_collector, types=(ProfileType.CPU,)))
        wait_until(lambda: exporter.stats().skipped >= 1, timeout=8)
        exporter.stop(timeout=5)
        assert acquire_ownership() is False, "the exporter released a sampler it did not own"
    finally:
        mine.stop()


# ---------------------------------------------------------------- lifecycle


def test_running_and_idempotent_stop(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(exporters, config(otlp_collector))
    assert exporter.running
    exporter.stop(timeout=5)
    assert not exporter.running
    exporter.stop(timeout=5)
    assert not exporter.running


def test_stop_is_prompt_even_mid_cpu_window(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(
        exporters,
        config(otlp_collector, types=(ProfileType.CPU,), interval=30.0, cpu_sample_duration=20.0),
    )
    started = time.monotonic()
    exporter.stop(timeout=5)
    assert time.monotonic() - started < 2.0
    assert not exporter.running


def test_nothing_is_sent_after_stop(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(exporters, config(otlp_collector, interval=0.1, cpu_sample_duration=0.05))
    otlp_collector.wait_for_profiles(1)
    exporter.stop(timeout=5)
    settled = len(otlp_collector.profile_requests())
    threading.Event().wait(0.4)
    assert len(otlp_collector.profile_requests()) == settled


def test_stop_leaves_no_threads(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    before = live_threads()
    exporter = export(
        exporters,
        config(otlp_collector, types=(ProfileType.HEAP, ProfileType.WALL, ProfileType.THREADS)),
    )
    otlp_collector.wait_for_profiles(1)
    exporter.stop(timeout=5)
    wait_until(lambda: new_threads_settled(before))


def test_empty_types_upload_nothing(
    otlp_collector: OTLPHTTPCollector, exporters: list[ProfileExporter]
) -> None:
    exporter = export(exporters, config(otlp_collector, types=(), interval=0.1, cpu_sample_duration=0.05))
    threading.Event().wait(0.4)
    exporter.stop(timeout=5)
    assert otlp_collector.profile_requests() == []
    assert exporter.stats() == ProfileExportStats()
