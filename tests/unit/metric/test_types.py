"""Counter, gauge and histogram behaviour, bound children and timers.

DESIGN §10.1; Go ``counter.go``, ``gauge.go``, ``histogram.go``.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import threading

import pytest

from pyrock.metric import (
    Bucket,
    CounterChild,
    GaugeChild,
    HistogramChild,
    Registry,
    Timer,
)
from tests.unit.metric._helpers import sample, samples, value

# ---------------------------------------------------------------- counters


def test_counter_inc_defaults_to_one_and_accepts_floats() -> None:
    reg = Registry()
    c = reg.counter("c_total")
    c.inc()
    c.inc(0.5)
    c.add(2.25)
    c.inc(0)
    assert value(reg, "c_total") == 3.75


def test_counter_large_increments_are_exact_integers() -> None:
    reg = Registry()
    c = reg.counter("big_total")
    c.inc(2**40)
    c.inc(1)
    assert value(reg, "big_total") == 2**40 + 1


@pytest.mark.parametrize("amount", [-1, -0.001, float("nan"), float("-inf")])
def test_counter_rejects_negative_or_nan(amount: float) -> None:
    reg = Registry()
    c = reg.counter("mono_total")
    c.inc(1)
    with pytest.raises(ValueError):  # noqa: PT011
        c.inc(amount)
    with pytest.raises(ValueError):  # noqa: PT011
        c.labels().add(amount)
    assert value(reg, "mono_total") == 1


def test_counter_unlabelled_inc_uses_missing_label_marker() -> None:
    reg = Registry()
    reg.counter("c_total", labels=["a", "b"]).inc()
    assert sample(reg, "c_total").labels == {"a": "_", "b": "_"}


def test_counter_labels_partial_fills_missing_with_marker() -> None:
    reg = Registry()
    reg.counter("c_total", labels=["a", "b"]).labels(a="x").inc()
    assert sample(reg, "c_total").labels == {"a": "x", "b": "_"}


def test_counter_unlabelled_without_declared_labels_has_empty_labels() -> None:
    reg = Registry()
    reg.counter("plain_total").inc()
    assert sample(reg, "plain_total").labels == {}


# ------------------------------------------------------------------ labels()


def test_labels_kwargs_and_mapping_bind_the_same_child() -> None:
    reg = Registry()
    c = reg.counter("req_total", labels=["http.method", "status"])
    by_mapping = c.labels({"http.method": "GET"}, status="200")
    by_kwargs = c.labels(http_method="GET", status="200")
    assert by_mapping is by_kwargs
    by_mapping.inc()
    by_kwargs.inc()
    assert value(reg, "req_total") == 2
    assert sample(reg, "req_total").labels == {"http_method": "GET", "status": "200"}


def test_labels_children_are_cached() -> None:
    reg = Registry()
    c = reg.counter("c_total", labels=["a"])
    g = reg.gauge("g", labels=["a"])
    h = reg.histogram("h_ms", labels=["a"])
    assert c.labels(a="1") is c.labels(a="1")
    assert g.labels(a="1") is g.labels({"a": "1"})
    assert h.labels(a="1") is h.labels(a="1")
    assert c.labels(a="1") is not c.labels(a="2")


def test_labels_return_child_types() -> None:
    reg = Registry()
    assert isinstance(reg.counter("c_total", labels=["a"]).labels(a="1"), CounterChild)
    assert isinstance(reg.gauge("g", labels=["a"]).labels(a="1"), GaugeChild)
    assert isinstance(reg.histogram("h_ms", labels=["a"]).labels(a="1"), HistogramChild)


def test_explicit_missing_values_share_the_unlabelled_series() -> None:
    reg = Registry()
    c = reg.counter("c_total", labels=["a"])
    c.inc()
    c.labels(a="_").inc()
    c.labels().inc()
    assert len(samples(reg, "c_total")) == 1
    assert value(reg, "c_total") == 3


def test_undeclared_label_raises_value_error_naming_the_key() -> None:
    reg = Registry()
    c = reg.counter("strict_total", labels=["a"])
    with pytest.raises(ValueError, match="nope"):
        c.labels(nope="x")
    with pytest.raises(ValueError, match="other"):
        reg.gauge("g", labels=["a"]).labels({"other.key": "x"})
    with pytest.raises(ValueError, match="zzz"):
        reg.histogram("h_ms", labels=["a"]).labels(a="1", zzz="2")


def test_label_values_are_converted_like_attributes() -> None:
    reg = Registry()
    c = reg.counter("typed_total", labels=["flag", "n", "ratio", "s"])
    c.labels(flag=True, n=3, ratio=1.5, s="x").inc()
    assert sample(reg, "typed_total").labels == {"flag": "true", "n": "3", "ratio": "1.5", "s": "x"}


def test_sequence_label_values_become_missing_marker() -> None:
    reg = Registry()
    reg.counter("seq_total", labels=["tags"]).labels(tags=["a", "b"]).inc()
    assert sample(reg, "seq_total").labels == {"tags": "_"}


def test_label_values_are_not_stringified_from_arbitrary_objects() -> None:
    calls: list[str] = []

    class Secret:
        def __str__(self) -> str:
            calls.append("str")
            return "hunter2"

        def __repr__(self) -> str:
            calls.append("repr")
            return "hunter2"

    reg = Registry()
    reg.counter("obj_total", labels=["v"]).labels(v=Secret()).inc()  # type: ignore[arg-type]
    assert sample(reg, "obj_total").labels == {"v": "_"}
    assert calls == []


# ------------------------------------------------------------------ gauges


def test_gauge_operations() -> None:
    reg = Registry()
    g = reg.gauge("depth")
    g.set(42)
    g.inc()
    g.dec()
    g.add(5)
    g.sub(2.5)
    assert value(reg, "depth") == 44.5
    g.dec(50)
    assert value(reg, "depth") == -5.5
    g.inc(0.25)
    assert value(reg, "depth") == -5.25
    g.set(0)
    assert value(reg, "depth") == 0


def test_gauge_children_are_independent() -> None:
    reg = Registry()
    g = reg.gauge("depth", labels=["q"])
    g.labels(q="a").set(1)
    g.labels(q="b").set(2)
    g.labels(q="a").add(10)
    assert value(reg, "depth", {"q": "a"}) == 11
    assert value(reg, "depth", {"q": "b"}) == 2


def test_gauge_accepts_non_finite_values() -> None:
    reg = Registry()
    g = reg.gauge("odd", labels=["k"])
    g.labels(k="inf").set(math.inf)
    g.labels(k="nan").set(math.nan)
    assert value(reg, "odd", {"k": "inf"}) == math.inf
    assert math.isnan(value(reg, "odd", {"k": "nan"}))


# --------------------------------------------------------------- histograms


def test_histogram_default_buckets_come_from_registry() -> None:
    assert Registry().histogram("h_ms").buckets == (0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000)


def test_histogram_custom_buckets_are_copied_as_floats() -> None:
    bounds = [1, 5, 10]
    h = Registry().histogram("h_ms", buckets=bounds)
    bounds.append(99)
    assert h.buckets == (1.0, 5.0, 10.0)


@pytest.mark.parametrize(
    "bad",
    [[2, 1], [1, 1], [1, float("nan")], [float("-inf"), 1], [1, float("inf")]],
)
def test_histogram_rejects_invalid_buckets(bad: list[float]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        Registry().histogram("bad_ms", buckets=bad)


def test_histogram_bucket_assignment_is_le_inclusive() -> None:
    reg = Registry()
    h = reg.histogram("h_ms", buckets=[1, 5, 10])
    for v in (-3, 0, 1, 1.0000001, 5, 9.99, 10, 10.0001, 1e9):
        h.observe(v)
    s = sample(reg, "h_ms")
    # Buckets are cumulative and exclude +Inf (that is ``count``).
    assert s.buckets == (Bucket(1.0, 3), Bucket(5.0, 5), Bucket(10.0, 7))
    assert s.count == 9
    assert s.sum == pytest.approx(-3 + 0 + 1 + 1.0000001 + 5 + 9.99 + 10 + 10.0001 + 1e9)


def test_histogram_values_above_every_bound_only_count() -> None:
    reg = Registry()
    h = reg.histogram("h_ms", buckets=[1])
    h.observe(2)
    h.observe(3)
    s = sample(reg, "h_ms")
    assert s.buckets == (Bucket(1.0, 0),)
    assert (s.count, s.sum) == (2, 5)


def test_histogram_child_with_labels() -> None:
    reg = Registry()
    h = reg.histogram("q_ms", buckets=[1, 2], labels=["db"])
    h.labels(db="pg").observe(1.5)
    h.labels(db="pg").observe(0.5)
    h.labels(db="my").observe(3)
    pg = sample(reg, "q_ms", {"db": "pg"})
    assert pg.buckets == (Bucket(1.0, 1), Bucket(2.0, 2))
    assert (pg.count, pg.sum) == (2, 2.0)
    my = sample(reg, "q_ms", {"db": "my"})
    assert (my.count, my.sum) == (1, 3.0)
    assert my.buckets == (Bucket(1.0, 0), Bucket(2.0, 0))


def test_histogram_samples_have_zero_value() -> None:
    reg = Registry()
    reg.histogram("h_ms", buckets=[1]).observe(1)
    assert sample(reg, "h_ms").value == 0


# ------------------------------------------------------------------- timers


def test_time_returns_timer() -> None:
    h = Registry().histogram("t_ms")
    assert isinstance(h.time(), Timer)
    assert isinstance(h.labels().time(), Timer)


def test_timer_context_manager_observes_milliseconds() -> None:
    reg = Registry()
    h = reg.histogram("t_ms", labels=["db"])
    with h.labels(db="pg").time() as timer:
        assert isinstance(timer, Timer)
        threading.Event().wait(0.03)
    s = sample(reg, "t_ms", {"db": "pg"})
    assert s.count == 1
    assert 25 <= s.sum < 5000


def test_timer_records_when_block_raises() -> None:
    reg = Registry()
    h = reg.histogram("t_ms")
    with pytest.raises(KeyError), h.time():
        raise KeyError("x")
    assert sample(reg, "t_ms").count == 1


def test_timer_decorator_on_sync_function() -> None:
    reg = Registry()
    h = reg.histogram("t_ms")

    @h.time()
    def double(x: int) -> int:
        """Doc."""
        return x * 2

    assert double(2) == 4
    assert double(3) == 6
    assert double.__name__ == "double"
    assert double.__doc__ == "Doc."
    s = sample(reg, "t_ms")
    assert s.count == 2
    assert 0 <= s.sum < 1000


def test_timer_decorator_records_when_function_raises() -> None:
    reg = Registry()
    h = reg.histogram("t_ms")

    @h.time()
    def boom() -> None:
        raise ValueError("x")

    with pytest.raises(ValueError, match="x"):
        boom()
    assert sample(reg, "t_ms").count == 1


def test_timer_decorator_on_async_function_times_the_await() -> None:
    reg = Registry()
    h = reg.histogram("t_ms")

    @h.time()
    async def work(x: int) -> int:
        await asyncio.sleep(0.03)
        return x + 1

    assert inspect.iscoroutinefunction(work)
    assert asyncio.run(work(1)) == 2
    s = sample(reg, "t_ms")
    assert s.count == 1
    assert 25 <= s.sum < 5000


def test_timer_decorator_is_reusable_across_calls_and_threads() -> None:
    reg = Registry()
    h = reg.histogram("t_ms")
    timed = h.time()(lambda: None)
    threads = [threading.Thread(target=timed) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sample(reg, "t_ms").count == 8
