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
