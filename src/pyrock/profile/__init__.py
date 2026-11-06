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
