"""Wall/CPU sampling profiler thread built on ``sys._current_frames()``.

CPU mode keeps only threads whose CPU clock advanced since the last tick
(``time.pthread_getcpuclockid`` + ``clock_gettime`` on Linux); elsewhere it
falls back to wall-clock and marks the profile with a ``profile.note``.
Only one pyrock sampler runs per process; a second one is skipped (and
counted), never stopped (see :func:`acquire_ownership`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import FrameType

from pyrock.profile._pprof import Profile

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


def cpu_clock_available() -> bool:
    """Whether per-thread CPU clocks are available on this platform."""
    raise NotImplementedError


def acquire_ownership() -> bool:
    """Claim the process-wide sampler slot; ``False`` if another sampler owns it."""
    raise NotImplementedError


def release_ownership() -> None:
    raise NotImplementedError


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
        raise NotImplementedError

    def sample_once(self) -> None:
        """Take one sample now, on the calling thread (exactly what each tick does)."""
        raise NotImplementedError

    def start(self) -> bool:
        """Start sampling; ``False`` (and nothing started) if another sampler owns the process.

        Idempotent: calling it while this profiler runs returns ``True`` and
        starts no second thread.
        """
        raise NotImplementedError

    def stop(self) -> Profile:
        """Stop sampling and return the profile collected so far, then clear it.

        Idempotent: the sampler thread is joined and ownership released once;
        later calls return what was collected since (possibly nothing). Ticks
        taken with :meth:`sample_once` are included.
        """
        raise NotImplementedError

    def collect(self, duration: float, /) -> Profile | None:
        """Sample for ``duration`` seconds; ``None`` if skipped because of another owner.

        Ticks are scheduled on ``clock`` and separated by ``sleep(1 / hz)``, so
        about ``duration * hz`` ticks are taken. Ownership is released afterwards.
        """
        raise NotImplementedError
