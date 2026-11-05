"""The wall/CPU sampling profiler (``pyrock.profile._sampler``).

Most tests drive the sampler through its seams (a frames source, a CPU clock,
a fake monotonic clock and sleep) so tick counts and values are exact. A few
use the real defaults against real threads.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from types import FrameType

import pytest

from pyrock.profile import _sampler
from pyrock.profile._pprof import Profile
from pyrock.profile._sampler import (
    OWNER_ENV,
    SamplingProfiler,
    acquire_ownership,
    cpu_clock_available,
    release_ownership,
)
from tests.support.collectors import wait_until
from tests.unit.profile._helpers import (
    LINUX,
    FakeClock,
    live_threads,
    needs_linux,
    new_threads_settled,
)

pytestmark = pytest.mark.usefixtures("no_owner_marker")


# ---------------------------------------------------------------- helpers


def frames_of(*threads: threading.Thread) -> Callable[[], Mapping[int, FrameType]]:
    """A frames source that reports only ``threads`` (their real current frames)."""

    def source() -> Mapping[int, FrameType]:
        current = sys._current_frames()
        return {t.ident: current[t.ident] for t in threads if t.ident in current}

    return source


def totals(profile: Profile, function: str) -> list[int]:
    """Per-column value totals over samples whose stack contains ``function``."""
    out = [0] * len(profile.sample_types)
    for sample in profile.samples:
        if any(function in frame.function for frame in sample.stack):
            for i, value in enumerate(sample.values):
                out[i] += value
    return out


def has_function(profile: Profile, function: str) -> bool:
    return any(function in f.function for s in profile.samples for f in s.stack)


def column(profile: Profile, type_: str) -> int:
    names = [t.type for t in profile.sample_types]
    assert type_ in names, names
    return names.index(type_)


def sample_types(profile: Profile) -> list[tuple[str, str]]:
    return [(t.type, t.unit) for t in profile.sample_types]


@pytest.fixture
def make_threads() -> Iterator[Callable[[str], threading.Thread]]:
    """Start parked threads running uniquely named functions."""
    releases: list[threading.Event] = []
    threads: list[threading.Thread] = []

    def make(name: str) -> threading.Thread:
        release = threading.Event()
        code = f"def {name}(release):\n    release.wait(10)\n"
        namespace: dict[str, object] = {}
        exec(code, namespace)
        thread = threading.Thread(target=namespace[name], args=(release,), daemon=True)
        thread.start()
        releases.append(release)
        threads.append(thread)
        wait_until(lambda: _in_function(thread, name))
        return thread

    yield make
    for release in releases:
        release.set()
    for thread in threads:
        thread.join(5)


def _in_function(thread: threading.Thread, name: str) -> bool:
    frame = sys._current_frames().get(thread.ident or -1)
    while frame is not None:
        if frame.f_code.co_name == name:
            return True
        frame = frame.f_back
    return False


# ---------------------------------------------------------------- platform


@pytest.mark.linux_only
@needs_linux
def test_cpu_clock_available_on_linux() -> None:
    assert cpu_clock_available() is True


def test_cpu_clock_available_is_a_bool() -> None:
    assert isinstance(cpu_clock_available(), bool)


def test_owner_env_name() -> None:
    assert OWNER_ENV == "PYROCK_PROFILER_OWNER"


# ---------------------------------------------------------------- ownership


def test_ownership_is_exclusive() -> None:
    assert acquire_ownership() is True
    try:
        assert acquire_ownership() is False
    finally:
        release_ownership()
    assert acquire_ownership() is True
    release_ownership()


def test_owner_marker_in_environment_blocks_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OWNER_ENV, "another-profiler")
    assert acquire_ownership() is False
    profiler = SamplingProfiler(hz=50)
    assert profiler.start() is False
    assert profiler.collect(0.01) is None


def test_second_sampler_is_skipped_never_stopped(parked_thread: threading.Thread) -> None:
    first = SamplingProfiler(hz=200, cpu=False, frames=frames_of(parked_thread))
    second = SamplingProfiler(hz=200, cpu=False, frames=frames_of(parked_thread))
    assert first.start() is True
    try:
        assert second.start() is False
        assert second.collect(0.01) is None
        assert acquire_ownership() is False
    finally:
        profile = first.stop()
    assert has_function(profile, "_parked_worker_for_unit_profile")
    # Ownership returns once the first sampler stops.
    assert second.start() is True
    second.stop()


def test_collect_releases_ownership() -> None:
    clock = FakeClock()
    profiler = SamplingProfiler(hz=10, cpu=False, frames=dict, clock=clock, sleep=clock.sleep)
    assert profiler.collect(0.5) is not None
    assert acquire_ownership() is True
    release_ownership()


# ---------------------------------------------------------------- lifecycle


def test_start_and_stop_are_idempotent_and_leak_no_threads(
    parked_thread: threading.Thread,
) -> None:
    before = live_threads()
    profiler = SamplingProfiler(hz=100, cpu=False)
    assert profiler.start() is True
    assert profiler.start() is True
    assert len(live_threads() - before) == 1, "a second start() must not add a thread"
    first = profiler.stop()
    second = profiler.stop()
    assert isinstance(first, Profile)
    assert isinstance(second, Profile)
    wait_until(lambda: new_threads_settled(before))
    assert acquire_ownership() is True
    release_ownership()


def test_stop_without_start_returns_an_empty_profile() -> None:
    profile = SamplingProfiler(hz=100).stop()
    assert profile.samples == ()


def test_stop_is_prompt() -> None:
    profiler = SamplingProfiler(hz=1)  # one tick per second: stop must not wait for it
    assert profiler.start() is True
    done = threading.Event()
    stopper = threading.Thread(target=lambda: (profiler.stop(), done.set()), daemon=True)
    stopper.start()
    assert done.wait(1.0), "stop() waited for the next tick"
    stopper.join(1)


# ---------------------------------------------------------------- sample types


def test_cpu_profile_shape() -> None:
    clock = FakeClock()
    profile = SamplingProfiler(
        hz=100, cpu=True, frames=dict, clock=clock, sleep=clock.sleep
    ).collect(0.05)
    assert profile is not None
    assert sample_types(profile) == [("samples", "count"), ("cpu", "nanoseconds")]
    assert profile.period == 10_000_000
    assert profile.period_type is not None
    assert profile.period_type.unit == "nanoseconds"


def test_wall_profile_shape() -> None:
    clock = FakeClock()
    profile = SamplingProfiler(
        hz=50, cpu=False, frames=dict, clock=clock, sleep=clock.sleep
    ).collect(0.1)
    assert profile is not None
    assert sample_types(profile) == [("samples", "count"), ("wall", "nanoseconds")]
    assert profile.period == 20_000_000


def test_duration_reflects_the_clock() -> None:
    clock = FakeClock()
    profile = SamplingProfiler(
        hz=10, cpu=False, frames=dict, clock=clock, sleep=clock.sleep
    ).collect(2.0)
    assert profile is not None
    assert profile.duration_ns == pytest.approx(2_000_000_000, rel=0.1)
    assert profile.start_time_ns > 0


# ---------------------------------------------------------------- rate


@pytest.mark.parametrize(("hz", "seconds"), [(100, 1.0), (10, 2.0), (250, 0.2)])
def test_sample_rate_is_honored(
    hz: int, seconds: float, make_threads: Callable[[str], threading.Thread]
) -> None:
    thread = make_threads(f"_rate_target_{hz}")
    clock = FakeClock()
    profiler = SamplingProfiler(
        hz=hz, cpu=False, frames=frames_of(thread), clock=clock, sleep=clock.sleep
    )
    profile = profiler.collect(seconds)
    assert profile is not None
    expected = hz * seconds
    count = totals(profile, f"_rate_target_{hz}")[column(profile, "samples")]
    assert abs(count - expected) <= 1, (count, expected)
    assert clock.sleeps, "ticks are separated by sleep()"
    assert all(s == pytest.approx(1 / hz) for s in clock.sleeps)


def test_wall_values_are_time_between_ticks(
    make_threads: Callable[[str], threading.Thread],
) -> None:
    thread = make_threads("_wall_value_target")
    clock = FakeClock()
    profile = SamplingProfiler(
        hz=100, cpu=False, frames=frames_of(thread), clock=clock, sleep=clock.sleep
    ).collect(0.5)
    assert profile is not None
    sums = totals(profile, "_wall_value_target")
    count, wall = sums[column(profile, "samples")], sums[column(profile, "wall")]
    assert count >= 49
    assert wall == pytest.approx(count * 10_000_000, rel=0.05)


# ---------------------------------------------------------------- CPU filter


def test_cpu_mode_keeps_only_threads_whose_cpu_clock_advanced(
    make_threads: Callable[[str], threading.Thread],
) -> None:
    busy = make_threads("_cpu_advancing_target")
    idle = make_threads("_cpu_frozen_target")
    clock = FakeClock()

    def cpu_clock(ident: int) -> int | None:
        if ident == busy.ident:
            return clock.now  # advances 10 ms per tick
        return 5_000_000  # never advances

    profiler = SamplingProfiler(
        hz=100, cpu=True, frames=frames_of(busy, idle), cpu_clock=cpu_clock, clock=clock
    )
    for _ in range(4):
        profiler.sample_once()
        clock.advance(0.01)
    profile = profiler.stop()
    assert not has_function(profile, "_cpu_frozen_target")
    sums = totals(profile, "_cpu_advancing_target")
    count, cpu = sums[column(profile, "samples")], sums[column(profile, "cpu")]
    assert count in (3, 4)  # the first reading may be a baseline only
    assert cpu == pytest.approx(count * 10_000_000, abs=10_000_000)
    assert "profile.note" not in profile.attrs


def test_wall_mode_keeps_idle_threads(make_threads: Callable[[str], threading.Thread]) -> None:
    busy = make_threads("_wall_busy_target")
    idle = make_threads("_wall_idle_target")
    clock = FakeClock()
    profiler = SamplingProfiler(
        hz=100,
        cpu=False,
        frames=frames_of(busy, idle),
        cpu_clock=lambda ident: clock.now if ident == busy.ident else 0,
        clock=clock,
    )
    for _ in range(3):
        profiler.sample_once()
        clock.advance(0.01)
    profile = profiler.stop()
    assert has_function(profile, "_wall_busy_target")
    assert has_function(profile, "_wall_idle_target")


def test_missing_cpu_clock_falls_back_to_wall_and_says_so(
    make_threads: Callable[[str], threading.Thread],
) -> None:
    thread = make_threads("_fallback_target")
    clock = FakeClock()
    profiler = SamplingProfiler(
        hz=100,
        cpu=True,
        frames=frames_of(thread),
        cpu_clock=lambda ident: None,
        clock=clock,
        sleep=clock.sleep,
    )
    profile = profiler.collect(0.1)
    assert profile is not None
    assert "profile.note" in profile.attrs
    assert has_function(profile, "_fallback_target")


def test_sample_once_accumulates_until_stop(
    make_threads: Callable[[str], threading.Thread],
) -> None:
    thread = make_threads("_accumulate_target")
    clock = FakeClock()
    profiler = SamplingProfiler(hz=100, cpu=False, frames=frames_of(thread), clock=clock)
    for _ in range(5):
        profiler.sample_once()
        clock.advance(0.01)
    profile = profiler.stop()
    assert totals(profile, "_accumulate_target")[column(profile, "samples")] == 5
    assert profiler.stop().samples == ()  # stop() hands the buffer over and clears it


# ---------------------------------------------------------------- self exclusion


def _sample_from_this_frame(profiler: SamplingProfiler) -> None:
    profiler.sample_once()


@pytest.mark.parametrize("exclude_self", [True, False])
def test_exclude_self(exclude_self: bool) -> None:
    profiler = SamplingProfiler(hz=100, cpu=False, exclude_self=exclude_self)
    _sample_from_this_frame(profiler)
    profile = profiler.stop()
    assert has_function(profile, "_sample_from_this_frame") is not exclude_self


def test_sampler_thread_never_samples_itself(parked_thread: threading.Thread) -> None:
    profiler = SamplingProfiler(hz=200, cpu=False)
    profile = profiler.collect(0.1)
    assert profile is not None
    assert has_function(profile, "_parked_worker_for_unit_profile")
    sampler_frames = {f.function for s in profile.samples for f in s.stack[:1]}
    assert "sample_once" not in sampler_frames


# ---------------------------------------------------------------- real threads


def test_stacks_are_leaf_first(parked_thread: threading.Thread) -> None:
    profile = SamplingProfiler(hz=200, cpu=False, frames=frames_of(parked_thread)).collect(0.05)
    assert profile is not None
    stacks = [
        [f.function for f in s.stack]
        for s in profile.samples
        if any("_parked_worker_for_unit_profile" in f.function for f in s.stack)
    ]
    assert stacks
    for names in stacks:
        target = next(i for i, n in enumerate(names) if "_parked_worker_for_unit_profile" in n)
        run = max(i for i, n in enumerate(names) if n.endswith("run"))
        assert 0 < target < run, names


def test_frames_carry_file_and_line(parked_thread: threading.Thread) -> None:
    profile = SamplingProfiler(hz=200, cpu=False, frames=frames_of(parked_thread)).collect(0.05)
    assert profile is not None
    frames = [
        f
        for s in profile.samples
        for f in s.stack
        if "_parked_worker_for_unit_profile" in f.function
    ]
    assert frames
    assert all(f.filename.endswith("_helpers.py") and f.line > 0 for f in frames)


@pytest.mark.linux_only
@needs_linux
def test_real_cpu_profile_finds_busy_thread_and_skips_parked(
    busy_thread: threading.Thread, parked_thread: threading.Thread
) -> None:
    profile = SamplingProfiler(hz=100, cpu=True).collect(0.3)
    assert profile is not None
    assert has_function(profile, "_busy_spin_for_unit_profile")
    assert not has_function(profile, "_parked_worker_for_unit_profile")
    assert "profile.note" not in profile.attrs


def test_real_wall_profile_includes_parked_thread(parked_thread: threading.Thread) -> None:
    profile = SamplingProfiler(hz=100, cpu=False).collect(0.1)
    assert profile is not None
    assert has_function(profile, "_parked_worker_for_unit_profile")


def test_module_exports() -> None:
    assert set(_sampler.__all__) >= {
        "OWNER_ENV",
        "SamplingProfiler",
        "acquire_ownership",
        "cpu_clock_available",
        "release_ownership",
    }
    assert LINUX in (True, False)
