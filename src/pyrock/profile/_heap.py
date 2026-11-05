"""Heap profiles from ``tracemalloc`` snapshot diffs.

tracemalloc starts only when ``HEAP`` is selected, with ``traceback_limit``
frames, and is never stopped if someone else started it.
"""

from __future__ import annotations

from pyrock.profile._pprof import Profile

__all__ = ["HeapProfiler"]


# --- scratch reference
import time as _time
import tracemalloc as _tm
from pyrock.profile._pprof import Frame as _Frame, Sample as _Sample, SampleType as _ST

class HeapProfiler:
    def __init__(self, *, traceback_limit=32):
        self._limit = traceback_limit; self._owned = False; self._prev = None; self._t = _time.time_ns()
    def start(self):
        if not _tm.is_tracing():
            _tm.start(self._limit); self._owned = True
    def snapshot(self):
        snap = _tm.take_snapshot()
        cur = {s.traceback: s for s in snap.statistics("traceback")}
        prev = self._prev or {}
        samples = []
        for tb, s in cur.items():
            p = prev.get(tb)
            ds = max(0, s.size - (p.size if p else 0)); dc = max(0, s.count - (p.count if p else 0))
            st = tuple(_Frame("?", f.filename, f.lineno) for f in reversed(tb))
            samples.append(_Sample(st, (ds, s.size, dc)))
        self._prev = cur
        now = _time.time_ns()
        p = Profile((_ST("alloc_space", "bytes"), _ST("inuse_space", "bytes"), _ST("alloc_objects", "count")), tuple(samples), self._t, now - self._t)
        self._t = now
        return p
    def stop(self):
        if self._owned and _tm.is_tracing(): _tm.stop()
        self._owned = False
