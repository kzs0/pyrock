"""Batch span processor: bounded queue, one worker thread, drop-on-full with stats."""

from __future__ import annotations

from dataclasses import dataclass

from pyrock.trace import Span, SpanExporter

__all__ = [
    "DEFAULT_EXPORT_TIMEOUT",
    "DEFAULT_MAX_BATCH_SIZE",
    "DEFAULT_MAX_QUEUE_SIZE",
    "DEFAULT_SCHEDULE_DELAY",
    "BatchProcessor",
    "BatchStats",
]

DEFAULT_MAX_QUEUE_SIZE = 2048
DEFAULT_MAX_BATCH_SIZE = 512
DEFAULT_SCHEDULE_DELAY = 5.0
DEFAULT_EXPORT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class BatchStats:
    """Cumulative export outcomes (Go: ``otlp.BatchStats``, plus Python extras)."""

    exported: int = 0
    """Spans successfully exported."""
    dropped: int = 0
    """Spans dropped because the queue was full (or after shutdown)."""
    failed: int = 0
    """Export calls that raised."""
    queue_len: int = 0
    """Spans currently queued."""


class BatchProcessor:
    """Queues ended spans and exports them in batches from a daemon thread.

    The thread starts lazily on the first span. ``on_end`` never blocks: when
    the queue is full the span is dropped and counted.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        schedule_delay: float = DEFAULT_SCHEDULE_DELAY,
        export_timeout: float = DEFAULT_EXPORT_TIMEOUT,
    ) -> None:
        raise NotImplementedError

    @property
    def exporter(self) -> SpanExporter:
        raise NotImplementedError

    def on_end(self, span: Span, /) -> None:
        """Enqueue a sampled span (non-blocking)."""
        raise NotImplementedError

    def force_flush(self, *, timeout: float | None = None) -> bool:
        """Export everything queued; ``False`` if the timeout expired first."""
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Flush, stop the worker and shut the exporter down (idempotent)."""
        raise NotImplementedError

    def stats(self) -> BatchStats:
        raise NotImplementedError

    def _after_fork_in_child(self) -> None:
        """Reset the queue and lock and restart lazily (called by the fork hook)."""
        raise NotImplementedError
