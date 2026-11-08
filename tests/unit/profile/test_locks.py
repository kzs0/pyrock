"""Instrumented synchronization primitives (``pyrock.profile.Lock`` and friends).

They must be drop-in replacements for the :mod:`threading` primitives, and
record contended waits into the MUTEX (Lock/RLock) and BLOCK
(Condition/Semaphore) profiles according to the sampling settings.

To know a waiter is really blocked without sleeping, tests wait until the
waiter thread is executing code from ``pyrock/profile/_locks.py`` while the
primitive is still held (or, for a Condition, inside ``wait``).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator

import pytest

import pyrock.profile
from pyrock.profile import Condition, Lock, RLock, Semaphore, _locks
from pyrock.profile._pprof import Profile
from tests.support.collectors import wait_until

CONTENTION_TYPES = [("contentions", "count"), ("delay", "nanoseconds")]


# ---------------------------------------------------------------- helpers


def raises_runtime_error(fn: Callable[[], object]) -> None:
    """``fn()`` raises ``RuntimeError`` (and not the stub's ``NotImplementedError``)."""
    with pytest.raises(RuntimeError) as info:
        fn()
    assert not isinstance(info.value, NotImplementedError)


def in_locks_code(thread: threading.Thread, function: str | None = None) -> bool:
    frame = sys._current_frames().get(thread.ident or -1)
    while frame is not None:
        if frame.f_code.co_filename == _locks.__file__ and (
            function is None or frame.f_code.co_name == function
        ):
            return True
        frame = frame.f_back
    return False


def blocked_in_primitive(thread: threading.Thread, function: str | None = None) -> None:
    wait_until(lambda: in_locks_code(thread, function))
    # The waiter has entered the instrumented primitive; give it a moment to block.
    threading.Event().wait(0.02)


def totals(profile: Profile, function: str) -> tuple[int, int]:
    contentions = delay = 0
    for sample in profile.samples:
        if any(function in f.function for f in sample.stack):
            contentions += sample.values[0]
            delay += sample.values[1]
    return contentions, delay


@pytest.fixture
def recording() -> Iterator[None]:
    """Record every contention (fraction 1, any wait) and start from a clean slate."""
    _locks.configure(mutex_sample_fraction=1, block_sample_rate=1e-9)
    _locks.reset()
    yield
    _locks.configure(mutex_sample_fraction=0, block_sample_rate=0.0)
    _locks.reset()


@pytest.fixture
def not_recording() -> Iterator[None]:
    _locks.configure(mutex_sample_fraction=0, block_sample_rate=0.0)
    _locks.reset()
    yield
    _locks.reset()


# ---------------------------------------------------------------- contention workloads


def _wait_on_lock_for_unit_profile(lock: Lock | RLock) -> None:
    with lock:
        pass


def _wait_on_condition_for_unit_profile(cond: Condition, flag: list[bool]) -> None:
    with cond:
        while not flag:
            cond.wait(5)


def _wait_on_semaphore_for_unit_profile(sem: Semaphore) -> None:
    sem.acquire()


def contend_on_lock(lock: Lock | RLock) -> None:
    lock.acquire()
    waiter = threading.Thread(target=_wait_on_lock_for_unit_profile, args=(lock,))
    waiter.start()
    try:
        blocked_in_primitive(waiter)
    finally:
        lock.release()
    waiter.join(5)
    assert not waiter.is_alive()


def contend_on_condition(cond: Condition) -> None:
    flag: list[bool] = []
    waiter = threading.Thread(target=_wait_on_condition_for_unit_profile, args=(cond, flag))
    waiter.start()
    blocked_in_primitive(waiter, "wait")
    with cond:
        flag.append(True)
        cond.notify_all()
    waiter.join(5)
    assert not waiter.is_alive()


def contend_on_semaphore(sem: Semaphore) -> None:
    waiter = threading.Thread(target=_wait_on_semaphore_for_unit_profile, args=(sem,))
    waiter.start()
    blocked_in_primitive(waiter)
    sem.release()
    waiter.join(5)
    assert not waiter.is_alive()


# ---------------------------------------------------------------- public names


def test_primitives_are_exported_from_pyrock_profile() -> None:
    for name in ("Lock", "RLock", "Condition", "Semaphore"):
        assert name in pyrock.profile.__all__
        assert getattr(pyrock.profile, name) is getattr(_locks, name)


# ---------------------------------------------------------------- Lock drop-in


def test_lock_acquire_release_and_locked() -> None:
    lock = Lock()
    assert not lock.locked()
    assert lock.acquire() is True
    assert lock.locked()
    assert lock.acquire(blocking=False) is False
    lock.release()
    assert not lock.locked()


def test_lock_acquire_timeout() -> None:
    lock = Lock()
    lock.acquire()
    try:
        assert lock.acquire(timeout=0.01) is False
    finally:
        lock.release()
    assert lock.acquire(timeout=0.01) is True
    lock.release()


def test_lock_is_a_context_manager() -> None:
    lock = Lock()
    with lock as entered:
        assert entered is True
        assert lock.locked()
    assert not lock.locked()


def test_lock_released_on_exception() -> None:
    lock = Lock()
    with pytest.raises(KeyError), lock:
        raise KeyError("boom")
    assert not lock.locked()


def test_lock_release_unlocked_raises_runtime_error() -> None:
    lock = Lock()
    raises_runtime_error(lock.release)


def test_lock_is_not_reentrant() -> None:
    lock = Lock()
    with lock:
        assert lock.acquire(blocking=False) is False


def test_lock_mutual_exclusion_under_threads() -> None:
    lock = Lock()
    counter = [0]

    def work() -> None:
        for _ in range(2000):
            with lock:
                value = counter[0]
                counter[0] = value + 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert counter[0] == 16000


# ---------------------------------------------------------------- RLock drop-in


def test_rlock_is_reentrant() -> None:
    lock = RLock()
    assert lock.acquire()
    assert lock.acquire()
    with lock:
        pass
    lock.release()
    lock.release()
    raises_runtime_error(lock.release)


def test_rlock_excludes_other_threads_until_fully_released() -> None:
    lock = RLock()
    results: list[bool] = []

    def try_acquire() -> None:
        got = lock.acquire(blocking=False)
        results.append(got)
        if got:
            lock.release()

    lock.acquire()
    lock.acquire()
    lock.release()
    other = threading.Thread(target=try_acquire)
    other.start()
    other.join(5)
    lock.release()
    other = threading.Thread(target=try_acquire)
    other.start()
    other.join(5)
    assert results == [False, True]


def test_rlock_release_by_non_owner_raises() -> None:
    lock = RLock()
    lock.acquire()
    errors: list[BaseException] = []

    def release() -> None:
        try:
            lock.release()
        except BaseException as exc:
            errors.append(exc)

    other = threading.Thread(target=release)
    other.start()
    other.join(5)
    lock.release()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert not isinstance(errors[0], NotImplementedError)


def test_rlock_acquire_timeout_from_other_thread() -> None:
    lock = RLock()
    results: list[bool] = []
    with lock:
        other = threading.Thread(target=lambda: results.append(lock.acquire(timeout=0.01)))
        other.start()
        other.join(5)
    assert results == [False]


# ---------------------------------------------------------------- Condition drop-in


@pytest.mark.parametrize("make_lock", [None, Lock, RLock], ids=["default", "Lock", "RLock"])
def test_condition_wait_and_notify(make_lock: Callable[[], Lock | RLock] | None) -> None:
    cond = Condition(make_lock() if make_lock else None)
    items: list[int] = []

    def produce() -> None:
        with cond:
            items.append(1)
            cond.notify()

    with cond:
        producer = threading.Thread(target=produce)
        producer.start()
        assert cond.wait_for(lambda: items, timeout=5)
    producer.join(5)
    assert items == [1]


def test_condition_wait_timeout_returns_false() -> None:
    cond = Condition()
    with cond:
        assert cond.wait(timeout=0.01) is False


def test_condition_wait_for_already_true() -> None:
    cond = Condition()
    with cond:
        assert cond.wait_for(lambda: True, timeout=0.01) is True


def test_condition_notify_all_wakes_every_waiter() -> None:
    cond = Condition()
    ready = threading.Barrier(4)
    woken: list[int] = []
    go: list[bool] = []

    def waiter(i: int) -> None:
        ready.wait(5)
        with cond:
            assert cond.wait_for(lambda: go, timeout=5)
            woken.append(i)

    threads = [threading.Thread(target=waiter, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    ready.wait(5)
    with cond:
        go.append(True)
        cond.notify_all()
    for t in threads:
        t.join(5)
    assert sorted(woken) == [0, 1, 2]


def test_condition_requires_the_lock() -> None:
    cond = Condition()
    raises_runtime_error(cond.notify)
    raises_runtime_error(lambda: cond.wait(0.01))


def test_condition_is_a_context_manager_over_its_lock() -> None:
    lock = Lock()
    cond = Condition(lock)
    with cond:
        assert lock.locked()
    assert not lock.locked()
    assert cond.acquire()
    assert lock.locked()
    cond.release()
    assert not lock.locked()


# ---------------------------------------------------------------- Semaphore drop-in


def test_semaphore_counts() -> None:
    sem = Semaphore(2)
    assert sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)
    assert not sem.acquire(blocking=False)
    sem.release(2)
    assert sem.acquire(timeout=0.01)
    assert sem.acquire(timeout=0.01)
    assert not sem.acquire(timeout=0.01)


def test_semaphore_default_value_is_one() -> None:
    sem = Semaphore()
    with sem:
        assert not sem.acquire(blocking=False)
    assert sem.acquire(blocking=False)


def test_semaphore_rejects_negative_value() -> None:
    with pytest.raises(ValueError, match=r"."):
        Semaphore(-1)


# ---------------------------------------------------------------- profiles


@pytest.mark.usefixtures("recording")
def test_profile_shapes() -> None:
    for profile in (_locks.mutex_profile(), _locks.block_profile()):
        assert [(t.type, t.unit) for t in profile.sample_types] == CONTENTION_TYPES


@pytest.mark.usefixtures("recording")
def test_empty_profiles_say_so_in_a_note() -> None:
    for profile in (_locks.mutex_profile(), _locks.block_profile()):
        assert profile.samples == ()
        assert profile.attrs.get("profile.note")


@pytest.mark.usefixtures("recording")
@pytest.mark.parametrize("make_lock", [Lock, RLock], ids=["Lock", "RLock"])
def test_contended_lock_is_recorded_in_the_mutex_profile(
    make_lock: Callable[[], Lock | RLock],
) -> None:
    contend_on_lock(make_lock())
    contentions, delay = totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile")
    assert contentions >= 1
    assert delay > 0
    assert totals(_locks.block_profile(), "_wait_on_lock_for_unit_profile") == (0, 0)


@pytest.mark.usefixtures("recording")
def test_uncontended_lock_records_nothing() -> None:
    lock = Lock()
    _wait_on_lock_for_unit_profile(lock)
    assert totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile") == (0, 0)


@pytest.mark.usefixtures("recording")
def test_condition_wait_is_recorded_in_the_block_profile() -> None:
    contend_on_condition(Condition())
    contentions, delay = totals(_locks.block_profile(), "_wait_on_condition_for_unit_profile")
    assert contentions >= 1
    assert delay > 0


@pytest.mark.usefixtures("recording")
def test_semaphore_wait_is_recorded_in_the_block_profile() -> None:
    contend_on_semaphore(Semaphore(0))
    contentions, delay = totals(_locks.block_profile(), "_wait_on_semaphore_for_unit_profile")
    assert contentions >= 1
    assert delay > 0
    assert totals(_locks.mutex_profile(), "_wait_on_semaphore_for_unit_profile") == (0, 0)


@pytest.mark.usefixtures("recording")
def test_reset_forgets_recorded_contention() -> None:
    contend_on_lock(Lock())
    assert totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile")[0] >= 1
    _locks.reset()
    assert totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile") == (0, 0)


@pytest.mark.usefixtures("recording")
def test_contention_accumulates_across_reads() -> None:
    contend_on_lock(Lock())
    first = totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile")[0]
    contend_on_lock(Lock())
    second = totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile")[0]
    assert second > first >= 1


@pytest.mark.usefixtures("not_recording")
def test_zero_sample_fraction_records_no_mutex_contention() -> None:
    contend_on_lock(Lock())
    assert totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile") == (0, 0)


@pytest.mark.usefixtures("not_recording")
def test_zero_block_rate_records_no_block_events() -> None:
    contend_on_condition(Condition())
    contend_on_semaphore(Semaphore(0))
    profile = _locks.block_profile()
    assert totals(profile, "_wait_on_condition_for_unit_profile") == (0, 0)
    assert totals(profile, "_wait_on_semaphore_for_unit_profile") == (0, 0)


def test_mutex_and_block_settings_are_independent() -> None:
    _locks.configure(mutex_sample_fraction=1, block_sample_rate=0.0)
    _locks.reset()
    try:
        contend_on_lock(Lock())
        contend_on_condition(Condition())
        assert totals(_locks.mutex_profile(), "_wait_on_lock_for_unit_profile")[0] >= 1
        assert totals(_locks.block_profile(), "_wait_on_condition_for_unit_profile") == (0, 0)
    finally:
        _locks.configure(mutex_sample_fraction=0, block_sample_rate=0.0)
        _locks.reset()


def test_primitives_work_while_not_configured() -> None:
    # Instrumentation is opt-in and must never change behaviour.
    lock, rlock, sem = Lock(), RLock(), Semaphore(1)
    with lock, rlock, rlock, sem:
        assert lock.locked()
