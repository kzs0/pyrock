"""Samplers. As in Go, sampling controls export, not span creation.

``Ratio`` is deterministic on the low 8 bytes of the trace ID (the same rule as
Go), so mixed-language traces make the same decision.

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


class AlwaysOn:
    """Samples every span (Go: ``AlwaysSampler``)."""

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        raise NotImplementedError


class AlwaysOff:
    """Drops every span (Go: ``NeverSampler``)."""

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        raise NotImplementedError


class Ratio:
    """Samples ``ratio`` of traces, deterministically on the trace ID (Go: ``RatioSampler``).

    ``ratio`` must be in ``[0, 1]``; ``ValueError`` otherwise.
    """

    def __init__(self, ratio: float, /) -> None:
        raise NotImplementedError

    @property
    def ratio(self) -> float:
        raise NotImplementedError

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        raise NotImplementedError


class ParentBased:
    """Follows the parent's decision; uses ``root`` for root spans (the default sampler)."""

    def __init__(self, root: Sampler, /) -> None:
        raise NotImplementedError

    @property
    def root(self) -> Sampler:
        raise NotImplementedError

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        raise NotImplementedError
