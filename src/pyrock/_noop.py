"""Preallocated noop singletons used when no ``Bedrock`` is active.

``__enter__`` returns the object itself, methods do nothing, and decorators
return the function unchanged, so library code pays roughly one
``ContextVar.get()`` per call and allocates nothing in steady state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrock._operation import Operation, OperationScope, Source, SourceScope, Step, StepScope
from pyrock.metric import Counter, Gauge, Histogram

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock

__all__ = [
    "NoopCounter",
    "NoopGauge",
    "NoopHistogram",
    "NoopOperation",
    "NoopOperationScope",
    "NoopSource",
    "NoopSourceScope",
    "NoopStep",
    "NoopStepScope",
    "noop_bedrock",
]


class NoopOperation(Operation):
    """Does nothing; ``failed`` is always ``False``, IDs are ``None``."""


class NoopStep(Step):
    """Does nothing."""


class NoopSource(Source):
    """Does nothing."""


class NoopOperationScope(OperationScope):
    """Enters as :data:`NOOP_OPERATION`; decorating returns the function unchanged."""


class NoopStepScope(StepScope):
    """Enters as :data:`NOOP_STEP`; decorating returns the function unchanged."""


class NoopSourceScope(SourceScope):
    """Enters as :data:`NOOP_SOURCE`; decorating returns the function unchanged."""


class NoopCounter(Counter):
    """Ignores every update; ``labels()`` returns itself."""


class NoopGauge(Gauge):
    """Ignores every update; ``labels()`` returns itself."""


class NoopHistogram(Histogram):
    """Ignores every update; ``labels()`` returns itself; ``time()`` is a noop timer."""


def noop_bedrock() -> Bedrock:
    """Return the process-wide noop ``Bedrock`` (``is_noop`` is ``True``)."""
    raise NotImplementedError
