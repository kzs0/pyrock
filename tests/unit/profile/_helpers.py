"""Workloads and helpers shared by the ``pyrock.profile`` unit tests.

The workload function names are unique so tests can find them in stacks.
"""

from __future__ import annotations

import sys
import threading
from types import FrameType

import pytest

from pyrock.profile._pprof import Frame, Profile, Sample, SampleType
from tests.support.collectors import wait_until

LINUX = sys.platform.startswith("linux")
needs_linux = pytest.mark.skipif(not LINUX, reason="per-thread CPU clocks are Linux-only")


def _parked_worker_for_unit_profile(release: threading.Event) -> None:
    release.wait(10)


def _busy_spin_for_unit_profile(stop: threading.Event) -> None:
    n = 0
    while not stop.is_set():
        for i in range(2000):
            n += i * i
    del n


def live_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.is_alive()}


def new_threads_settled(before: set[threading.Thread]) -> bool:
    """Whether every thread started since ``before`` has exited."""
    return not live_threads() - before


class FakeClock:
    """A monotonic nanosecond clock that only moves when told to (or slept on)."""

    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.now = start_ns
        self.sleeps: list[float] = []

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += round(seconds * 1e9)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


def frame_names(frame: FrameType | None) -> list[str]:
    """Function names of ``frame`` and its callers, leaf first."""
    names = []
    while frame is not None:
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    return names


def thread_frames(thread: threading.Thread) -> list[str]:
    assert thread.ident is not None
    return frame_names(sys._current_frames().get(thread.ident))


def wait_in_function(thread: threading.Thread, name: str, timeout: float = 5.0) -> None:
    """Wait until ``thread``'s stack contains the function ``name``."""
    wait_until(lambda: name in thread_frames(thread), timeout=timeout)


# ---------------------------------------------------------------- a fixed profile

LEAF = Frame("leaf_fn", "/srv/app/leaf.py", 10, "app.leaf")
LEAF_OTHER_LINE = Frame("leaf_fn", "/srv/app/leaf.py", 12, "app.leaf")
MID = Frame("mid_fn", "/srv/app/mid.py", 20, "app.mid")
ROOT = Frame("root_fn", "/srv/app/root.py", 30, "app.root")

START_NS = 1_700_000_000_123_456_789
DURATION_NS = 2_000_000_000


def make_profile(**overrides: object) -> Profile:
    fields: dict[str, object] = {
        "sample_types": (SampleType("samples", "count"), SampleType("cpu", "nanoseconds")),
        "samples": (
            Sample((LEAF, MID, ROOT), (3, 30_000_000), {"thread.name": "worker", "task.id": 7}),
            Sample((LEAF_OTHER_LINE, MID, ROOT), (1, 10_000_000)),
            Sample((MID, ROOT), (2, 20_000_000)),
        ),
        "start_time_ns": START_NS,
        "duration_ns": DURATION_NS,
        "period_type": SampleType("cpu", "nanoseconds"),
        "period": 10_000_000,
        "comments": ("pyrock unit test",),
    }
    fields.update(overrides)
    return Profile(**fields)  # type: ignore[arg-type]
