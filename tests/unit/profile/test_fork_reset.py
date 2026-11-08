"""``pyrock.profile._after_fork_in_child()``: profiling state is usable in a forked child.

Each scenario holds a profiling lock (or ownership) in the parent at fork time,
as a sampler window, a contended recorder or a heap snapshot would, and checks
that the child can profile again without deadlocking.
"""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import threading
import time
import traceback
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

import pyrock.profile
from pyrock.profile import _heap, _locks, _sampler

pytestmark = [
    pytest.mark.fork,
    pytest.mark.linux_only,
    pytest.mark.skipif(
        not (hasattr(os, "fork") and sys.platform.startswith("linux")),
        reason="needs os.fork on Linux",
    ),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

CHILD_TIMEOUT = 10.0


def in_child(scenario: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Fork, run ``scenario`` in the child, and return what it reported (a hang fails)."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        code = 0
        try:
            payload: dict[str, Any] = {"ok": True, **scenario()}
        except BaseException:
            payload = {"ok": False, "error": traceback.format_exc()}
            code = 1
        try:
            os.write(write_fd, json.dumps(payload).encode())
        finally:
            os._exit(code)
    os.close(write_fd)
    chunks: list[bytes] = []
    deadline = time.monotonic() + CHILD_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.kill(pid, signal.SIGKILL)
                raise AssertionError("forked child hung (deadlock?)")
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)
    result: dict[str, Any] = json.loads(b"".join(chunks) or b"{}")
    assert result.get("ok"), result.get("error", "child reported nothing")
    return result


@contextmanager
def held(get_lock: Callable[[], Any]) -> Iterator[None]:
    """Hold ``get_lock()`` on another thread for the duration of the block."""
    acquired, release = threading.Event(), threading.Event()

    def hold() -> None:
        with get_lock():
            acquired.set()
            release.wait()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert acquired.wait(5)
    try:
        yield
    finally:
        release.set()
        thread.join(5)


@pytest.fixture
def not_tracing() -> Iterator[None]:
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already tracing in this process")
    yield
    if tracemalloc.is_tracing():
        tracemalloc.stop()


# ---------------------------------------------------------------- sampler


def test_sampler_slot_held_at_fork_is_free_in_the_child() -> None:
    assert _sampler.acquire_ownership()
    try:
        with held(lambda: _sampler._owner_lock):

            def child() -> dict[str, Any]:
                pyrock.profile._after_fork_in_child()
                first = _sampler.acquire_ownership()
                second = _sampler.acquire_ownership()
                _sampler.release_ownership()
                return {"first": first, "second": second}

            result = in_child(child)
    finally:
        _sampler.release_ownership()
    assert (result["first"], result["second"]) == (True, False)


def test_child_cpu_collection_works_after_a_parent_window() -> None:
    assert _sampler.acquire_ownership()  # a CPU window is open in the parent
    try:

        def child() -> dict[str, Any]:
            pyrock.profile._after_fork_in_child()
            profile = _sampler.SamplingProfiler(cpu=False).collect(0.05)
            return {"collected": profile is not None}

        assert in_child(child)["collected"] is True
    finally:
        _sampler.release_ownership()
    assert _sampler.acquire_ownership()  # the parent's claim was released normally
    _sampler.release_ownership()


def test_reset_keeps_a_claim_made_by_the_child_itself() -> None:
    def child() -> dict[str, Any]:
        assert _sampler.acquire_ownership()
        pyrock.profile._after_fork_in_child()
        return {"again": _sampler.acquire_ownership()}

    assert in_child(child)["again"] is False


# ---------------------------------------------------------------- locks


@pytest.mark.parametrize("kind", ["mutex", "block"])
def test_recorder_lock_held_at_fork_does_not_deadlock(kind: str) -> None:
    def recorder() -> Any:
        return _locks._mutex if kind == "mutex" else _locks._block

    recorder().record(sys._getframe(), 1, 10)
    with held(lambda: recorder().lock):

        def child() -> dict[str, Any]:
            pyrock.profile._after_fork_in_child()
            empty = recorder().profile(1).samples == ()
            recorder().record(sys._getframe(), 2, 20)
            _locks.reset()
            return {"empty": empty, "after_reset": len(recorder().profile(1).samples)}

        result = in_child(child)
    _locks.reset()
    assert result == {"ok": True, "empty": True, "after_reset": 0}


def test_contention_settings_survive_the_reset() -> None:
    before = _locks.settings()
    _locks.configure(mutex_sample_fraction=5, block_sample_rate=0.25)
    try:

        def child() -> dict[str, Any]:
            pyrock.profile._after_fork_in_child()
            return {"settings": list(_locks.settings())}

        assert in_child(child)["settings"] == [5, 0.25]
    finally:
        _locks.configure(mutex_sample_fraction=before[0], block_sample_rate=before[1])


# ---------------------------------------------------------------- heap


def test_child_stops_tracing_pyrock_started(not_tracing: None) -> None:
    profiler = _heap.HeapProfiler()
    profiler.start()
    try:

        def child() -> dict[str, Any]:
            pyrock.profile._after_fork_in_child()
            tracing = tracemalloc.is_tracing()
            fresh = _heap.HeapProfiler()
            fresh.start()  # a child exporter selecting HEAP starts and owns it again
            restarted = tracemalloc.is_tracing()
            fresh.stop()
            return {
                "tracing": tracing,
                "restarted": restarted,
                "stopped": not tracemalloc.is_tracing(),
            }

        result = in_child(child)
        assert tracemalloc.is_tracing(), "the parent keeps tracing"
    finally:
        profiler.stop()
    assert not tracemalloc.is_tracing()
    assert (result["tracing"], result["restarted"], result["stopped"]) == (False, True, True)


def test_stale_parent_profiler_never_stops_child_tracing(not_tracing: None) -> None:
    profiler = _heap.HeapProfiler()
    profiler.start()
    try:

        def child() -> dict[str, Any]:
            pyrock.profile._after_fork_in_child()
            tracemalloc.start()  # the user's own tracing in the child
            profiler.stop()  # the parent's (inherited) owner object
            return {"tracing": tracemalloc.is_tracing()}

        assert in_child(child)["tracing"] is True
    finally:
        profiler.stop()


def test_child_keeps_tracing_someone_else_started(not_tracing: None) -> None:
    tracemalloc.start()
    try:

        def child() -> dict[str, Any]:
            pyrock.profile._after_fork_in_child()
            return {"tracing": tracemalloc.is_tracing()}

        assert in_child(child)["tracing"] is True
    finally:
        tracemalloc.stop()


def test_child_profiler_started_before_the_reset_adopts_tracing(not_tracing: None) -> None:
    profiler = _heap.HeapProfiler()
    profiler.start()
    try:

        def child() -> dict[str, Any]:
            fresh = _heap.HeapProfiler()
            fresh.start()  # adopts the parent's pyrock tracing
            pyrock.profile._after_fork_in_child()
            kept = tracemalloc.is_tracing()
            fresh.stop()
            return {"kept": kept, "stopped": not tracemalloc.is_tracing()}

        result = in_child(child)
    finally:
        profiler.stop()
    assert (result["kept"], result["stopped"]) == (True, True)


def test_heap_locks_held_at_fork_are_replaced(not_tracing: None) -> None:
    profiler = _heap.HeapProfiler()
    with held(lambda: _heap._tracing_lock), held(lambda: profiler._lock):

        def child() -> dict[str, Any]:
            pyrock.profile._after_fork_in_child()
            profiler.start()
            snapshot = profiler.snapshot()
            profiler.stop()
            return {"types": [t.type for t in snapshot.sample_types]}

        result = in_child(child)
    assert "inuse_space" in result["types"]
