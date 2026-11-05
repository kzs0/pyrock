"""Preallocated noop singletons used when no ``Bedrock`` is active.

``__enter__`` returns the object itself and methods do nothing, so library
code pays roughly one ``ContextVar.get()`` per call and allocates nothing in
steady state.

Decorators from the module helpers (``pyrock.operation`` etc.) always wrap and
resolve the active instance at call time, so code decorated before ``init()``
is instrumented later. A noop scope returns the function unchanged only when
it came from an explicit noop instance (``bedrock=noop_bedrock()`` or
``noop_bedrock().operation(...)``).
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
    """Enters as :data:`NOOP_OPERATION`.

    Decorating returns the function unchanged (explicit noop ``bedrock=`` only).
    """


class NoopStepScope(StepScope):
    """Enters as :data:`NOOP_STEP`.

    Decorating returns the function unchanged (explicit noop ``bedrock=`` only).
    """


class NoopSourceScope(SourceScope):
    """Enters as :data:`NOOP_SOURCE`.

    Decorating returns the function unchanged (explicit noop ``bedrock=`` only).
    """


class NoopCounter(Counter):
    """Ignores every update; ``labels()`` returns itself."""


class NoopGauge(Gauge):
    """Ignores every update; ``labels()`` returns itself."""


class NoopHistogram(Histogram):
    """Ignores every update; ``labels()`` returns itself; ``time()`` is a noop timer."""


def noop_bedrock() -> Bedrock:
    """Return the process-wide noop ``Bedrock`` (``is_noop`` is ``True``)."""
    raise NotImplementedError
