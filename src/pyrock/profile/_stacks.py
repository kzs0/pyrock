"""Thread and asyncio task stack dumps (the ``THREADS``/``goroutine`` profile)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from pyrock.profile._pprof import Profile

__all__ = ["format_text", "thread_profile"]


def thread_profile(*, loops: Iterable[asyncio.AbstractEventLoop] = ()) -> Profile:
    """Stacks of all threads plus pending tasks on ``loops``.

    Sample types: ``threads/count`` and ``tasks/count``.
    """
    raise NotImplementedError


def format_text(*, loops: Iterable[asyncio.AbstractEventLoop] = ()) -> str:
    """Human-readable stacks (``/debug/pprof/goroutine?debug=1``)."""
    raise NotImplementedError
