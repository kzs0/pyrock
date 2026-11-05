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


# --- scratch reference (for test validation only)
import random as _random
import sys as _sys
import threading as _threading
import time as _time
from pyrock.profile._pprof import Frame as _Frame, Sample as _Sample, SampleType as _ST

_state = {"mutex": 0, "block": 0.0}
_guard = _threading.Lock()
_records = {"mutex": {}, "block": {}}

def configure(*, mutex_sample_fraction, block_sample_rate):
    _state["mutex"] = mutex_sample_fraction; _state["block"] = block_sample_rate

def reset():
    with _guard:
        _records["mutex"].clear(); _records["block"].clear()

def _stack():
    f = _sys._getframe(1)
    while f is not None and f.f_code.co_filename == __file__:
        f = f.f_back
    out = []
    while f is not None:
        out.append(_Frame(f.f_code.co_name, f.f_code.co_filename, f.f_lineno)); f = f.f_back
    return tuple(out)

def _record(kind, delay_ns):
    if kind == "mutex":
        n = _state["mutex"]
        if n <= 0 or _random.randrange(n) != 0: return
    else:
        if _state["block"] <= 0: return
    st = _stack()
    with _guard:
        c, d = _records[kind].get(st, (0, 0))
        _records[kind][st] = (c + 1, d + delay_ns)

def _profile(kind):
    with _guard:
        samples = tuple(_Sample(st, v) for st, v in _records[kind].items())
    attrs = {} if samples else {"profile.note": "no instrumented primitives recorded contention"}
    return Profile((_ST("contentions", "count"), _ST("delay", "nanoseconds")), samples, _time.time_ns(), 0, attrs=attrs)

def mutex_profile(): return _profile("mutex")
def block_profile(): return _profile("block")

def _timed(kind, inner_acquire, blocking, timeout):
    if inner_acquire(False): return True
    if not blocking: return False
    t0 = _time.monotonic_ns()
    got = inner_acquire(True, timeout)
    _record(kind, _time.monotonic_ns() - t0)
    return got

class _Primitive:
    def __enter__(self): return self.acquire()
    def __exit__(self, *a): self.release()

class Lock(_Primitive):
    def __init__(self): self._l = _threading.Lock()
    def acquire(self, blocking=True, timeout=-1):
        return _timed("mutex", lambda b, t=-1: self._l.acquire(b, t) if b else self._l.acquire(False), blocking, timeout)
    def release(self): self._l.release()
    def locked(self): return self._l.locked()

class RLock(_Primitive):
    def __init__(self): self._l = _threading.RLock()
    def acquire(self, blocking=True, timeout=-1):
        return _timed("mutex", lambda b, t=-1: self._l.acquire(b, t) if b else self._l.acquire(False), blocking, timeout)
    def release(self): self._l.release()
    def _is_owned(self): return self._l._is_owned()
    def _release_save(self): return self._l._release_save()
    def _acquire_restore(self, x): self._l._acquire_restore(x)

class Condition(_Primitive):
    def __init__(self, lock=None):
        self._lock = lock if lock is not None else RLock()
        inner = self._lock._l
        self._c = _threading.Condition(inner)
    def acquire(self, blocking=True, timeout=-1): return self._lock.acquire(blocking, timeout)
    def release(self): self._lock.release()
    def wait(self, timeout=None):
        t0 = _time.monotonic_ns()
        r = self._c.wait(timeout)
        _record("block", _time.monotonic_ns() - t0)
        return r
    def wait_for(self, predicate, timeout=None):
        result = predicate()
        if result: return result
        t0 = _time.monotonic_ns()
        r = self._c.wait_for(predicate, timeout)
        _record("block", _time.monotonic_ns() - t0)
        return r
    def notify(self, n=1): self._c.notify(n)
    def notify_all(self): self._c.notify_all()

class Semaphore(_Primitive):
    def __init__(self, value=1): self._s = _threading.Semaphore(value)
    def acquire(self, blocking=True, timeout=None):
        if self._s.acquire(False): return True
        if not blocking: return False
        t0 = _time.monotonic_ns()
        got = self._s.acquire(True, timeout)
        _record("block", _time.monotonic_ns() - t0)
        return got
    def release(self, n=1): self._s.release(n)
