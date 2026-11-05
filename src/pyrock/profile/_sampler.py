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


# --- scratch reference (for test validation only)
import os as _os
import sys as _sys
import threading as _threading
import time as _time
from pyrock.profile._pprof import Frame as _Frame, Sample as _Sample, SampleType as _ST

_owner = {"held": False}
_olock = _threading.Lock()

def cpu_clock_available():
    return hasattr(_time, "pthread_getcpuclockid")

def acquire_ownership():
    with _olock:
        if _os.environ.get(OWNER_ENV) or _owner["held"]: return False
        _owner["held"] = True; return True

def release_ownership():
    with _olock: _owner["held"] = False

def _default_cpu(ident):
    try: return _time.clock_gettime_ns(_time.pthread_getcpuclockid(ident))
    except (OSError, AttributeError, OverflowError): return None

def _stack(frame):
    out = []
    while frame is not None:
        out.append(_Frame(frame.f_code.co_name, frame.f_code.co_filename, frame.f_lineno)); frame = frame.f_back
    return tuple(out)

class SamplingProfiler:
    def __init__(self, *, hz=100, cpu=True, exclude_self=True, frames=None, cpu_clock=None, clock=None, sleep=None):
        self._hz, self._cpu, self._excl = hz, cpu, exclude_self
        self._frames = frames or _sys._current_frames
        self._cpu_clock = cpu_clock or _default_cpu
        self._clock = clock or _time.monotonic_ns
        self._sleep = sleep
        self._stop = _threading.Event()
        self._thread = None
        self._lock = _threading.Lock()
        self._reset()

    def _reset(self):
        self._agg = {}; self._last_cpu = {}; self._last_tick = None; self._note = False
        self._start_wall = _time.time_ns(); self._start_mono = self._clock()

    def sample_once(self):
        now = self._clock()
        wall = (now - self._last_tick) if self._last_tick is not None else 1_000_000_000 // self._hz
        self._last_tick = now
        me = _threading.get_ident()
        for ident, frame in dict(self._frames()).items():
            if self._excl and ident == me: continue
            value = wall
            if self._cpu:
                c = self._cpu_clock(ident)
                if c is None:
                    self._note = True
                else:
                    prev = self._last_cpu.get(ident); self._last_cpu[ident] = c
                    if prev is None or c <= prev: continue
                    value = c - prev
            st = _stack(frame)
            with self._lock:
                n, v = self._agg.get(st, (0, 0)); self._agg[st] = (n + 1, v + value)

    def _profile(self):
        kind = "cpu" if self._cpu else "wall"
        with self._lock:
            samples = tuple(_Sample(st, (n, v)) for st, (n, v) in self._agg.items())
        attrs = {"profile.note": "wall-clock fallback"} if self._note else {}
        p = Profile((_ST("samples", "count"), _ST(kind, "nanoseconds")), samples, self._start_wall, self._clock() - self._start_mono, _ST(kind, "nanoseconds"), 1_000_000_000 // self._hz, attrs=attrs)
        self._reset()
        return p

    def _wait(self):
        if self._sleep is not None: self._sleep(1 / self._hz); return self._stop.is_set()
        return self._stop.wait(1 / self._hz)

    def _run(self):
        while not self._stop.is_set():
            self.sample_once()
            if self._wait(): break

    def start(self):
        if self._thread is not None: return True
        if not acquire_ownership(): return False
        self._stop.clear(); self._reset()
        self._thread = _threading.Thread(target=self._run, daemon=True, name="pyrock-sampler")
        self._thread.start()
        return True

    def stop(self):
        if self._thread is not None:
            self._stop.set(); self._thread.join(); self._thread = None
            release_ownership()
        return self._profile()

    def collect(self, duration, /):
        if not acquire_ownership(): return None
        try:
            self._reset()
            deadline = self._clock() + duration * 1e9
            while self._clock() < deadline:
                self.sample_once()
                if self._sleep is not None: self._sleep(1 / self._hz)
                else: _time.sleep(1 / self._hz)
            return self._profile()
        finally:
            release_ownership()
