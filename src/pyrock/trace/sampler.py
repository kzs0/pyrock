"""Samplers. As in Go, sampling controls export, not span creation.

These samplers follow DESIGN.md, which deliberately differs from Go:

* ``Ratio`` is deterministic on the low 8 bytes of the trace ID, so every
  service sees the same decision for a trace (Go's ``RatioSampler`` draws a
  random number per span).
* ``ParentBased`` drops spans whose parent is unsampled (Go consults the root
  sampler when the parent is unsampled).

.. code-block:: python

    pyrock.init(trace_sampler=sampler.ParentBased(sampler.Ratio(0.1)))
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pyrock.attr import AttrValue

__all__ = [
    "AlwaysOff",
    "AlwaysOn",
    "Decision",
    "ParentBased",
    "Ratio",
    "Sampler",
    "SamplingResult",
]


class Decision(enum.IntEnum):
    """Sampling decision (Go: ``SamplingDecision``)."""

    DROP = 0
    RECORD_ONLY = 1
    RECORD_AND_SAMPLE = 2


@dataclass(frozen=True, slots=True)
class SamplingResult:
    """The outcome of :meth:`Sampler.should_sample`."""

    decision: Decision
    attrs: Mapping[str, AttrValue] = field(default_factory=dict)

    @property
    def sampled(self) -> bool:
        """Whether the span is exported (``RECORD_AND_SAMPLE``)."""
        return self.decision is Decision.RECORD_AND_SAMPLE

    @property
    def recording(self) -> bool:
        """Whether the span records attributes and events."""
        return self.decision is not Decision.DROP


@runtime_checkable
class Sampler(Protocol):
    """Decides whether a new span is sampled.

    ``parent_sampled`` is ``None`` for a root span, otherwise the parent's
    *sampled* flag (local or remote).
    """

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult: ...


_ID_SPACE = 1 << 64
_SAMPLE = SamplingResult(Decision.RECORD_AND_SAMPLE)
_DROP = SamplingResult(Decision.DROP)


class AlwaysOn:
    """Samples every span (Go: ``AlwaysSampler``)."""

    __slots__ = ()

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        """Always ``RECORD_AND_SAMPLE``."""
        return _SAMPLE


class AlwaysOff:
    """Drops every span (Go: ``NeverSampler``)."""

    __slots__ = ()

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        """Always ``DROP``."""
        return _DROP


class Ratio:
    """Samples ``ratio`` of traces, deterministically on the trace ID (Go: ``RatioSampler``).

    Unlike Go (random per span), the decision depends only on the trace ID's
    low 8 bytes: a trace is sampled when that unsigned 64-bit number is below
    ``ratio * 2**64``. ``ratio`` must be in ``[0, 1]``; ``ValueError`` otherwise.
    """

    __slots__ = ("_bound", "_ratio")

    def __init__(self, ratio: float, /) -> None:
        if not 0.0 <= ratio <= 1.0:  # also rejects NaN
            raise ValueError(f"ratio must be in [0, 1], got {ratio!r}")
        self._ratio = ratio
        self._bound = int(ratio * _ID_SPACE)

    @property
    def ratio(self) -> float:
        """The configured sampling ratio."""
        return self._ratio

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        """Sample when the trace ID's low 8 bytes fall below the ratio bound."""
        return _SAMPLE if int.from_bytes(trace_id[8:16]) < self._bound else _DROP


class ParentBased:
    """Follows the parent's decision; uses ``root`` for root spans (the default sampler).

    A sampled parent gives a sampled child and an unsampled parent an
    unsampled (dropped) child. Go instead consults ``root`` when the parent is
    unsampled.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Sampler, /) -> None:
        self._root = root

    @property
    def root(self) -> Sampler:
        """The sampler consulted for root spans."""
        return self._root

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        """Follow ``parent_sampled``; delegate to ``root`` when there is no parent."""
        if parent_sampled is None:
            return self._root.should_sample(trace_id, name, None)
        return _SAMPLE if parent_sampled else _DROP
