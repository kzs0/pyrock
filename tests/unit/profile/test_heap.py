"""Heap profiles from tracemalloc snapshot diffs (``pyrock.profile._heap``)."""

from __future__ import annotations

import tracemalloc
from collections.abc import Iterator

import pytest

from pyrock.profile._heap import HeapProfiler
from pyrock.profile._pprof import Profile

HEAP_TYPES = {("alloc_space", "bytes"), ("inuse_space", "bytes"), ("alloc_objects", "count")}


@pytest.fixture
def not_tracing() -> Iterator[None]:
    """Run with tracemalloc off; never leave it running."""
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc is already tracing in this process")
    yield
    if tracemalloc.is_tracing():
        tracemalloc.stop()


def _allocate_for_heap_profile(count: int = 2000, size: int = 1024) -> list[bytearray]:
    return [bytearray(size) for _ in range(count)]


def sums(profile: Profile, function: str) -> dict[str, int]:
    out = {t.type: 0 for t in profile.sample_types}
    for sample in profile.samples:
        if any(function in f.function for f in sample.stack):
            for t, value in zip(profile.sample_types, sample.values, strict=True):
                out[t.type] += value
    return out


# ---------------------------------------------------------------- tracemalloc ownership


@pytest.mark.usefixtures("not_tracing")
def test_start_starts_tracemalloc_and_stop_stops_it() -> None:
    heap = HeapProfiler()
    heap.start()
    assert tracemalloc.is_tracing()
    heap.stop()
    assert not tracemalloc.is_tracing()


@pytest.mark.usefixtures("not_tracing")
def test_default_traceback_limit_is_32() -> None:
    heap = HeapProfiler()
    heap.start()
    try:
        assert tracemalloc.get_traceback_limit() == 32
    finally:
        heap.stop()


@pytest.mark.usefixtures("not_tracing")
def test_traceback_limit_is_configurable() -> None:
    heap = HeapProfiler(traceback_limit=8)
    heap.start()
    try:
        assert tracemalloc.get_traceback_limit() == 8
    finally:
        heap.stop()


@pytest.mark.usefixtures("not_tracing")
def test_pre_started_tracemalloc_is_left_running() -> None:
    tracemalloc.start(5)
    heap = HeapProfiler()
    heap.start()
    assert tracemalloc.get_traceback_limit() == 5, "pyrock must not restart someone else's tracing"
    heap.stop()
    assert tracemalloc.is_tracing()


@pytest.mark.usefixtures("not_tracing")
def test_start_and_stop_are_idempotent() -> None:
    heap = HeapProfiler()
    heap.start()
    heap.start()
    heap.stop()
    assert not tracemalloc.is_tracing()
    heap.stop()
    assert not tracemalloc.is_tracing()


@pytest.mark.usefixtures("not_tracing")
def test_stop_after_someone_else_stopped_and_restarted() -> None:
    heap = HeapProfiler()
    heap.start()
    tracemalloc.stop()
    tracemalloc.start()  # now owned by "someone else"
    heap.stop()
    assert tracemalloc.is_tracing()


@pytest.mark.usefixtures("not_tracing")
def test_two_profilers_only_the_starter_stops() -> None:
    first, second = HeapProfiler(), HeapProfiler()
    first.start()
    second.start()
    second.stop()
    assert tracemalloc.is_tracing()
    first.stop()
    assert not tracemalloc.is_tracing()


# ---------------------------------------------------------------- snapshots


@pytest.mark.usefixtures("not_tracing")
def test_snapshot_shape() -> None:
    heap = HeapProfiler()
    heap.start()
    try:
        profile = heap.snapshot()
    finally:
        heap.stop()
    assert {(t.type, t.unit) for t in profile.sample_types} == HEAP_TYPES
    assert len(profile.sample_types) == 3
    assert all(len(s.values) == 3 for s in profile.samples)
    assert profile.start_time_ns > 0
    assert profile.duration_ns >= 0


@pytest.mark.usefixtures("not_tracing")
def test_snapshot_diff_reports_new_allocations() -> None:
    heap = HeapProfiler()
    heap.start()
    try:
        heap.snapshot()  # baseline
        retained = _allocate_for_heap_profile()
        profile = heap.snapshot()
        found = sums(profile, "_allocate_for_heap_profile")
        assert found["alloc_space"] >= 2000 * 1024
        assert found["inuse_space"] >= 2000 * 1024
        assert found["alloc_objects"] >= 2000

        del retained
        after = sums(heap.snapshot(), "_allocate_for_heap_profile")
    finally:
        heap.stop()
    # Nothing new was allocated there since the previous snapshot and it was freed.
    assert after.get("alloc_space", 0) < 100 * 1024
    assert after.get("inuse_space", 0) < 100 * 1024


@pytest.mark.usefixtures("not_tracing")
def test_retained_memory_stays_in_use_without_new_allocations() -> None:
    heap = HeapProfiler()
    heap.start()
    try:
        heap.snapshot()
        retained = _allocate_for_heap_profile()
        heap.snapshot()
        steady = sums(heap.snapshot(), "_allocate_for_heap_profile")
        assert len(retained) == 2000
    finally:
        heap.stop()
    assert steady.get("alloc_space", 0) < 100 * 1024
    assert steady.get("inuse_space", 0) >= 2000 * 1024


@pytest.mark.usefixtures("not_tracing")
def test_heap_stacks_are_leaf_first_with_source_lines() -> None:
    heap = HeapProfiler()
    heap.start()
    try:
        heap.snapshot()
        retained = _allocate_for_heap_profile()
        profile = heap.snapshot()
    finally:
        heap.stop()
    stacks = [
        s.stack
        for s in profile.samples
        if any("_allocate_for_heap_profile" in f.function for f in s.stack)
    ]
    assert stacks
    for stack in stacks:
        names = [f.function for f in stack]
        target = next(i for i, n in enumerate(names) if "_allocate_for_heap_profile" in n)
        caller = next(i for i, n in enumerate(names) if "test_heap_stacks_are_leaf_first" in n)
        assert target < caller, names
        assert stack[target].filename.endswith("test_heap.py")
        assert stack[target].line > 0
    del retained
