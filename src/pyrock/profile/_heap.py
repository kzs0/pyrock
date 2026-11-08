"""Heap profiles from ``tracemalloc`` snapshot diffs.

tracemalloc starts only when ``HEAP`` is selected, with ``traceback_limit``
frames, and is never stopped if someone else started it. A forked child stops
tracing it inherited only if pyrock started it in the parent (see
:func:`_after_fork_in_child`); a child exporter selecting ``HEAP`` starts it
again and owns it.

Each :meth:`HeapProfiler.snapshot` compares the live traced memory with the
previous snapshot, per allocation traceback:

* ``inuse_space/bytes``: bytes still allocated there now;
* ``alloc_space/bytes`` and ``alloc_objects/count``: the growth since the
  previous snapshot (memory allocated there and still alive). tracemalloc only
  sees live blocks, so allocations freed between two snapshots are invisible.

tracemalloc records only file names and line numbers. Function names are
resolved from the code objects of loaded modules where possible, otherwise the
frame is named ``file:line``.
"""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time
import tracemalloc
import weakref
from collections import deque
from collections.abc import Iterable
from types import CodeType, FunctionType, ModuleType

from pyrock.profile._pprof import Frame, Profile, Sample, SampleType

__all__ = ["HeapProfiler"]

NOT_TRACING_NOTE = "tracemalloc is not tracing; enable HEAP profiling to collect allocations"

_SAMPLE_TYPES = (
    SampleType("alloc_space", "bytes"),
    SampleType("inuse_space", "bytes"),
    SampleType("alloc_objects", "count"),
)
_FILTERS = (tracemalloc.Filter(False, tracemalloc.__file__),)
_MAX_INDEXED_FILES = 4096

# Starting and stopping tracemalloc is process-wide: serialize ownership changes.
_tracing_lock = threading.Lock()
_owner: tuple[int, int] | None = None
"""``(pid, token)`` of the profiler that started tracing; guarded by ``_tracing_lock``.

The pid makes a claim inherited through ``fork`` recognisable as the parent's.
"""
_tokens = itertools.count(1)
_profilers: weakref.WeakSet[HeapProfiler] = weakref.WeakSet()
"""Live profilers, so a forked child can replace their (possibly held) locks."""


def _after_fork_in_child() -> None:
    """Make heap profiling usable in a forked child, acquiring no inherited lock.

    Every lock is replaced. If pyrock started tracemalloc in the parent, the
    child stops it: no profiler in the child owns it, so it would otherwise
    trace (and cost) forever. Tracing someone else started is left alone.
    """
    global _tracing_lock, _owner
    _tracing_lock = threading.Lock()
    for profiler in list(_profilers):
        profiler._lock = threading.Lock()
        profiler._previous = None
    if _owner is not None and _owner[0] != os.getpid():
        _owner = None
        if tracemalloc.is_tracing():
            tracemalloc.stop()


class HeapProfiler:
    """Takes snapshots and reports allocations since the previous one."""

    def __init__(self, *, traceback_limit: int = 32) -> None:
        if traceback_limit < 1:
            raise ValueError("traceback_limit must be >= 1")
        self._traceback_limit = traceback_limit
        self._token = next(_tokens)
        self._lock = threading.Lock()
        self._previous: tracemalloc.Snapshot | None = None
        self._previous_wall_ns = time.time_ns()
        self._names = _FunctionNames()
        _profilers.add(self)

    @property
    def _claim(self) -> tuple[int, int]:
        return (os.getpid(), self._token)

    def start(self) -> None:
        """Start tracemalloc if it is not already tracing (remembering who owns it).

        Tracing that pyrock started in a parent process (inherited through
        ``fork``) is adopted, so this profiler stops it later.
        """
        global _owner
        with _tracing_lock:
            claim = self._claim
            if _owner == claim:
                return
            if tracemalloc.is_tracing():
                if _owner is None or _owner[0] == claim[0]:
                    return  # someone else's tracing, or another pyrock profiler's here
            else:
                tracemalloc.start(self._traceback_limit)
            _owner = claim
        with self._lock:
            self._previous = None
            self._previous_wall_ns = time.time_ns()

    def snapshot(self) -> Profile:
        """``alloc_space/bytes``, ``inuse_space/bytes``, ``alloc_objects/count`` samples.

        Without tracing the profile is empty and says so in ``profile.note``.
        """
        with self._lock:
            now_ns = time.time_ns()
            start_ns, self._previous_wall_ns = self._previous_wall_ns, now_ns
            if not tracemalloc.is_tracing():
                self._previous = None
                return self._profile((), start_ns, now_ns, {"profile.note": NOT_TRACING_NOTE})
            current = tracemalloc.take_snapshot().filter_traces(_FILTERS)
            previous, self._previous = self._previous, current
            samples = tuple(self._samples(current, previous))
        return self._profile(samples, start_ns, now_ns, {})

    def stop(self) -> None:
        """Stop tracemalloc only if this profiler started it."""
        global _owner
        with _tracing_lock:
            if _owner != self._claim:
                return
            _owner = None
            tracemalloc.stop()
        with self._lock:
            self._previous = None

    def _samples(
        self, current: tracemalloc.Snapshot, previous: tracemalloc.Snapshot | None
    ) -> Iterable[Sample]:
        if previous is None:
            for stat in current.statistics("traceback"):
                yield Sample(self._stack(stat.traceback), (stat.size, stat.size, stat.count))
            return
        for diff in current.compare_to(previous, "traceback"):
            alloc, objects = max(diff.size_diff, 0), max(diff.count_diff, 0)
            if alloc or diff.size:
                yield Sample(self._stack(diff.traceback), (alloc, diff.size, objects))

    def _stack(self, traceback: tracemalloc.Traceback) -> tuple[Frame, ...]:
        # A Traceback iterates oldest frame first; profiles are leaf first.
        return tuple(self._names.frame(f.filename, f.lineno) for f in reversed(traceback))

    @staticmethod
    def _profile(
        samples: tuple[Sample, ...], start_ns: int, end_ns: int, attrs: dict[str, str]
    ) -> Profile:
        return Profile(
            sample_types=_SAMPLE_TYPES,
            samples=samples,
            start_time_ns=start_ns,
            duration_ns=max(end_ns - start_ns, 0),
            period_type=_SAMPLE_TYPES[0],
            attrs=attrs,
        )


class _FunctionNames:
    """Resolves ``(filename, line)`` to frames named after the enclosing function.

    Per file, the code objects reachable from the loaded module that defines it
    (functions, methods and their nested code) are indexed by line, innermost
    code winning. Not thread-safe; :class:`HeapProfiler` holds its lock.
    """

    def __init__(self) -> None:
        self._files: dict[str, tuple[str, dict[int, str]]] = {}
        self._frames: dict[tuple[str, int], Frame] = {}
        self._modules_seen = -1
        self._modules_by_file: dict[str, str] = {}

    def frame(self, filename: str, line: int) -> Frame:
        key = (filename, line)
        frame = self._frames.get(key)
        if frame is None:
            module, lines = self._index(filename)
            function = lines.get(line) or f"{_basename(filename)}:{line}"
            frame = self._frames[key] = Frame(function, filename, line, module)
        return frame

    def _index(self, filename: str) -> tuple[str, dict[int, str]]:
        found = self._files.get(filename)
        if found is None:
            if len(self._files) >= _MAX_INDEXED_FILES:
                self._files.clear()
                self._frames.clear()
            name = self._module_name(filename)
            module = sys.modules.get(name) if name else None
            lines = _line_index(module, filename) if module is not None else {}
            found = self._files[filename] = (name, lines)
        return found

    def _module_name(self, filename: str) -> str:
        modules = sys.modules.copy()
        if len(modules) != self._modules_seen:
            self._modules_seen = len(modules)
            self._modules_by_file = {
                path: name
                for name, module in modules.items()
                if isinstance(path := getattr(module, "__file__", None), str)
            }
        return self._modules_by_file.get(filename, "")


def _line_index(module: ModuleType, filename: str) -> dict[int, str]:
    """Line -> qualified name of the innermost code object of ``filename`` covering it."""
    lines: dict[int, str] = {}
    seen: set[int] = set()
    # Breadth first, so nested code is indexed after (and overrides) its parent.
    pending = deque(c for c in _module_codes(module) if c.co_filename == filename)
    while pending:
        code = pending.popleft()
        if id(code) in seen:
            continue
        seen.add(id(code))
        for _start, _end, line in code.co_lines():
            if line is not None:
                lines[line] = code.co_qualname
        pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
    return lines


def _module_codes(module: ModuleType) -> Iterable[CodeType]:
    """Code objects of the functions and methods defined in ``module`` (classes included)."""
    pending: list[object] = list(vars(module).values())
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, staticmethod | classmethod):
            value = value.__func__
        elif isinstance(value, property):
            pending.extend(f for f in (value.fget, value.fset, value.fdel) if f is not None)
            continue
        if isinstance(value, FunctionType):
            yield value.__code__
        elif isinstance(value, type) and value.__module__ == module.__name__:
            pending.extend(vars(value).values())


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]
