"""Continuous profiling built from stdlib pieces, exported as OTLP Profiles.

=========== ===============================================================
``CPU``     sampler thread (100 Hz) keeping threads whose CPU clock advanced
``WALL``    the same sampler without the CPU filter (Python extra)
``HEAP``    ``tracemalloc`` snapshot diffs per interval
``THREADS`` thread stacks plus pending asyncio tasks (alias ``GOROUTINE``)
``MUTEX``   wait times recorded by the instrumented :class:`Lock`/:class:`RLock`
``BLOCK``   wait times recorded by :class:`Condition`/:class:`Semaphore`
=========== ===============================================================

``HEAP`` starts tracemalloc only if it is not already tracing, and stopping the
exporter stops tracemalloc only if pyrock started it. Every upload carries an
``x-profile-type`` header with the :class:`ProfileType` value.

This package is never imported by ``import pyrock``; it loads on first use.
"""

from __future__ import annotations

from pyrock.profile import _heap, _locks, _sampler
from pyrock.profile._config import (
    DEFAULT_PROFILE_TYPES,
    ProfileExportConfig,
    ProfileExportStats,
    ProfileType,
)
from pyrock.profile._export import ProfileExporter, start_export
from pyrock.profile._locks import Condition, Lock, RLock, Semaphore

__all__ = [
    "DEFAULT_PROFILE_TYPES",
    "Condition",
    "Lock",
    "ProfileExportConfig",
    "ProfileExportStats",
    "ProfileExporter",
    "ProfileType",
    "RLock",
    "Semaphore",
    "start_export",
]


def _after_fork_in_child() -> None:
    """Reset process-wide profiling state in a forked child (pyrock's fork hook calls it).

    Acquires no lock: any lock a parent thread held at fork time is replaced.
    The sampler slot is freed, contention recorders are rebuilt empty, heap
    profiler locks are replaced, and tracemalloc tracing that pyrock started in
    the parent is stopped. Call it before restarting profile exporters in the
    child (claims made by the child itself are kept either way). Call it only
    when ``pyrock.profile`` is already imported (``sys.modules``), so a fork
    never imports profiling.
    """
    _sampler._after_fork_in_child()
    _locks._after_fork_in_child()
    _heap._after_fork_in_child()
