"""RuntimeCollector: documented families and types, gc counters, python_info, /proc parsing.

DESIGN §10.2; Go ``metric/runtime.go`` (Python equivalents, Prometheus client names).
"""

from __future__ import annotations

import asyncio
import gc
import sys
import threading
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from pyrock.metric import MetricType, Registry, runtime
from pyrock.metric.runtime import RuntimeCollector
from tests.unit.metric._helpers import family, samples, value

PYTHON_FAMILIES = {
    "python_gc_collections_total": MetricType.COUNTER,
    "python_gc_collected_total": MetricType.COUNTER,
    "python_gc_uncollectable_total": MetricType.COUNTER,
    "python_gc_pause_seconds": MetricType.HISTOGRAM,
    "python_threads": MetricType.GAUGE,
    "python_info": MetricType.GAUGE,
}

PROCESS_FAMILIES = {
    "process_cpu_seconds_total": MetricType.COUNTER,
    "process_resident_memory_bytes": MetricType.GAUGE,
    "process_virtual_memory_bytes": MetricType.GAUGE,
    "process_open_fds": MetricType.GAUGE,
    "process_max_fds": MetricType.GAUGE,
    "process_start_time_seconds": MetricType.GAUGE,
}


@pytest.fixture
def registry() -> Registry:
    return Registry()


@pytest.fixture
def collector(registry: Registry) -> Iterator[RuntimeCollector]:
    rc = RuntimeCollector(registry)
    try:
        yield rc
    finally:
        rc.close()


def _collect(rc: RuntimeCollector, registry: Registry) -> dict[str, MetricType]:
    rc.collect()
    return {f.name: f.type for f in registry.collect() if f.samples}


def _total(registry: Registry, name: str) -> float:
    return sum(s.value for s in samples(registry, name))


# ------------------------------------------------------------------ families


def test_python_families_have_documented_types(
    collector: RuntimeCollector, registry: Registry
) -> None:
    gc.collect()
    found = _collect(collector, registry)
    for name, kind in PYTHON_FAMILIES.items():
        assert found.get(name) == kind, name


@pytest.mark.linux_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc metrics")
def test_process_families_on_linux(collector: RuntimeCollector, registry: Registry) -> None:
    found = _collect(collector, registry)
    for name, kind in PROCESS_FAMILIES.items():
        assert found.get(name) == kind, name
        assert value(registry, name) > 0, name


def test_gc_collections_per_generation(collector: RuntimeCollector, registry: Registry) -> None:
    _collect(collector, registry)
    generations = {s.labels["generation"] for s in samples(registry, "python_gc_collections_total")}
    assert generations == {str(g) for g in range(len(gc.get_stats()))}


def test_gc_counters_are_monotonic_and_track_collections(
    collector: RuntimeCollector, registry: Registry
) -> None:
    _collect(collector, registry)
    before = value(registry, "python_gc_collections_total", {"generation": "2"})
    collected_before = _total(registry, "python_gc_collected_total")
    for _ in range(3):
        cycle: list[object] = []
        cycle.append(cycle)
        del cycle
        gc.collect()
    _collect(collector, registry)
    after = value(registry, "python_gc_collections_total", {"generation": "2"})
    assert after >= before + 3
    assert _total(registry, "python_gc_collected_total") >= collected_before + 3
    # Repeated collections never go backwards.
    _collect(collector, registry)
    assert value(registry, "python_gc_collections_total", {"generation": "2"}) >= after


def test_gc_counters_match_gc_get_stats(collector: RuntimeCollector, registry: Registry) -> None:
    gc.disable()
    try:
        stats = gc.get_stats()
        _collect(collector, registry)
        for gen, stat in enumerate(stats):
            got = value(registry, "python_gc_collections_total", {"generation": str(gen)})
            assert got == stat["collections"]
    finally:
        gc.enable()


def test_gc_pause_histogram_observes_collections(
    collector: RuntimeCollector, registry: Registry
) -> None:
    _collect(collector, registry)
    fam = family(registry, "python_gc_pause_seconds")
    before = sum(s.count for s in fam.samples) if fam else 0
    gc.collect()
    gc.collect()
    _collect(collector, registry)
    pauses = samples(registry, "python_gc_pause_seconds")
    assert sum(s.count for s in pauses) >= before + 2
    assert all(s.sum >= 0 for s in pauses)


def test_close_removes_gc_callback_and_is_idempotent(registry: Registry) -> None:
    callbacks_before = list(gc.callbacks)
    rc = RuntimeCollector(registry)
    assert len(gc.callbacks) == len(callbacks_before) + 1
    rc.close()
    rc.close()
    assert gc.callbacks == callbacks_before
    rc.collect()
    count = sum(s.count for s in samples(registry, "python_gc_pause_seconds"))
    gc.collect()
    rc.collect()
    assert sum(s.count for s in samples(registry, "python_gc_pause_seconds")) == count


def test_python_threads_counts_live_threads(
    collector: RuntimeCollector, registry: Registry
) -> None:
    release = threading.Event()
    started = threading.Barrier(4)

    def park() -> None:
        started.wait()
        release.wait()

    threads = [threading.Thread(target=park, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        started.wait()
        _collect(collector, registry)
        assert value(registry, "python_threads") >= 4
    finally:
        release.set()
        for t in threads:
            t.join()


def test_python_info_labels(collector: RuntimeCollector, registry: Registry) -> None:
    _collect(collector, registry)
    (info,) = samples(registry, "python_info")
    assert info.value == 1
    assert info.labels["implementation"] == sys.implementation.name
    major, minor = sys.version_info[:2]
    assert info.labels["version"].startswith(f"{major}.{minor}.")
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    expected_gil = "true" if is_gil_enabled is None or is_gil_enabled() else "false"
    assert info.labels["gil"] == expected_gil


def test_runtime_families_carry_const_labels() -> None:
    reg = Registry(const_labels={"env": "prod"})
    rc = RuntimeCollector(reg)
    try:
        rc.collect()
        for name in ("python_threads", "python_info", "python_gc_collections_total"):
            assert all(s.labels.get("env") == "prod" for s in samples(reg, name)), name
    finally:
        rc.close()


def test_runtime_families_use_registry_prefix() -> None:
    reg = Registry("myapp")
    rc = RuntimeCollector(reg)
    try:
        rc.collect()
        assert {f.name for f in reg.collect() if f.samples} >= {
            "myapp_python_threads",
            "myapp_python_info",
        }
    finally:
        rc.close()


# ------------------------------------------------------------------ asyncio


def test_asyncio_tasks_counts_tracked_loop_tasks(
    collector: RuntimeCollector, registry: Registry
) -> None:
    async def main() -> float:
        collector.track_loop(asyncio.get_running_loop())
        gate = asyncio.Event()
        tasks = [asyncio.create_task(gate.wait()) for _ in range(3)]
        await asyncio.sleep(0)
        collector.collect()
        got = _total(registry, "python_asyncio_tasks")
        gate.set()
        await asyncio.gather(*tasks)
        return got

    assert asyncio.run(main()) >= 3


def test_asyncio_tasks_family_is_a_gauge(collector: RuntimeCollector, registry: Registry) -> None:
    async def main() -> None:
        collector.track_loop(asyncio.get_running_loop())
        collector.collect()

    asyncio.run(main())
    fam = family(registry, "python_asyncio_tasks")
    assert fam is not None
    assert fam.type == MetricType.GAUGE


def test_closed_tracked_loop_is_ignored(collector: RuntimeCollector, registry: Registry) -> None:
    loop = asyncio.new_event_loop()
    collector.track_loop(loop)
    loop.close()
    del loop
    gc.collect()
    collector.collect()  # must not raise
    assert _total(registry, "python_asyncio_tasks") == 0


# --------------------------------------------------------------- tracemalloc


def test_tracemalloc_bytes_only_while_tracing(
    collector: RuntimeCollector, registry: Registry
) -> None:
    was_tracing = tracemalloc.is_tracing()
    if was_tracing:  # pragma: no cover - depends on the test runner
        pytest.skip("tracemalloc already tracing")
    _collect(collector, registry)
    assert samples(registry, "python_tracemalloc_traced_bytes") == []
    tracemalloc.start()
    try:
        keep = [bytes(1024) for _ in range(64)]
        _collect(collector, registry)
        assert value(registry, "python_tracemalloc_traced_bytes") > 0
        del keep
    finally:
        tracemalloc.stop()


# ------------------------------------------------------------- /proc parsing

CLOCK_TICKS = 100
PAGE_SIZE = 4096


def _fake_proc(
    root: Path,
    *,
    comm: str = "python3",
    limits: bool = True,
    fds: int = 3,
    statm: bool = True,
) -> Path:
    me = root / "self"
    (me / "fd").mkdir(parents=True)
    for fd in range(fds):
        (me / "fd" / str(fd)).write_text("")
    # Fields after the comm: state ppid pgrp session tty tpgid flags minflt cminflt majflt
    # cmajflt utime(14) stime(15) cutime cstime priority nice threads itrealvalue
    # starttime(22) vsize(23) rss(24) ...
    after = [
        "S", "1", "1", "1", "0", "-1", "4194304", "100", "0", "0", "0",
        "250", "150", "7", "3", "20", "0", "4", "0",
        "5000", str(1000 * PAGE_SIZE), "250",
        "18446744073709551615", "1", "1", "0", "0", "0", "0", "0",
    ]  # fmt: skip
    (me / "stat").write_text(f"4242 ({comm}) " + " ".join(after) + "\n")
    if statm:
        (me / "statm").write_text("1000 250 100 10 0 300 0\n")
    if limits:
        (me / "limits").write_text(
            "Limit                     Soft Limit           Hard Limit           Units     \n"
            "Max cpu time              unlimited            unlimited            seconds   \n"
            "Max open files            1024                 524288               files     \n"
            "Max processes             63432                63432                processes \n"
        )
    (root / "stat").write_text(
        "cpu  1 2 3 4 5 6 7 0 0 0\nintr 0\nctxt 1\nbtime 1700000000\nprocesses 9\n"
    )
    return root


def _read(root: Path) -> dict[str, float]:
    return runtime._read_proc(str(root), clock_ticks=CLOCK_TICKS, page_size=PAGE_SIZE)


def test_read_proc_parses_a_fake_proc_tree(tmp_path: Path) -> None:
    got = _read(_fake_proc(tmp_path))
    assert got == {
        "process_cpu_seconds_total": pytest.approx((250 + 150) / CLOCK_TICKS),
        "process_virtual_memory_bytes": 1000 * PAGE_SIZE,
        "process_resident_memory_bytes": 250 * PAGE_SIZE,
        "process_start_time_seconds": pytest.approx(1700000000 + 5000 / CLOCK_TICKS),
        "process_open_fds": 3,
        "process_max_fds": 1024,
    }


@pytest.mark.parametrize("comm", ["my proc", "weird) (name", "a) S 1 2 (b"])
def test_read_proc_handles_spaces_and_parens_in_comm(tmp_path: Path, comm: str) -> None:
    got = _read(_fake_proc(tmp_path, comm=comm))
    assert got["process_cpu_seconds_total"] == pytest.approx(4.0)
    assert got["process_virtual_memory_bytes"] == 1000 * PAGE_SIZE


def test_read_proc_omits_only_metrics_from_missing_files(tmp_path: Path) -> None:
    got = _read(_fake_proc(tmp_path, limits=False))
    assert "process_max_fds" not in got
    assert got["process_open_fds"] == 3
    assert got["process_resident_memory_bytes"] == 250 * PAGE_SIZE


def test_read_proc_unlimited_max_fds_is_omitted_or_infinite(tmp_path: Path) -> None:
    root = _fake_proc(tmp_path)
    limits = root / "self" / "limits"
    limits.write_text(limits.read_text().replace("1024    ", "unlimited"))
    got = _read(root)
    assert got.get("process_max_fds", float("inf")) == float("inf")


def test_read_proc_garbage_stat_does_not_raise(tmp_path: Path) -> None:
    root = _fake_proc(tmp_path)
    (root / "self" / "stat").write_text("not a stat line")
    got = _read(root)
    assert "process_cpu_seconds_total" not in got
    assert got["process_open_fds"] == 3


def test_read_proc_missing_root_is_empty(tmp_path: Path) -> None:
    assert _read(tmp_path / "does-not-exist") == {}


def test_read_proc_other_pid(tmp_path: Path) -> None:
    root = _fake_proc(tmp_path)
    (root / "self").rename(root / "4242")
    got = runtime._read_proc(str(root), pid="4242", clock_ticks=CLOCK_TICKS, page_size=PAGE_SIZE)
    assert got["process_open_fds"] == 3


def test_collect_survives_absent_proc(
    monkeypatch: pytest.MonkeyPatch, collector: RuntimeCollector, registry: Registry
) -> None:
    """Non-Linux (or a sandbox without /proc): python_* families still work."""
    monkeypatch.setattr(runtime, "_read_proc", lambda *a, **k: {})
    found = _collect(collector, registry)
    assert found.get("python_threads") == MetricType.GAUGE
    assert found.get("python_info") == MetricType.GAUGE
    for name in ("process_open_fds", "process_max_fds"):
        assert samples(registry, name) == [] or value(registry, name) >= 0


def test_collect_uses_proc_values(
    monkeypatch: pytest.MonkeyPatch, collector: RuntimeCollector, registry: Registry
) -> None:
    fake = {
        "process_cpu_seconds_total": 4.0,
        "process_virtual_memory_bytes": 4096000.0,
        "process_resident_memory_bytes": 1024000.0,
        "process_start_time_seconds": 1700000050.0,
        "process_open_fds": 3.0,
        "process_max_fds": 1024.0,
    }
    monkeypatch.setattr(runtime, "_read_proc", lambda *a, **k: dict(fake))
    _collect(collector, registry)
    for name in (
        "process_virtual_memory_bytes",
        "process_resident_memory_bytes",
        "process_start_time_seconds",
        "process_open_fds",
        "process_max_fds",
    ):
        assert value(registry, name) == fake[name], name
