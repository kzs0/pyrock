"""Batch span processor: bounded queue, one worker thread, drop-on-full with stats."""

from __future__ import annotations

import collections
import contextlib
import logging
import threading
import time
from dataclasses import dataclass

from pyrock._transport import forward_fork, remaining
from pyrock.trace import Span, SpanExporter
from pyrock.trace.otlp import ExportError

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


FAILURE_LOG_INTERVAL = 60.0
"""Seconds between export-failure warnings after the first one."""

_log = logging.getLogger("pyrock")


class BatchProcessor:
    """Queues ended spans and exports them in batches from a daemon thread.

    The thread starts lazily on the first span. ``on_end`` never blocks: when
    the queue is full the newest span is dropped and counted. A batch is sent
    when ``max_batch_size`` spans are queued, ``schedule_delay`` seconds after
    the previous batch, on :meth:`force_flush` and on :meth:`shutdown`. Each
    export call gets ``export_timeout``; an exception from the exporter counts
    as one failed call and never stops the worker.

    Failures are also logged as a warning on the ``pyrock`` logger: the first
    one at once, then at most one every :data:`FAILURE_LOG_INTERVAL` seconds
    (with the number of failures since the last warning). The warning carries
    the exception type, plus the message for an
    :class:`~pyrock.trace.otlp.ExportError` (whose messages pyrock writes and
    never include credentials); other exceptions' messages are not logged.
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
        if max_queue_size < 1 or max_batch_size < 1:
            raise ValueError("max_queue_size and max_batch_size must be positive")
        if not schedule_delay > 0 or not export_timeout > 0:
            raise ValueError("schedule_delay and export_timeout must be positive")
        self._exporter = exporter
        self._max_queue_size = max_queue_size
        self._max_batch_size = max_batch_size
        self._schedule_delay = min(schedule_delay, threading.TIMEOUT_MAX)
        self._export_timeout = export_timeout
        self._shut_down = False
        self._reset()

    def _reset(self) -> None:
        """Fresh queue, lock, counters and no worker (construction and after fork)."""
        lock = threading.Lock()
        self._work = threading.Condition(lock)  # wakes the worker
        self._done = threading.Condition(lock)  # wakes force_flush waiters
        self._queue: collections.deque[Span] = collections.deque()
        self._worker: threading.Thread | None = None
        # Sequence numbers of accepted spans: taken by the worker, then completed.
        self._accepted = 0
        self._taken = 0
        self._completed = 0
        self._flush_target = 0
        self._exported = 0
        self._dropped = 0
        self._failed = 0
        self._last_failure_log: float | None = None
        self._unlogged_failures = 0

    @property
    def exporter(self) -> SpanExporter:
        """The exporter batches are sent to."""
        return self._exporter

    def on_end(self, span: Span, /) -> None:
        """Enqueue a sampled span (non-blocking)."""
        with self._work:
            queue = self._queue
            if self._shut_down or len(queue) >= self._max_queue_size:
                self._dropped += 1
                return
            queue.append(span)
            self._accepted += 1
            if self._worker is None:
                self._locked_start_worker()
            elif len(queue) == 1 or len(queue) == self._max_batch_size:
                self._work.notify()  # starts the schedule, or a full batch is ready

    def force_flush(self, *, timeout: float | None = None) -> bool:
        """Export everything queued; ``False`` if the timeout expired first.

        Spans already handed to the exporter count as done whether or not the
        export succeeded.
        """
        with self._work:
            target = self._accepted
            if self._completed >= target:
                return True
            self._flush_target = max(self._flush_target, target)
            self._work.notify()
            return self._done.wait_for(lambda: self._completed >= target, timeout)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Flush, stop the worker and shut the exporter down (idempotent).

        Spans still queued when ``timeout`` expires are dropped and counted.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._work:
            if self._shut_down:
                return
            self._shut_down = True
            self._work.notify()
            worker = self._worker
        if worker is not None:
            worker.join(remaining(deadline))
        with self._work:
            self._locked_discard_queue()
        self._exporter.shutdown(timeout=remaining(deadline))

    def stats(self) -> BatchStats:
        """A consistent snapshot of the counters."""
        with self._work:
            return BatchStats(self._exported, self._dropped, self._failed, len(self._queue))

    def _after_fork_in_child(self) -> None:
        """Reset the queue and lock and restart lazily (called by the fork hook).

        The parent's queued spans and counters are discarded (they belong to
        the parent). The exporter's own ``_after_fork_in_child`` runs too,
        when it has one, so it never reuses the parent's connection.
        """
        self._reset()
        forward_fork(self._exporter)

    # -- worker (all helpers named _locked_* run with the lock held) --------

    def _locked_start_worker(self) -> None:
        worker = threading.Thread(target=self._run, name="pyrock-batch-export", daemon=True)
        self._worker = worker
        worker.start()

    def _run(self) -> None:
        while True:
            with self._work:
                batch = self._locked_next_batch()
            if batch is None:
                return
            self._export(batch)

    def _locked_next_batch(self) -> list[Span] | None:
        """Wait until a batch is due and take it; ``None`` once stopped and drained.

        An idle worker sleeps until a span arrives; the schedule delay then
        bounds how long queued spans wait.
        """
        queue = self._queue
        deadline = time.monotonic() + self._schedule_delay
        while not self._locked_batch_due():
            if not queue:
                if self._shut_down:
                    return None
                self._work.wait()
                deadline = time.monotonic() + self._schedule_delay
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._work.wait(remaining)
        size = min(len(queue), self._max_batch_size)
        self._taken += size
        return [queue.popleft() for _ in range(size)]

    def _locked_batch_due(self) -> bool:
        queue = self._queue
        return bool(queue) and (
            len(queue) >= self._max_batch_size
            or self._shut_down
            or self._flush_target > self._taken
        )

    def _export(self, batch: list[Span]) -> None:
        error: Exception | None = None
        try:
            self._exporter.export(batch, timeout=self._export_timeout)
        except Exception as exc:  # counted; the worker must survive any exporter failure
            error = exc
        report = 0
        with self._work:
            if error is None:
                self._exported += len(batch)
            else:
                self._failed += 1
                report = self._locked_failure_report()
            self._completed += len(batch)
            self._done.notify_all()
        if error is not None and report:
            _log_failure(error, report, len(batch))

    def _locked_failure_report(self) -> int:
        """Failures to report now (this one included), or 0 while rate-limited."""
        self._unlogged_failures += 1
        now = time.monotonic()
        last = self._last_failure_log
        if last is not None and now - last < FAILURE_LOG_INTERVAL:
            return 0
        self._last_failure_log = now
        count, self._unlogged_failures = self._unlogged_failures, 0
        return count

    def _locked_discard_queue(self) -> None:
        """Drop what a timed-out shutdown left queued, releasing flush waiters."""
        left = len(self._queue)
        if left:
            self._queue.clear()
            self._dropped += left
            self._taken += left
            self._completed += left
            self._done.notify_all()


def _log_failure(error: Exception, failures: int, spans: int) -> None:
    """Warn about failed exports without leaking anything an exception might carry."""
    detail = type(error).__name__
    if isinstance(error, ExportError) and str(error):
        detail = f"{detail}: {error}"
    with contextlib.suppress(Exception):  # logging must never break the export worker
        _log.warning(
            "trace export failed, %d span(s) dropped: %s"
            " (%d failed export(s) since the last warning)",
            spans,
            detail,
            failures,
        )
