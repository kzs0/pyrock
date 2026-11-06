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

    __slots__ = ("_propagators",)

    def __init__(self, *propagators: Propagator) -> None:
        self._propagators = propagators

    @property
    def propagators(self) -> tuple[Propagator, ...]:
        """The propagators, in the order they run."""
        return self._propagators

    def extract(self, carrier: Any) -> SpanContext | None:
        """Return the first context any propagator finds, or ``None``."""
        for propagator in self._propagators:
            context = propagator.extract(carrier)
            if context is not None:
                return context
        return None

    def inject(self, carrier: Any, span_context: SpanContext | None = None) -> None:
        """Let every propagator write ``span_context`` (default: the current span's)."""
        for propagator in self._propagators:
            propagator.inject(carrier, span_context)
