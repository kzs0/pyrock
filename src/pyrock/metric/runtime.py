"""Python runtime and process metrics (the equivalent of Go's runtime metrics).

Families: ``python_gc_collections_total{generation}``,
``python_gc_collected_total``, ``python_gc_uncollectable_total``,
``python_gc_pause_seconds`` (histogram, via ``gc.callbacks``),
``python_threads``, ``python_asyncio_tasks`` (tracked loops only),
``python_info{implementation,version,gil}``, ``process_cpu_seconds_total``,
``process_resident_memory_bytes``, ``process_virtual_memory_bytes``,
``process_open_fds``, ``process_max_fds``, ``process_start_time_seconds`` and
``python_tracemalloc_traced_bytes`` (only while tracemalloc is tracing).
"""

from __future__ import annotations

import asyncio

from pyrock.metric import Registry

__all__ = ["RuntimeCollector"]


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
    raise NotImplementedError


class RuntimeCollector:
    """Registers runtime families on ``registry`` and refreshes them on each collect."""

    def __init__(self, registry: Registry, /) -> None:
        raise NotImplementedError

    def collect(self) -> None:
        raise NotImplementedError

    def track_loop(self, loop: asyncio.AbstractEventLoop, /) -> None:
        """Count tasks on ``loop`` in ``python_asyncio_tasks`` (held weakly)."""
        raise NotImplementedError

    def close(self) -> None:
        """Remove the ``gc`` callback (idempotent)."""
        raise NotImplementedError
