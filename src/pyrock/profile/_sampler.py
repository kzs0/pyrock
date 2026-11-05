"""Wall/CPU sampling profiler thread built on ``sys._current_frames()``.

CPU mode keeps only threads whose CPU clock advanced since the last tick
(``time.pthread_getcpuclockid`` + ``clock_gettime`` on Linux); elsewhere it
falls back to wall-clock and marks the profile with a ``profile.note``.
Only one pyrock sampler runs per process; a second one is skipped (and
counted), never stopped (see :func:`acquire_ownership`).
"""

from __future__ import annotations

from pyrock.profile._pprof import Profile

__all__ = [
    "OWNER_ENV",
    "SamplingProfiler",
    "acquire_ownership",
    "cpu_clock_available",
    "release_ownership",
]

OWNER_ENV = "PYROCK_PROFILER_OWNER"


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

    def __init__(self, *, hz: int = 100, cpu: bool = True, exclude_self: bool = True) -> None:
        raise NotImplementedError

    def start(self) -> bool:
        """Start sampling; ``False`` (and nothing started) if another sampler owns the process."""
        raise NotImplementedError

    def stop(self) -> Profile:
        """Stop sampling and return the collected profile."""
        raise NotImplementedError

    def collect(self, duration: float, /) -> Profile | None:
        """Sample for ``duration`` seconds; ``None`` if skipped because of another owner."""
        raise NotImplementedError
