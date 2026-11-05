"""Opt-in instrumented synchronization primitives for MUTEX/BLOCK profiles.

Drop-in replacements for :mod:`threading` primitives that record wait time
when contended, sampled by ``mutex_sample_fraction`` (Lock/RLock) and
``block_sample_rate`` (Condition/Semaphore). With no instrumented primitives
in use the profiles are empty and say so in a ``profile.note`` attribute.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType

from pyrock.profile._pprof import Profile

__all__ = [
    "Condition",
    "Lock",
    "RLock",
    "Semaphore",
    "block_profile",
    "configure",
    "mutex_profile",
    "reset",
]


def configure(*, mutex_sample_fraction: int, block_sample_rate: float) -> None:
    """Set process-wide sampling (0 disables recording)."""
    raise NotImplementedError


def reset() -> None:
    """Forget every recorded contention (sampling settings are kept)."""
    raise NotImplementedError


def mutex_profile() -> Profile:
    """``contentions/count`` and ``delay/nanoseconds`` from Lock/RLock waits."""
    raise NotImplementedError


def block_profile() -> Profile:
    """``contentions/count`` and ``delay/nanoseconds`` from Condition/Semaphore waits."""
    raise NotImplementedError


class _Primitive:
    def __enter__(self) -> bool:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class Lock(_Primitive):
    """Instrumented :class:`threading.Lock`."""

    def __init__(self) -> None:
        raise NotImplementedError

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError

    def locked(self) -> bool:
        raise NotImplementedError


class RLock(_Primitive):
    """Instrumented :class:`threading.RLock`."""

    def __init__(self) -> None:
        raise NotImplementedError

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError


class Condition(_Primitive):
    """Instrumented :class:`threading.Condition` (wait time counts as BLOCK)."""

    def __init__(self, lock: Lock | RLock | None = None) -> None:
        raise NotImplementedError

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> bool:
        raise NotImplementedError

    def wait_for(self, predicate: Callable[[], object], timeout: float | None = None) -> bool:
        raise NotImplementedError

    def notify(self, n: int = 1) -> None:
        raise NotImplementedError

    def notify_all(self) -> None:
        raise NotImplementedError


class Semaphore(_Primitive):
    """Instrumented :class:`threading.Semaphore` (wait time counts as BLOCK)."""

    def __init__(self, value: int = 1) -> None:
        raise NotImplementedError

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        raise NotImplementedError

    def release(self, n: int = 1) -> None:
        raise NotImplementedError
