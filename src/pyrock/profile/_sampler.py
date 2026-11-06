"""Wall/CPU sampling profiler thread built on ``sys._current_frames()``.

CPU mode keeps only threads whose CPU clock advanced since the last tick
(``time.pthread_getcpuclockid`` + ``clock_gettime`` on Linux); elsewhere it
falls back to wall-clock and marks the profile with a ``profile.note``.
Only one pyrock sampler runs per process; a second one is skipped (and
counted), never stopped (see :func:`acquire_ownership`).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from types import FrameType

from pyrock.profile._frames import StackKey, StackTable
from pyrock.profile._pprof import Profile, Sample, SampleType

__all__ = [
    "OWNER_ENV",
    "CPUClock",
    "FramesSource",
    "SamplingProfiler",
    "acquire_ownership",
    "cpu_clock_available",
    "release_ownership",
]

OWNER_ENV = "PYROCK_PROFILER_OWNER"
"""When set in the environment, another profiler owns the process and sampling is skipped."""

FramesSource = Callable[[], Mapping[int, FrameType]]
"""Returns the current frame of each thread, keyed by thread ident (``sys._current_frames``)."""

CPUClock = Callable[[int], int | None]
"""Returns a thread's CPU time in nanoseconds by thread ident, or ``None`` if unavailable."""

FALLBACK_NOTE = "per-thread CPU clocks unavailable; values are wall-clock time"

_SAMPLES = SampleType("samples", "count")
_CPU = SampleType("cpu", "nanoseconds")
_WALL = SampleType("wall", "nanoseconds")

_owner_lock = threading.Lock()
_owned = False


def cpu_clock_available() -> bool:
    """Whether per-thread CPU clocks are available on this platform."""
    if not hasattr(time, "pthread_getcpuclockid"):
        return False
    try:
        time.clock_gettime_ns(time.pthread_getcpuclockid(threading.get_ident()))
    except OSError:
        return False
    return True


def acquire_ownership() -> bool:
    """Claim the process-wide sampler slot; ``False`` if another sampler owns it."""
    global _owned
    if os.environ.get(OWNER_ENV):
        return False
    with _owner_lock:
        if _owned:
            return False
        _owned = True
        return True


def release_ownership() -> None:
    """Give the process-wide sampler slot back (no-op if it is free)."""
    global _owned
    with _owner_lock:
        _owned = False


class _ThreadCPUClock:
    """Per-thread CPU time through ``pthread_getcpuclockid``, caching clock ids by ident.

    A clock id is resolved while the thread is known to be alive (its frame
    was just read); later readings of an exited thread fail with ``OSError``,
    which drops the cached id instead of touching the dead thread again.
    """

    __slots__ = ("_ids",)

    def __init__(self) -> None:
        self._ids: dict[int, int] = {}

    def __call__(self, ident: int) -> int | None:
        clock_id = self._ids.get(ident)
        try:
            if clock_id is None:
                clock_id = self._ids[ident] = time.pthread_getcpuclockid(ident)
            return time.clock_gettime_ns(clock_id)
        except OSError:
            self._ids.pop(ident, None)
            return None

    def retain(self, idents: Mapping[int, object]) -> None:
        """Forget clock ids of threads that are gone."""
        if len(self._ids) > len(idents):
            self._ids = {i: c for i, c in self._ids.items() if i in idents}


def _no_cpu_clock(ident: int) -> int | None:
    return None


class SamplingProfiler:
    """Samples every thread's stack at ``hz`` until :meth:`stop`."""

    def __init__(
        self,
        *,
        hz: int = 100,
        cpu: bool = True,
        exclude_self: bool = True,
        frames: FramesSource | None = None,
        cpu_clock: CPUClock | None = None,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        """Configure a sampler; nothing starts until :meth:`start` or :meth:`collect`.

        Test seams (defaults in parentheses): ``frames`` (``sys._current_frames``),
        ``cpu_clock`` (per-thread CPU clocks via ``time.pthread_getcpuclockid``;
        ``None`` readings mean unavailable, which falls back to wall-clock and
        sets the ``profile.note`` attribute), ``clock`` (``time.monotonic_ns``,
        used for tick scheduling, durations and WALL values) and ``sleep`` (the
        wait between ticks; by default an interruptible wait so :meth:`stop`
        is prompt).

        Each tick records, per sampled thread, one ``samples/count`` and the
        thread's CPU-time delta (CPU) or the time since the previous tick (WALL).
        In CPU mode a thread is kept only when its CPU clock advanced since its
        previous reading; the first reading of a thread is only a baseline.
        With ``exclude_self`` the thread taking the sample is skipped.
        The profile's ``period`` is ``1_000_000_000 // hz`` nanoseconds.
        """
        if hz <= 0:
            raise ValueError("hz must be positive")
        self._period_ns = 1_000_000_000 // hz
        self._cpu = cpu
        self._exclude_self = exclude_self
        self._frames: FramesSource = frames or sys._current_frames
        self._native_clock: _ThreadCPUClock | None = None
        if cpu_clock is None and cpu:
            if cpu_clock_available():
                self._native_clock = cpu_clock = _ThreadCPUClock()
            else:
                cpu_clock = _no_cpu_clock
        self._cpu_clock: CPUClock = cpu_clock or _no_cpu_clock
        self._clock = clock or time.monotonic_ns
        self._sleep = sleep
        self._wake = threading.Event()

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._owns = False
        self._stacks = StackTable()
        self._totals: dict[StackKey, list[int]] = {}
        self._last_cpu: dict[int, int] = {}
        self._last_tick: int | None = None
        self._fell_back = False
        self._begin_wall_ns = 0
        self._begin_ns = 0
        self._reset_window()

    # -- sampling -----------------------------------------------------------

    def sample_once(self) -> None:
        """Take one sample now, on the calling thread (exactly what each tick does)."""
        current = self._frames()
        skip = threading.get_ident() if self._exclude_self else None
        with self._lock:
            now = self._clock()
            last = self._last_tick
            wall = self._period_ns if last is None else now - last
            self._last_tick = now
            totals, key = self._totals, self._stacks.key
            for ident, frame in current.items():
                if ident == skip:
                    continue
                value = self._cpu_delta(ident, wall) if self._cpu else wall
                if value is None:
                    continue
                stack = key(frame)
                entry = totals.get(stack)
                if entry is None:
                    totals[stack] = [1, value]
                else:
                    entry[0] += 1
                    entry[1] += value
            if self._cpu:
                self._forget_exited(current)

    def _cpu_delta(self, ident: int, wall: int) -> int | None:
        """CPU time since the thread's previous reading; ``None`` to skip the thread."""
        reading = self._cpu_clock(ident)
        if reading is None:
            self._fell_back = True
            return wall
        previous = self._last_cpu.get(ident)
        self._last_cpu[ident] = reading
        if previous is None or reading <= previous:
            return None
        return reading - previous

    def _forget_exited(self, current: Mapping[int, object]) -> None:
        if len(self._last_cpu) > len(current):
            self._last_cpu = {i: v for i, v in self._last_cpu.items() if i in current}
        if self._native_clock is not None:
            self._native_clock.retain(current)

    def _run_until(self, deadline_ns: int | None, cancel: threading.Event) -> None:
        """Tick every period until ``deadline_ns`` (on ``clock``) or ``cancel``."""
        sleep = self._sleep or cancel.wait
        period = self._period_ns
        next_tick = self._clock()
        while not cancel.is_set():
            if deadline_ns is not None and self._clock() >= deadline_ns:
                return
            self.sample_once()
            now = self._clock()
            next_tick += period
            if next_tick <= now:  # running late: drop missed ticks rather than burst
                next_tick = now + period
            sleep((next_tick - now) / 1e9)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        """Start sampling; ``False`` (and nothing started) if another sampler owns the process.

        Idempotent: calling it while this profiler runs returns ``True`` and
        starts no second thread.
        """
        with self._lock:
            if self._thread is not None:
                return True
            if not acquire_ownership():
                return False
            self._owns = True
            self._wake.clear()
            self._reset_window()
            self._thread = threading.Thread(
                target=self._run_until,
                args=(None, self._wake),
                name="pyrock-sampling-profiler",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> Profile:
        """Stop sampling and return the profile collected so far, then clear it.

        Idempotent: the sampler thread is joined and ownership released once;
        later calls return what was collected since (possibly nothing). Ticks
        taken with :meth:`sample_once` are included.
        """
        with self._lock:
            thread, self._thread = self._thread, None
            owned, self._owns = self._owns, False
        if thread is not None:
            self._wake.set()
            thread.join()
        if owned:
            release_ownership()
        return self._drain()

    def collect(
        self, duration: float, /, *, cancel: threading.Event | None = None
    ) -> Profile | None:
        """Sample for ``duration`` seconds; ``None`` if skipped because of another owner.

        Ticks are scheduled on ``clock`` and separated by ``sleep(1 / hz)``, so
        about ``duration * hz`` ticks are taken. Ownership is released afterwards.
        Setting ``cancel`` ends the window early (the default sleep waits on it).
        """
        if not acquire_ownership():
            return None
        try:
            with self._lock:
                self._reset_window()
            deadline = self._clock() + round(duration * 1e9)
            self._run_until(deadline, cancel or threading.Event())
        finally:
            release_ownership()
        return self._drain()

    # -- results ------------------------------------------------------------

    def _reset_window(self) -> None:
        """Start a new collection window (caller holds the lock)."""
        self._totals = {}
        self._stacks.clear()
        self._last_cpu = {}
        self._last_tick = None
        self._fell_back = False
        self._begin_wall_ns = time.time_ns()
        self._begin_ns = self._clock()

    def _drain(self) -> Profile:
        """Build the profile collected so far and start a new window."""
        with self._lock:
            frames = self._stacks.frames
            samples = tuple(
                Sample(frames(key), (count, value))
                for key, (count, value) in self._totals.items()
            )
            profile = Profile(
                sample_types=(_SAMPLES, _CPU if self._cpu else _WALL),
                samples=samples,
                start_time_ns=self._begin_wall_ns,
                duration_ns=max(self._clock() - self._begin_ns, 0),
                period_type=_CPU if self._cpu else _WALL,
                period=self._period_ns,
                attrs={"profile.note": FALLBACK_NOTE} if self._fell_back else {},
            )
            self._reset_window()
        return profile
