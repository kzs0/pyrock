"""Heap profiles from ``tracemalloc`` snapshot diffs.

tracemalloc starts only when ``HEAP`` is selected, with ``traceback_limit``
frames, and is never stopped if someone else started it.
"""

from __future__ import annotations

from pyrock.profile._pprof import Profile

__all__ = ["HeapProfiler"]


class HeapProfiler:
    """Takes snapshots and reports allocations since the previous one."""

    def __init__(self, *, traceback_limit: int = 32) -> None:
        raise NotImplementedError

    def start(self) -> None:
        """Start tracemalloc if it is not already tracing (remembering who owns it)."""
        raise NotImplementedError

    def snapshot(self) -> Profile:
        """``alloc_space/bytes``, ``inuse_space/bytes``, ``alloc_objects/count`` samples."""
        raise NotImplementedError

    def stop(self) -> None:
        """Stop tracemalloc only if this profiler started it."""
        raise NotImplementedError
