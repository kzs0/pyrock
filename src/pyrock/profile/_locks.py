"""Opt-in instrumented synchronization primitives for MUTEX/BLOCK profiles.

Drop-in replacements for :mod:`threading` primitives that record wait time
when contended, sampled by ``mutex_sample_fraction`` (Lock/RLock) and
``block_sample_rate`` (Condition/Semaphore). With no instrumented primitives
in use the profiles are empty and say so in a ``profile.note`` attribute.

Sampling follows Go's runtime:

* **mutex** (``mutex_sample_fraction = N``): on average one contended
  acquisition in ``N`` is recorded, with its count and delay scaled by ``N``.
* **block** (``block_sample_rate = r`` seconds): a wait of at least ``r`` is
  always recorded; a shorter wait ``d`` is recorded with probability ``d / r``
  and scaled by ``r / d``.

An uncontended acquisition costs one extra non-blocking ``acquire`` call and
records nothing. Recorded stacks are the waiter's (leaf first), starting at
the caller of the primitive.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType, TracebackType
from typing import Any, TypeVar

from pyrock.profile._frames import StackKey, StackTable
from pyrock.profile._pprof import Profile, Sample, SampleType

__all__ = [
    "Condition",
    "Lock",
    "RLock",
    "Semaphore",
    "block_profile",
    "configure",
    "mutex_profile",
    "reset",
    "settings",
]

T = TypeVar("T")

EMPTY_NOTE = "no contention recorded by pyrock.profile instrumented primitives"

_CONTENTIONS = SampleType("contentions", "count")
_DELAY = SampleType("delay", "nanoseconds")


@dataclass(frozen=True, slots=True)
class _Settings:
    mutex_fraction: int = 0
    block_rate_ns: int = 0


# Replaced as a whole by configure(), so readers need no lock.
_settings = _Settings()


class _Recorder:
    """Aggregated contention samples for one profile kind (guarded by its lock)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stacks = StackTable()
        self.totals: dict[StackKey, list[int]] = {}
        self.since_ns = time.time_ns()

    def record(self, frame: FrameType | None, count: int, delay_ns: int) -> None:
        with self.lock:
            key = self.stacks.key(frame)
            entry = self.totals.get(key)
            if entry is None:
                self.totals[key] = [count, delay_ns]
            else:
                entry[0] += count
                entry[1] += delay_ns

    def profile(self, period: int) -> Profile:
        with self.lock:
            samples = tuple(
                Sample(self.stacks.frames(key), (count, delay))
                for key, (count, delay) in self.totals.items()
            )
            since = self.since_ns
        now = time.time_ns()
        return Profile(
            sample_types=(_CONTENTIONS, _DELAY),
            samples=samples,
            start_time_ns=since,
            duration_ns=max(now - since, 0),
            period_type=_CONTENTIONS,
            period=period,
            attrs={} if samples else {"profile.note": EMPTY_NOTE},
        )


_mutex = _Recorder()
_block = _Recorder()


def configure(*, mutex_sample_fraction: int, block_sample_rate: float) -> None:
    """Set process-wide sampling (0 disables recording)."""
    global _settings
    if mutex_sample_fraction < 0 or block_sample_rate < 0:
        raise ValueError("sampling settings must be >= 0")
    _settings = _Settings(mutex_sample_fraction, round(block_sample_rate * 1e9))


def settings() -> tuple[int, float]:
    """The current ``(mutex_sample_fraction, block_sample_rate)``."""
    current = _settings
    return current.mutex_fraction, current.block_rate_ns / 1e9


def reset() -> None:
    """Forget every recorded contention (sampling settings are kept).

    Swaps in fresh recorders instead of clearing the old ones, so it acquires
    no lock and is safe in a forked child even if a parent thread held a
    recorder lock at fork time. A recording racing with it lands in the
    discarded recorder, as if it happened just before the reset.
    """
    global _mutex, _block
    _mutex = _Recorder()
    _block = _Recorder()


def _after_fork_in_child() -> None:
    """Fresh, empty recorders in a forked child (contention seen by the parent is dropped)."""
    reset()


def mutex_profile() -> Profile:
    """``contentions/count`` and ``delay/nanoseconds`` from Lock/RLock waits."""
    return _mutex.profile(_settings.mutex_fraction)


def block_profile() -> Profile:
    """``contentions/count`` and ``delay/nanoseconds`` from Condition/Semaphore waits."""
    return _block.profile(_settings.block_rate_ns)


# -- recording --------------------------------------------------------------


def _caller() -> FrameType | None:
    """The innermost frame outside this module (the code using the primitive)."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    return frame


def _mutex_wait(acquire: Callable[[bool, float], bool], timeout: float) -> bool:
    """A contended blocking acquire, recorded in the MUTEX profile when sampled."""
    fraction = _settings.mutex_fraction
    if fraction <= 0 or (fraction > 1 and random.random() * fraction >= 1):  # noqa: S311
        return acquire(True, timeout)
    started = time.perf_counter_ns()
    acquired = acquire(True, timeout)
    delay = time.perf_counter_ns() - started
    _mutex.record(_caller(), fraction, delay * fraction)
    return acquired


def _record_block(started_ns: int) -> None:
    """Record a wait that began at ``started_ns`` in the BLOCK profile when sampled."""
    rate = _settings.block_rate_ns
    delay = time.perf_counter_ns() - started_ns
    if delay >= rate:
        _block.record(_caller(), 1, delay)
    elif delay > 0 and random.random() * rate < delay:  # noqa: S311
        _block.record(_caller(), max(round(rate / delay), 1), rate)


def _check_timeout(blocking: bool, timeout: float | None, none: float | None) -> None:
    if not blocking and timeout != none:
        raise ValueError("can't specify a timeout for a non-blocking call")


# -- primitives -------------------------------------------------------------


class _Primitive(ABC):
    """Context-manager protocol shared by every primitive."""

    __slots__ = ()

    @abstractmethod
    def acquire(self, blocking: bool = True, timeout: Any = -1) -> bool:
        """Acquire; ``True`` on success."""

    @abstractmethod
    def release(self) -> None:
        """Release once."""

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class Lock(_Primitive):
    """Instrumented :class:`threading.Lock`."""

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Like :meth:`threading.Lock.acquire`; contended waits feed the MUTEX profile."""
        _check_timeout(blocking, timeout, -1)
        if self._lock.acquire(False):
            return True
        return blocking and _mutex_wait(self._lock.acquire, timeout)

    def release(self) -> None:
        """Release; ``RuntimeError`` if unlocked."""
        self._lock.release()

    def locked(self) -> bool:
        """Whether the lock is held by any thread."""
        return self._lock.locked()


class RLock(_Primitive):
    """Instrumented :class:`threading.RLock`."""

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Like :meth:`threading.RLock.acquire`; contended waits feed the MUTEX profile."""
        _check_timeout(blocking, timeout, -1)
        if self._lock.acquire(False):
            return True
        return blocking and _mutex_wait(self._lock.acquire, timeout)

    def release(self) -> None:
        """Release one level; ``RuntimeError`` if not held by this thread."""
        self._lock.release()


class Condition(_Primitive):
    """Instrumented :class:`threading.Condition` (wait time counts as BLOCK).

    Acquiring the condition goes through the (instrumented) lock; ``wait``
    re-acquires the underlying lock directly, so its time counts as BLOCK only.
    """

    __slots__ = ("_cond", "_lock")

    def __init__(self, lock: Lock | RLock | None = None) -> None:
        self._lock = RLock() if lock is None else lock
        self._cond = threading.Condition(self._lock._lock)

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the underlying lock."""
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        """Release the underlying lock."""
        self._lock.release()

    def wait(self, timeout: float | None = None) -> bool:
        """Like :meth:`threading.Condition.wait`; the wait feeds the BLOCK profile."""
        if _settings.block_rate_ns <= 0:
            return self._cond.wait(timeout)
        started = time.perf_counter_ns()
        notified = self._cond.wait(timeout)
        _record_block(started)
        return notified

    def wait_for(self, predicate: Callable[[], T], timeout: float | None = None) -> T:
        """Wait until ``predicate()`` is true or ``timeout`` expires; return its last value."""
        deadline = None if timeout is None else time.monotonic() + timeout
        result = predicate()
        while not result:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
            self.wait(remaining)
            result = predicate()
        return result

    def notify(self, n: int = 1) -> None:
        """Wake up to ``n`` waiters; ``RuntimeError`` without the lock."""
        self._cond.notify(n)

    def notify_all(self) -> None:
        """Wake every waiter; ``RuntimeError`` without the lock."""
        self._cond.notify_all()


class Semaphore(_Primitive):
    """Instrumented :class:`threading.Semaphore` (wait time counts as BLOCK)."""

    __slots__ = ("_sem",)

    def __init__(self, value: int = 1) -> None:
        self._sem = threading.Semaphore(value)

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Like :meth:`threading.Semaphore.acquire`; waits feed the BLOCK profile."""
        _check_timeout(blocking, timeout, None)
        if self._sem.acquire(False):
            return True
        if not blocking:
            return False
        if _settings.block_rate_ns <= 0:
            return self._sem.acquire(True, timeout)
        started = time.perf_counter_ns()
        acquired = self._sem.acquire(True, timeout)
        _record_block(started)
        return acquired

    def release(self, n: int = 1) -> None:
        """Release ``n`` times."""
        self._sem.release(n)
