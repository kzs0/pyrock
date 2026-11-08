"""Thread safety: exact totals under contention (meaningful on free-threaded builds too).

DESIGN §10.1 and §15: one lock per series; correct without the GIL.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator

import pytest

from pyrock.metric import Registry
from tests.unit.metric._helpers import sample, samples, value

THREADS = 16
PER_THREAD = 2000


@pytest.fixture(autouse=True)
def _fast_switching() -> Iterator[None]:
    """Force frequent thread switches on GIL builds to surface races."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def _run(worker: Callable[[int], None], n: int = THREADS) -> None:
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def target(i: int) -> None:
        try:
            barrier.wait()
            worker(i)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_counter_same_series_exact_total() -> None:
    reg = Registry()
    c = reg.counter("hot_total", labels=["k"])

    def worker(_: int) -> None:
        for _ in range(PER_THREAD):
            c.labels(k="same").inc()

    _run(worker)
    assert value(reg, "hot_total") == THREADS * PER_THREAD


def test_counter_unlabelled_same_series_exact_total() -> None:
    reg = Registry()
    c = reg.counter("hot_total")
    child = c.labels()

    def worker(i: int) -> None:
        for _ in range(PER_THREAD):
            (c if i % 2 else child).inc(0.5)

    _run(worker)
    assert value(reg, "hot_total") == THREADS * PER_THREAD * 0.5


def test_counter_distinct_series_exact_totals() -> None:
    reg = Registry()
    c = reg.counter("spread_total", labels=["worker"])

    def worker(i: int) -> None:
        for _ in range(PER_THREAD):
            c.labels(worker=str(i)).inc()

    _run(worker)
    got = {s.labels["worker"]: s.value for s in samples(reg, "spread_total")}
    assert got == {str(i): PER_THREAD for i in range(THREADS)}


def test_gauge_same_series_inc_dec_balances() -> None:
    reg = Registry()
    g = reg.gauge("level")

    def worker(i: int) -> None:
        for _ in range(PER_THREAD):
            if i % 2:
                g.inc()
            else:
                g.add(2)
                g.sub(1)
                g.dec(2)

    _run(worker)
    odd = even = THREADS // 2
    assert value(reg, "level") == odd * PER_THREAD * 1 + even * PER_THREAD * (2 - 1 - 2)


def test_histogram_same_series_exact_count_sum_and_buckets() -> None:
    reg = Registry()
    h = reg.histogram("lat_ms", buckets=[1, 2])

    def worker(i: int) -> None:
        for _ in range(PER_THREAD):
            h.observe(1 if i % 2 else 3)

    _run(worker)
    s = sample(reg, "lat_ms")
    total = THREADS * PER_THREAD
    assert s.count == total
    assert s.sum == (total // 2) * 1 + (total // 2) * 3
    assert [b.count for b in s.buckets] == [total // 2, total // 2]


def test_concurrent_labels_creation_returns_one_child() -> None:
    reg = Registry()
    c = reg.counter("race_total", labels=["k"])
    seen: list[object] = []

    def worker(_: int) -> None:
        child = c.labels(k="x")
        child.inc()
        seen.append(child)

    _run(worker, 32)
    assert len({id(child) for child in seen}) == 1
    assert value(reg, "race_total") == 32


def test_concurrent_registration_returns_one_metric() -> None:
    reg = Registry()
    seen: list[object] = []

    def worker(_: int) -> None:
        seen.append(reg.counter("shared_total", "S", labels=["a"]))

    _run(worker, 32)
    assert len({id(m) for m in seen}) == 1


def test_series_limit_holds_under_contention() -> None:
    reg = Registry(series_limit=10)
    c = reg.counter("capped_total", labels=["k"])

    def worker(i: int) -> None:
        for j in range(50):
            c.labels(k=f"{i}-{j}").inc()

    _run(worker)
    assert len(samples(reg, "capped_total")) == 10
    assert reg.dropped_series() == THREADS * 50 - 10


def test_collect_while_writing_is_consistent() -> None:
    reg = Registry()
    h = reg.histogram("busy_ms", buckets=[1])
    c = reg.counter("busy_total", labels=["k"])
    snapshots: list[tuple[int, list[int]]] = []

    def worker(i: int) -> None:
        if i == 0:
            for _ in range(200):
                for fam in reg.collect():
                    if fam.name == "busy_ms":
                        for s in fam.samples:
                            snapshots.append((s.count, [b.count for b in s.buckets]))
            return
        for j in range(PER_THREAD // 4):
            h.observe(0.5)
            c.labels(k=str(j % 20)).inc()

    _run(worker, 8)
    for count, buckets in snapshots:
        assert all(b <= count for b in buckets)
    assert sample(reg, "busy_ms").count == 7 * (PER_THREAD // 4)
