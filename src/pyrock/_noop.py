"""Preallocated noop metrics returned by the helpers when no ``Bedrock`` is active.

Every method does nothing and allocates nothing; ``labels()`` returns the
metric itself, so ``pyrock.counter("x").labels(k="v").inc()`` is free in an
application that never calls ``init()``.

The other noop singletons sit next to what they stand in for: the noop
operation, step and source in :mod:`pyrock._operation`, the noop instance
(``pyrock.current()`` with nothing active) in :mod:`pyrock._bedrock`.
"""

from __future__ import annotations

from typing import Any

from pyrock.attr import AttrValue
from pyrock.metric import Counter, Gauge, Histogram, Timer

__all__ = [
    "NOOP_COUNTER",
    "NOOP_GAUGE",
    "NOOP_HISTOGRAM",
    "NoopCounter",
    "NoopGauge",
    "NoopHistogram",
]


def _nothing(_: Any) -> None:
    """What the noop histogram's timer observes into."""


class NoopCounter(Counter):
    """Ignores every update; ``labels()`` returns itself."""

    __slots__ = ()

    def __init__(self) -> None:
        """Build the singleton (not registered anywhere)."""

    def labels(self, mapping: Any = None, /, **labels: AttrValue) -> NoopCounter:
        return self

    def inc(self, amount: float = 1.0, /) -> None:
        pass

    def add(self, amount: float, /) -> None:
        pass


class NoopGauge(Gauge):
    """Ignores every update; ``labels()`` returns itself."""

    __slots__ = ()

    def __init__(self) -> None:
        """Build the singleton (not registered anywhere)."""

    def labels(self, mapping: Any = None, /, **labels: AttrValue) -> NoopGauge:
        return self

    def set(self, value: float, /) -> None:
        pass

    def add(self, amount: float, /) -> None:
        pass

    def inc(self, amount: float = 1.0, /) -> None:
        pass

    def dec(self, amount: float = 1.0, /) -> None:
        pass

    def sub(self, amount: float, /) -> None:
        pass


class NoopHistogram(Histogram):
    """Ignores every update; ``labels()`` returns itself; ``time()`` is a noop timer."""

    __slots__ = ()

    def __init__(self) -> None:
        """Build the singleton (not registered anywhere)."""

    def labels(self, mapping: Any = None, /, **labels: AttrValue) -> NoopHistogram:
        return self

    def observe(self, value: float, /) -> None:
        pass

    def time(self) -> Timer:
        return Timer(_nothing)


NOOP_COUNTER = NoopCounter()
NOOP_GAUGE = NoopGauge()
NOOP_HISTOGRAM = NoopHistogram()
