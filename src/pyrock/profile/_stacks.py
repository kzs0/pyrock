"""Thread and asyncio task stack dumps (the ``THREADS``/``goroutine`` profile).

Thread stacks come from ``sys._current_frames()`` and include the calling
thread. Tasks come from ``asyncio.all_tasks(loop)`` on the given loops; only
pending tasks are listed, each with the coroutine chain ``Task.get_stack``
reports. Loops are read from the outside without blocking them, so a task
that finishes concurrently may be listed once more.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from types import FrameType

from pyrock.profile._frames import StackKey, StackTable
from pyrock.profile._pprof import Frame, Profile, Sample, SampleType

__all__ = ["format_text", "thread_profile"]

_SAMPLE_TYPES = (SampleType("threads", "count"), SampleType("tasks", "count"))


def _pending_tasks(loops: Iterable[asyncio.AbstractEventLoop]) -> Iterator[asyncio.Task[object]]:
    for loop in loops:
        if loop.is_closed():
            continue
        try:
            tasks = asyncio.all_tasks(loop)
        except RuntimeError:  # the loop's task set changed while it was copied
            continue
        yield from (task for task in tasks if not task.done())


def _task_frames(task: asyncio.Task[object]) -> list[FrameType]:
    """The task's coroutine frames, leaf first (``get_stack`` is oldest first)."""
    return task.get_stack()[::-1]


def thread_profile(*, loops: Iterable[asyncio.AbstractEventLoop] = ()) -> Profile:
    """Stacks of all threads plus pending tasks on ``loops``.

    Sample types: ``threads/count`` and ``tasks/count``. Identical stacks are
    aggregated into one sample.
    """
    start_ns = time.time_ns()
    table = StackTable()
    counts: dict[StackKey, list[int]] = {}
    for frame in sys._current_frames().values():
        counts.setdefault(table.key(frame), [0, 0])[0] += 1
    for task in _pending_tasks(loops):
        counts.setdefault(table.key_of(_task_frames(task)), [0, 0])[1] += 1
    samples = tuple(Sample(table.frames(key), (t, k)) for key, (t, k) in counts.items())
    return Profile(
        sample_types=_SAMPLE_TYPES,
        samples=samples,
        start_time_ns=start_ns,
        duration_ns=0,
        period_type=_SAMPLE_TYPES[0],
        period=1,
    )


def _format_stack(lines: list[str], frames: Iterable[Frame]) -> None:
    for frame in frames:
        lines.append(f"    {frame.function}")
        lines.append(f"        {frame.filename}:{frame.line}")
    lines.append("")


def format_text(*, loops: Iterable[asyncio.AbstractEventLoop] = ()) -> str:
    """Human-readable stacks (``/debug/pprof/goroutine?debug=1``), leaf first."""
    names = {t.ident: t.name for t in threading.enumerate()}
    table = StackTable()
    lines: list[str] = []
    for ident, frame in sys._current_frames().items():
        lines.append(f'thread {ident} "{names.get(ident, "?")}":')
        _format_stack(lines, table.frames(table.key(frame)))
    for task in _pending_tasks(loops):
        lines.append(f'task "{task.get_name()}":')
        _format_stack(lines, table.frames(table.key_of(_task_frames(task))))
    return "\n".join(lines)
