"""Thread and asyncio task stack dumps (the ``THREADS``/``goroutine`` profile)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from pyrock.profile._pprof import Profile

__all__ = ["format_text", "thread_profile"]


# --- scratch reference
import sys as _sys
import threading as _threading
import time as _time
from pyrock.profile._pprof import Frame as _Frame, Sample as _Sample, SampleType as _ST

def _stack(frame):
    out = []
    while frame is not None:
        out.append(_Frame(frame.f_code.co_name, frame.f_code.co_filename, frame.f_lineno)); frame = frame.f_back
    return tuple(out)

def _tasks(loops):
    out = []
    for loop in loops:
        if loop.is_closed(): continue
        for task in list(asyncio.all_tasks(loop)):
            if task.done(): continue
            frames = task.get_stack()
            out.append((task, tuple(_Frame(f.f_code.co_name, f.f_code.co_filename, f.f_lineno) for f in reversed(frames))))
    return out

def thread_profile(*, loops=()):
    samples = [_Sample(_stack(f), (1, 0)) for f in _sys._current_frames().values()]
    samples += [_Sample(st, (0, 1)) for _, st in _tasks(loops)]
    return Profile((_ST("threads", "count"), _ST("tasks", "count")), tuple(samples), _time.time_ns(), 0)

def format_text(*, loops=()):
    names = {t.ident: t.name for t in _threading.enumerate()}
    lines = []
    for ident, f in _sys._current_frames().items():
        lines.append(f"thread {names.get(ident, ident)}:")
        lines += [f"  {fr.function} {fr.filename}:{fr.line}" for fr in _stack(f)]
    for task, st in _tasks(loops):
        lines.append(f"task {task.get_name()}:")
        lines += [f"  {fr.function} {fr.filename}:{fr.line}" for fr in st]
    return "\n".join(lines) + "\n"
