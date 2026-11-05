"""Private helpers for the core end-to-end tests (lifecycle, operations, logging, ...).

Everything here uses only pyrock's public API and the shared ``tests/support``
fakes. Nothing calls pyrock at import time, so these modules always collect.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from tests.support.prom import PromText, parse

T = TypeVar("T")

QUIET: dict[str, Any] = {
    "server_enabled": False,
    "disable_environment_detection": True,
    "runtime_metrics": False,
}
"""Overrides that keep an ``init()`` free of servers, probes and runtime series."""


def quiet(**overrides: Any) -> dict[str, Any]:
    """``QUIET`` plus ``log_install="none"`` (unless given) plus ``overrides``."""
    out: dict[str, Any] = {**QUIET, "log_install": "none"}
    out.update(overrides)
    return out


def isolated(fn: Callable[..., T]) -> Callable[..., T]:
    """Run a test in a brand-new, empty ``contextvars.Context``.

    An ``init()`` that is never deactivated (explicit ``shutdown()``) would
    otherwise leave its instance active for later tests in the same thread.
    ``functools.wraps`` keeps the signature so pytest still injects fixtures.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        return contextvars.Context().run(fn, *args, **kwargs)

    return wrapper


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion (the suite has no asyncio plugin)."""
    return asyncio.run(coro)


def prom_of(bedrock: Any) -> PromText:
    """Parse the Prometheus exposition of ``bedrock``'s registry."""
    from pyrock.metric import prometheus

    return parse(prometheus.encode(bedrock.metrics))


def prom_text(capture: Any) -> PromText:
    """Parse the exposition text of a ``pyrock.testing.capture()`` result."""
    return parse(capture.metrics.text())


def count(value: float | None) -> float:
    """A counter value where an absent series means zero (Go only creates hit series)."""
    return 0.0 if value is None else value


def span_attr_or_resource(span: dict[str, Any], key: str) -> Any:
    """Static attrs are exported as resource attrs (Go) or span attrs; accept either."""
    if key in span["resource"]:
        return span["resource"][key]
    return span["attributes"].get(key)


def spans_named(capture: Any, base: str) -> list[Any]:
    """Captured spans named ``base`` or a numbered duplicate ``base[n]``.

    Root-level duplicates may or may not be numbered (numbering is per parent),
    so tests that repeat a root operation match both forms.
    """
    return [s for s in capture.spans if s.name == base or s.name.startswith(base + "[")]
