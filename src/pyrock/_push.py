"""Background Prometheus metrics push (Go: ``startMetricsPush``).

Each interval POSTs exposition text with ``Content-Type`` set to the
Prometheus text type. Every request has a bounded lifetime; if a push is still
in flight the next tick is skipped; :meth:`MetricsPusher.stop` performs a
final flush bounded by the caller's timeout.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pyrock.metric import Registry

__all__ = ["MetricsPusher", "PushStats"]


@dataclass(frozen=True, slots=True)
class PushStats:
    pushed: int = 0
    failed: int = 0
    skipped: int = 0


class MetricsPusher:
    """Pushes ``registry`` to ``endpoint`` every ``interval`` seconds from a daemon thread."""

    def __init__(
        self,
        registry: Registry,
        /,
        *,
        endpoint: str,
        headers: Mapping[str, str] | None = None,
        interval: float = 15.0,
        timeout: float = 10.0,
    ) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def push_once(self, *, timeout: float | None = None) -> bool:
        """Push now; ``False`` on failure or when a push is already in flight."""
        raise NotImplementedError

    def stop(self, *, timeout: float | None = None, final_flush: bool = True) -> None:
        raise NotImplementedError

    def stats(self) -> PushStats:
        raise NotImplementedError
