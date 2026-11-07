"""Python runtime and process metrics (the equivalent of Go's runtime metrics).

Families: ``python_gc_collections_total{generation}``,
``python_gc_collected_total{generation}``,
``python_gc_uncollectable_total{generation}``, ``python_gc_pause_seconds``
(histogram, via ``gc.callbacks``), ``python_threads``,
``python_asyncio_tasks`` (tracked loops only), ``python_info{implementation,
version,gil}``, ``process_cpu_seconds_total``, ``process_resident_memory_bytes``,
``process_virtual_memory_bytes``, ``process_open_fds``, ``process_max_fds``,
``process_start_time_seconds`` and ``python_tracemalloc_traced_bytes`` (only
while tracemalloc is tracing). Every family takes the registry prefix and its
constant labels.

Process metrics come from ``/proc`` on Linux. Elsewhere (or without ``/proc``)
CPU time comes from :func:`time.process_time`, the descriptor limit from
``resource`` and open descriptors from ``/dev/fd`` where those exist.
"""

from __future__ import annotations

import collections
import contextlib
import gc
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pyrock.metric import Counter, Gauge, Registry

if TYPE_CHECKING:
    import asyncio

__all__ = ["RuntimeCollector"]

GC_PAUSE_BUCKETS = (0.00001, 0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0)
"""Bounds (seconds) of ``python_gc_pause_seconds``."""

_MAX_PENDING_PAUSES = 100_000
"""Pauses buffered between collects; older ones are discarded beyond this."""

_PROCESS_COUNTERS = ("process_cpu_seconds_total",)
_PROCESS_GAUGES = (
    "process_resident_memory_bytes",
    "process_virtual_memory_bytes",
    "process_open_fds",
    "process_max_fds",
    "process_start_time_seconds",
)

_HELP = {
    "python_gc_collections_total": "Garbage collections, per generation.",
    "python_gc_collected_total": "Objects collected by the garbage collector, per generation.",
    "python_gc_uncollectable_total": "Uncollectable objects found by the garbage collector.",
    "python_gc_pause_seconds": "Duration of garbage collection pauses.",
    "python_threads": "Live Python threads.",
    "python_asyncio_tasks": "Unfinished asyncio tasks on tracked event loops.",
    "python_info": "Python implementation, version and GIL state.",
    "python_tracemalloc_traced_bytes": "Memory currently traced by tracemalloc.",
    "process_cpu_seconds_total": "Total user and system CPU time in seconds.",
    "process_resident_memory_bytes": "Resident memory size in bytes.",
    "process_virtual_memory_bytes": "Virtual memory size in bytes.",
    "process_open_fds": "Open file descriptors.",
    "process_max_fds": "Maximum number of open file descriptors.",
    "process_start_time_seconds": "Start time of the process since the Unix epoch in seconds.",
}

# Field positions in /proc/<pid>/stat after the ")" closing the command name.
_STAT_UTIME, _STAT_STIME, _STAT_STARTTIME, _STAT_VSIZE, _STAT_RSS = 11, 12, 19, 20, 21


# --------------------------------------------------------------------- /proc


def _read_proc(
    proc_root: str = "/proc",
    /,
    *,
    pid: str = "self",
    clock_ticks: int | None = None,
    page_size: int | None = None,
) -> dict[str, float]:
    """Read Linux process metrics from ``{proc_root}/{pid}`` and ``{proc_root}/stat``.

    Returns a mapping from metric family name to value for whichever of these
    could be read: ``process_cpu_seconds_total`` (utime + stime from ``stat``,
    divided by ``clock_ticks``), ``process_virtual_memory_bytes`` (vsize),
    ``process_resident_memory_bytes`` (rss pages times ``page_size``),
    ``process_start_time_seconds`` (starttime / ``clock_ticks`` plus ``btime``
    from ``{proc_root}/stat``), ``process_open_fds`` (entries in ``fd/``) and
    ``process_max_fds`` (the soft "Max open files" limit in ``limits``).
    The ``stat`` line is parsed after the **last** ``)`` so process names
    containing spaces or parentheses are handled.

    ``clock_ticks`` / ``page_size`` default to ``os.sysconf`` values. A missing
    or unreadable file omits only the metrics derived from it; a missing
    ``proc_root`` yields ``{}``. Never raises for I/O or parse errors.
    :meth:`RuntimeCollector.collect` calls this through the module global, so
    tests may replace it.
    """
    if not os.path.isdir(proc_root):
        return {}
    base = os.path.join(proc_root, pid)
    ticks = clock_ticks or _sysconf("SC_CLK_TCK")
    page = page_size or _sysconf("SC_PAGE_SIZE")
    out: dict[str, float] = {}
    stat = _read_text(os.path.join(base, "stat"))
    if stat is not None and ticks and page:
        out.update(_parse_stat(stat, ticks, page, _boot_time(proc_root)))
    with contextlib.suppress(OSError):
        out["process_open_fds"] = len(os.listdir(os.path.join(base, "fd")))
    limits = _read_text(os.path.join(base, "limits"))
    max_fds = None if limits is None else _parse_max_open_files(limits)
    if max_fds is not None:
        out["process_max_fds"] = max_fds
    return out


def _sysconf(name: str, /) -> int | None:
    try:
        return os.sysconf(name)
    except (AttributeError, OSError, ValueError):
        return None


def _read_text(path: str, /) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _parse_stat(text: str, ticks: int, page: int, btime: float | None, /) -> dict[str, float]:
    """Metrics from one ``/proc/<pid>/stat`` line; ``{}`` if it does not parse."""
    _, paren, rest = text.rpartition(")")
    fields = rest.split()
    if not paren or len(fields) <= _STAT_RSS:
        return {}
    try:
        cpu = (int(fields[_STAT_UTIME]) + int(fields[_STAT_STIME])) / ticks
        start = int(fields[_STAT_STARTTIME]) / ticks
        vsize = float(int(fields[_STAT_VSIZE]))
        rss = float(int(fields[_STAT_RSS]) * page)
    except ValueError:
        return {}
    out = {
        "process_cpu_seconds_total": cpu,
        "process_virtual_memory_bytes": vsize,
        "process_resident_memory_bytes": rss,
    }
    if btime is not None:
        out["process_start_time_seconds"] = btime + start
    return out


def _boot_time(proc_root: str, /) -> float | None:
    text = _read_text(os.path.join(proc_root, "stat"))
    for line in (text or "").splitlines():
        name, _, value = line.partition(" ")
        if name == "btime":
            try:
                return float(int(value))
            except ValueError:
                return None
    return None


def _parse_max_open_files(text: str, /) -> float | None:
    """The soft "Max open files" limit, or ``None`` if absent or unlimited."""
    for line in text.splitlines():
        if line.startswith("Max open files"):
            soft = line[len("Max open files") :].split()[:1]
            return float(soft[0]) if soft and soft[0].isdigit() else None
    return None


def _fallback_process() -> dict[str, float]:
    """Portable process metrics for platforms without ``/proc``."""
    out = {"process_cpu_seconds_total": time.process_time()}
    try:
        import resource  # POSIX only, imported on demand
    except ImportError:
        pass
    else:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != resource.RLIM_INFINITY:
            out["process_max_fds"] = float(soft)
    with contextlib.suppress(OSError):
        out["process_open_fds"] = float(len(os.listdir("/dev/fd")))
    return out


# --------------------------------------------------------------------- python


def _python_version() -> str:
    """``3.13.1``, or ``3.15.0b1`` for a pre-release."""
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    if v.releaselevel != "final":
        version += {"alpha": "a", "beta": "b", "candidate": "rc"}[v.releaselevel] + str(v.serial)
    return version


def _gil_enabled() -> bool:
    """True unless a free-threaded build runs with the GIL disabled (3.13+)."""
    is_gil_enabled: Callable[[], bool] | None = getattr(sys, "_is_gil_enabled", None)
    return True if is_gil_enabled is None else is_gil_enabled()


class _GCPauseRecorder:
    """A ``gc.callbacks`` entry that buffers pause durations without taking locks.

    The callback can run in the middle of any allocation, including while a
    metric lock is held by the same thread, so it only appends to a deque;
    :meth:`RuntimeCollector.collect` moves the pauses into the histogram.
    """

    __slots__ = ("_start", "pending")

    def __init__(self) -> None:
        self._start = 0.0
        self.pending: collections.deque[float] = collections.deque(maxlen=_MAX_PENDING_PAUSES)

    def __call__(self, phase: str, info: Mapping[str, Any], /) -> None:
        if phase == "start":
            self._start = time.perf_counter()
        else:
            self.pending.append(time.perf_counter() - self._start)


def _remove_gc_callback(callback: _GCPauseRecorder, /) -> None:
    if callback in gc.callbacks:
        gc.callbacks.remove(callback)


# ------------------------------------------------------------------ collector


class RuntimeCollector:
    """Registers runtime families on ``registry`` and refreshes them on each collect.

    The collector registers itself with the registry, so every snapshot (scrape
    or push) is fresh, and adds exactly one ``gc.callbacks`` entry. Call
    :meth:`close` to undo both.
    """

    def __init__(self, registry: Registry, /) -> None:
        self._registry = registry
        self._lock = threading.Lock()
        self._loops: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()
        self._info_labels: dict[str, str] | None = None

        def counter(name: str, *labels: str) -> Counter:
            return registry.counter(name, _HELP.get(name, ""), labels=labels)

        def gauge(name: str, *labels: str) -> Gauge:
            return registry.gauge(name, _HELP.get(name, ""), labels=labels)

        self._gc_collections = counter("python_gc_collections_total", "generation")
        self._gc_collected = counter("python_gc_collected_total", "generation")
        self._gc_uncollectable = counter("python_gc_uncollectable_total", "generation")
        self._gc_pauses = registry.histogram(
            "python_gc_pause_seconds",
            _HELP["python_gc_pause_seconds"],
            buckets=GC_PAUSE_BUCKETS,
        )
        self._threads = gauge("python_threads")
        self._asyncio_tasks = gauge("python_asyncio_tasks")
        self._info = gauge("python_info", "implementation", "version", "gil")
        self._tracemalloc = gauge("python_tracemalloc_traced_bytes")
        self._process_counters = {name: counter(name) for name in _PROCESS_COUNTERS}
        self._process_gauges = {name: gauge(name) for name in _PROCESS_GAUGES}

        recorder = _GCPauseRecorder()
        self._pauses = recorder.pending
        gc.callbacks.append(recorder)
        self._finalizer = weakref.finalize(self, _remove_gc_callback, recorder)
        registry.register_collector(self)

    def close(self) -> None:
        """Remove the gc callback and stop refreshing on snapshots (idempotent)."""
        self._finalizer()
        self._registry.unregister_collector(self)

    def track_loop(self, loop: asyncio.AbstractEventLoop, /) -> None:
        """Count tasks on ``loop`` in ``python_asyncio_tasks`` (held weakly)."""
        with self._lock:
            self._loops.add(loop)

    def collect(self) -> None:
        """Refresh every runtime family."""
        self._collect_gc()
        self._threads.set(threading.active_count())
        self._collect_asyncio()
        self._collect_info()
        self._collect_process()
        self._collect_tracemalloc()

    def _collect_gc(self) -> None:
        for generation, stats in enumerate(gc.get_stats()):
            labels = {"generation": str(generation)}
            self._gc_collections._set(stats["collections"], labels)
            self._gc_collected._set(stats["collected"], labels)
            self._gc_uncollectable._set(stats["uncollectable"], labels)
        pauses = self._pauses
        while pauses:
            try:
                self._gc_pauses.observe(pauses.popleft())
            except IndexError:  # pragma: no cover - drained concurrently
                break

    def _collect_asyncio(self) -> None:
        with self._lock:
            loops = list(self._loops)
        live = [loop for loop in loops if not loop.is_closed()]
        if not live:
            self._asyncio_tasks._clear()
            return
        self._asyncio_tasks.set(sum(_count_tasks(loop) for loop in live))

    def _collect_info(self) -> None:
        labels = {
            "implementation": sys.implementation.name,
            "version": _python_version(),
            "gil": "true" if _gil_enabled() else "false",
        }
        if labels != self._info_labels:  # the GIL can be re-enabled at runtime
            self._info._clear()
            self._info_labels = labels
        self._info.labels(labels).set(1)

    def _collect_process(self) -> None:
        values = _read_proc()
        if len(values) < len(_PROCESS_COUNTERS) + len(_PROCESS_GAUGES):
            for name, value in _fallback_process().items():
                values.setdefault(name, value)
        for name, value in values.items():
            if name in self._process_counters:
                self._process_counters[name]._set(value)
            elif name in self._process_gauges:
                self._process_gauges[name].set(value)

    def _collect_tracemalloc(self) -> None:
        tracemalloc = sys.modules.get("tracemalloc")  # never imported just to check
        if tracemalloc is not None and tracemalloc.is_tracing():
            self._tracemalloc.set(tracemalloc.get_traced_memory()[0])
        else:
            self._tracemalloc._clear()


def _count_tasks(loop: asyncio.AbstractEventLoop, /) -> int:
    """Unfinished tasks on ``loop``; 0 if they cannot be listed from this thread."""
    import asyncio  # already imported by whoever owns ``loop``

    try:
        return len(asyncio.all_tasks(loop))
    except RuntimeError:
        return 0
