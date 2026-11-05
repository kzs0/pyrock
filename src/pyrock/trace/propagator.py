"""The propagator protocol (Go: ``trace.Propagator``) and a composite."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pyrock.trace import SpanContext

__all__ = ["CompositePropagator", "Propagator"]


@runtime_checkable
class Propagator(Protocol):
    """Reads and writes trace context on a transport-specific carrier.

    ``extract`` returns a remote :class:`SpanContext`, or ``None`` when the
    carrier has no valid context. ``inject`` writes ``span_context`` (default:
    the current span's context) and is a no-op when there is none.
    """

    def extract(self, carrier: Any) -> SpanContext | None: ...

    def inject(self, carrier: Any, span_context: SpanContext | None = None) -> None: ...


class CompositePropagator:
    """Runs several propagators: ``extract`` returns the first hit, ``inject`` calls all."""

    def __init__(self, *propagators: Propagator) -> None:
        raise NotImplementedError

    @property
    def propagators(self) -> tuple[Propagator, ...]:
        raise NotImplementedError

    def extract(self, carrier: Any) -> SpanContext | None:
        raise NotImplementedError

    def inject(self, carrier: Any, span_context: SpanContext | None = None) -> None:
        raise NotImplementedError
