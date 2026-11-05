"""``BatchProcessor`` (DESIGN §7.3): bounded queue, batches, schedule, stats, shutdown, fork.

Waits use conditions on a fake exporter, never sleeps. Spans are light fakes:
the processor only queues them and hands them to the exporter.
"""

from __future__ import annotations

import dataclasses
import json
import os
import select
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from typing import Any, cast

import pytest

from pyrock.trace import Span, SpanContext
from pyrock.trace.otlp.batch import (
    DEFAULT_EXPORT_TIMEOUT,
    DEFAULT_MAX_BATCH_SIZE,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_SCHEDULE_DELAY,
    BatchProcessor,
    BatchStats,
)

WAIT = 5.0
NEVER = 3600.0


class FakeSpan:
    """Quacks like an ended, sampled :class:`Span` for the processor's purposes."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.context = SpanContext(b"\x01" * 16, b"\x02" * 8, 1)
        self.ended = True
        self.is_recording = False

    def __repr__(self) -> str:
        return f"FakeSpan({self.name!r})"


def spans(n: int, prefix: str = "s") -> list[Span]:
    return [cast("Span", FakeSpan(f"{prefix}{i}")) for i in range(n)]


class FakeExporter:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.batches: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.calls = 0
        self.shutdowns = 0
        self.fail_calls: set[int] = set()
        self.gate = threading.Event()
        self.gate.set()

    def export(self, batch: Sequence[Span], /, *, timeout: float | None = None) -> None:
        with self.cond:
            self.calls += 1
            call = self.calls
            self.timeouts.append(timeout)
            self.cond.notify_all()
        self.gate.wait(WAIT * 4)
        if call in self.fail_calls:
            with self.cond:
                self.cond.notify_all()
            raise RuntimeError(f"export {call} failed")
        with self.cond:
            self.batches.append([cast("FakeSpan", s).name for s in batch])
            self.cond.notify_all()

    def shutdown(self, *, timeout: float | None = None) -> None:
        with self.cond:
            self.shutdowns += 1

    @property
    def exported(self) -> list[str]:
        with self.cond:
            return [name for batch in self.batches for name in batch]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = WAIT) -> None:
        with self.cond:
            assert self.cond.wait_for(predicate, timeout), "timed out waiting for the exporter"


@pytest.fixture
def exporter() -> Iterator[FakeExporter]:
    exp = FakeExporter()
    yield exp
    exp.gate.set()


def _processor(exporter: FakeExporter, **kwargs: Any) -> BatchProcessor:
    kwargs.setdefault("schedule_delay", NEVER)
    return BatchProcessor(exporter, **kwargs)


# ---------------------------------------------------------------- data types


def test_defaults_match_go() -> None:
    assert DEFAULT_MAX_QUEUE_SIZE == 2048
    assert DEFAULT_MAX_BATCH_SIZE == 512
    assert DEFAULT_SCHEDULE_DELAY == 5.0
    assert DEFAULT_EXPORT_TIMEOUT == 30.0


def test_batch_stats_defaults_and_frozen() -> None:
    stats = BatchStats()
    assert (stats.exported, stats.dropped, stats.failed, stats.queue_len) == (0, 0, 0, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.exported = 1  # type: ignore[misc]


def test_exporter_property(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    try:
        assert bp.exporter is exporter
    finally:
        bp.shutdown()


def test_initial_stats(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    try:
        assert bp.stats() == BatchStats(0, 0, 0, 0)
    finally:
        bp.shutdown()


# -------------------------------------------------------------- the worker


def test_worker_starts_lazily_as_daemon(exporter: FakeExporter) -> None:
    before = set(threading.enumerate())
    bp = _processor(exporter)
    try:
        assert set(threading.enumerate()) - before == set()
        bp.on_end(spans(1)[0])
        started = set(threading.enumerate()) - before
        assert started, "the worker should start on the first span"
        assert all(t.daemon for t in started)
    finally:
        bp.shutdown()


def test_schedule_delay_flushes_partial_batch(exporter: FakeExporter) -> None:
    bp = _processor(exporter, schedule_delay=0.05)
    try:
        bp.on_end(spans(1)[0])
        exporter.wait_for(lambda: len(exporter.batches) == 1)
        assert exporter.batches == [["s0"]]
    finally:
        bp.shutdown()


def test_full_batch_exports_without_waiting_for_schedule(exporter: FakeExporter) -> None:
    bp = _processor(exporter, max_batch_size=4)
    try:
        for span in spans(4):
            bp.on_end(span)
        exporter.wait_for(lambda: len(exporter.batches) == 1)
        assert exporter.batches == [["s0", "s1", "s2", "s3"]]
    finally:
        bp.shutdown()


def test_partial_batch_waits_for_schedule(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    try:
        for span in spans(5):
            bp.on_end(span)
        assert bp.stats().queue_len == 5
        assert exporter.calls == 0
        assert bp.force_flush(timeout=WAIT) is True
        assert bp.stats().queue_len == 0
        assert exporter.exported == ["s0", "s1", "s2", "s3", "s4"]
    finally:
        bp.shutdown()


def test_batches_are_at_most_512_and_keep_order(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    try:
        items = spans(1100)
        for span in items:
            bp.on_end(span)
        assert bp.force_flush(timeout=WAIT) is True
    finally:
        bp.shutdown()
    sizes = [len(b) for b in exporter.batches]
    assert max(sizes) <= DEFAULT_MAX_BATCH_SIZE
    assert sum(sizes) == 1100
    assert len(sizes) >= 3
    assert exporter.exported == [cast("FakeSpan", s).name for s in items]


def test_custom_max_batch_size(exporter: FakeExporter) -> None:
    bp = _processor(exporter, max_batch_size=3)
    try:
        for span in spans(10):
            bp.on_end(span)
        assert bp.force_flush(timeout=WAIT)
    finally:
        bp.shutdown()
    assert all(len(b) <= 3 for b in exporter.batches)
    assert len(exporter.exported) == 10


def test_export_timeout_is_passed_to_exporter(exporter: FakeExporter) -> None:
    bp = _processor(exporter, export_timeout=7.5)
    try:
        bp.on_end(spans(1)[0])
        assert bp.force_flush(timeout=WAIT)
    finally:
        bp.shutdown()
    assert exporter.timeouts
    for timeout in exporter.timeouts:
        assert timeout is not None
        assert 0 < timeout <= 7.5


# --------------------------------------------------------------- flushing


def test_force_flush_with_nothing_queued(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    try:
        assert bp.force_flush(timeout=WAIT) is True
        assert exporter.calls == 0
    finally:
        bp.shutdown()


def test_force_flush_times_out_while_exporter_is_stuck(exporter: FakeExporter) -> None:
    exporter.gate.clear()
    bp = _processor(exporter)
    try:
        bp.on_end(spans(1)[0])
        started = time.monotonic()
        assert bp.force_flush(timeout=0.2) is False
        assert time.monotonic() - started < WAIT
        exporter.gate.set()
        assert bp.force_flush(timeout=WAIT) is True
        assert exporter.exported == ["s0"]
    finally:
        exporter.gate.set()
        bp.shutdown()


def test_force_flush_exports_spans_enqueued_during_export(exporter: FakeExporter) -> None:
    exporter.gate.clear()
    bp = _processor(exporter, max_batch_size=1)
    try:
        bp.on_end(spans(1, "a")[0])
        exporter.wait_for(lambda: exporter.calls == 1)  # first batch is in flight
        for span in spans(3, "b"):
            bp.on_end(span)
        exporter.gate.set()
        assert bp.force_flush(timeout=WAIT) is True
        assert exporter.exported == ["a0", "b0", "b1", "b2"]
    finally:
        exporter.gate.set()
        bp.shutdown()


# ---------------------------------------------------------- queue bound


def test_full_queue_drops_and_counts(exporter: FakeExporter) -> None:
    exporter.gate.clear()
    bp = _processor(exporter, max_queue_size=10, max_batch_size=2)
    try:
        for span in spans(100):
            bp.on_end(span)  # must never block
        stats = bp.stats()
        # At most one batch (2 spans) can be in flight besides the 10 queued.
        assert 88 <= stats.dropped <= 90
        assert stats.queue_len <= 10
        exporter.gate.set()
        assert bp.force_flush(timeout=WAIT)
        final = bp.stats()
    finally:
        exporter.gate.set()
        bp.shutdown()
    assert final.exported + final.dropped == 100
    assert final.exported == len(exporter.exported)
    assert final.queue_len == 0
    assert final.failed == 0


def test_dropped_spans_are_the_newest(exporter: FakeExporter) -> None:
    exporter.gate.clear()
    bp = _processor(exporter, max_queue_size=3, max_batch_size=512)
    try:
        for span in spans(10):
            bp.on_end(span)
        exporter.gate.set()
        assert bp.force_flush(timeout=WAIT)
    finally:
        exporter.gate.set()
        bp.shutdown()
    assert exporter.exported == ["s0", "s1", "s2"]
    assert bp.stats().dropped == 7


def test_concurrent_producers_with_full_queue_account_for_every_span(
    exporter: FakeExporter,
) -> None:
    exporter.gate.clear()
    bp = _processor(exporter, max_queue_size=50, max_batch_size=5)
    barrier = threading.Barrier(8)

    def produce(t: int) -> None:
        barrier.wait()
        for span in spans(100, f"t{t}."):
            bp.on_end(span)
            bp.stats()

    threads = [threading.Thread(target=produce, args=(t,)) for t in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert bp.stats().dropped >= 800 - 50 - 5
        exporter.gate.set()
        assert bp.force_flush(timeout=WAIT)
        stats = bp.stats()
    finally:
        exporter.gate.set()
        bp.shutdown()
    assert stats.exported + stats.dropped == 800
    assert stats.exported == len(exporter.exported)
    assert len(set(exporter.exported)) == len(exporter.exported)


def test_stats_under_concurrency(exporter: FakeExporter) -> None:
    bp = _processor(exporter, max_queue_size=100_000, max_batch_size=64)
    barrier = threading.Barrier(8)

    def produce(t: int) -> None:
        barrier.wait()
        for span in spans(500, f"t{t}."):
            bp.on_end(span)

    threads = [threading.Thread(target=produce, args=(t,)) for t in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert bp.force_flush(timeout=WAIT * 2)
        stats = bp.stats()
    finally:
        bp.shutdown()
    assert stats == BatchStats(exported=4000, dropped=0, failed=0, queue_len=0)
    assert sorted(exporter.exported) == sorted(f"t{t}.s{i}" for t in range(8) for i in range(500))


# -------------------------------------------------------------- failures


def test_exporter_exception_does_not_kill_worker(exporter: FakeExporter) -> None:
    exporter.fail_calls = {1}
    bp = _processor(exporter)
    try:
        for span in spans(3, "a"):
            bp.on_end(span)
        bp.force_flush(timeout=WAIT)
        exporter.wait_for(lambda: exporter.calls == 1)
        for span in spans(2, "b"):
            bp.on_end(span)
        assert bp.force_flush(timeout=WAIT) is True
        stats = bp.stats()
    finally:
        bp.shutdown()
    assert exporter.exported == ["b0", "b1"]
    assert stats.failed == 1
    assert stats.exported == 2
    assert stats.dropped == 0


def test_failed_count_is_per_export_call(exporter: FakeExporter) -> None:
    exporter.fail_calls = {1, 2, 3}
    bp = _processor(exporter, max_batch_size=2)
    try:
        for span in spans(6):
            bp.on_end(span)
        bp.force_flush(timeout=WAIT)
        exporter.wait_for(lambda: exporter.calls >= 3)
        bp.force_flush(timeout=WAIT)
        stats = bp.stats()
    finally:
        bp.shutdown()
    assert stats.failed == 3
    assert stats.exported == 0


# --------------------------------------------------------------- shutdown


def test_shutdown_drains_queue_and_shuts_exporter_down(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    for span in spans(10):
        bp.on_end(span)
    bp.shutdown(timeout=WAIT)
    assert exporter.exported == [f"s{i}" for i in range(10)]
    assert exporter.shutdowns == 1
    assert bp.stats().exported == 10
    assert bp.stats().queue_len == 0


def test_shutdown_is_idempotent(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    bp.on_end(spans(1)[0])
    bp.shutdown()
    bp.shutdown()
    bp.shutdown(timeout=0.1)
    assert exporter.shutdowns == 1
    assert exporter.exported == ["s0"]


def test_shutdown_without_spans(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    bp.shutdown()
    assert exporter.shutdowns == 1
    assert exporter.calls == 0


def test_shutdown_stops_worker_thread(exporter: FakeExporter) -> None:
    before = set(threading.enumerate())
    bp = _processor(exporter)
    bp.on_end(spans(1)[0])
    started = set(threading.enumerate()) - before
    bp.shutdown(timeout=WAIT)
    for thread in started:
        thread.join(WAIT)
        assert not thread.is_alive()


def test_spans_after_shutdown_are_dropped(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    bp.shutdown()
    for span in spans(3):
        bp.on_end(span)
    assert bp.stats().dropped == 3
    assert bp.force_flush(timeout=0.5) in (True, False)
    assert exporter.calls == 0


def test_shutdown_with_stuck_exporter_honours_timeout(exporter: FakeExporter) -> None:
    exporter.gate.clear()
    bp = _processor(exporter)
    bp.on_end(spans(1)[0])
    bp.force_flush(timeout=0.01)
    started = time.monotonic()
    try:
        bp.shutdown(timeout=0.2)
        assert time.monotonic() - started < WAIT
    finally:
        exporter.gate.set()


# ------------------------------------------------------------------ fork


def _in_child(scenario: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never return into pytest
        os.close(read_fd)
        code = 0
        try:
            payload: dict[str, Any] = {"ok": True, **scenario()}
        except BaseException:
            payload = {"ok": False, "error": traceback.format_exc()}
            code = 1
        try:
            os.write(write_fd, json.dumps(payload).encode())
        finally:
            os._exit(code)
    os.close(write_fd)
    chunks: list[bytes] = []
    deadline = time.monotonic() + 15
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("forked child did not finish in time")
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)
    result: dict[str, Any] = json.loads(b"".join(chunks) or b'{"ok": false}')
    assert result["ok"], result.get("error")
    return result


@pytest.mark.fork
@pytest.mark.skipif(
    not (hasattr(os, "fork") and sys.platform.startswith("linux")), reason="needs os.fork on Linux"
)
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_after_fork_in_child_resets_and_restarts(exporter: FakeExporter) -> None:
    bp = _processor(exporter)
    bp.on_end(spans(1, "parent")[0])  # starts the worker; stays queued

    def child() -> dict[str, Any]:
        bp._after_fork_in_child()
        before = bp.stats()
        bp.on_end(spans(1, "child")[0])
        flushed = bp.force_flush(timeout=WAIT)
        return {
            "queue_after_reset": before.queue_len,
            "flushed": flushed,
            "exported": exporter.exported,
        }

    try:
        result = _in_child(child)
        assert bp.force_flush(timeout=WAIT)
    finally:
        bp.shutdown()
    assert result["queue_after_reset"] == 0
    assert result["flushed"] is True
    assert result["exported"] == ["child0"]
    assert exporter.exported == ["parent0"]
