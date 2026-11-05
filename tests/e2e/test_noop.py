"""Noop behaviour when no ``Bedrock`` is active (DESIGN.md §4.8; Go: ``noop.go``).

Every test runs in a brand-new, empty context so nothing another test
activated can leak in.
"""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

import pyrock
from tests.e2e._core_helpers import isolated, run


def _sync(x: int) -> int:
    return x + 1


async def _coro(x: int) -> int:
    return x + 1


def _gen(n: int) -> Iterator[int]:
    yield from range(n)


async def _agen(n: int) -> AsyncIterator[int]:
    for i in range(n):
        yield i


FUNCTIONS = [_sync, _coro, _gen, _agen]
FUNCTION_IDS = ["sync", "coroutine", "generator", "async-generator"]


@isolated
def test_current_is_the_noop_singleton() -> None:
    first = pyrock.current()
    assert first.is_noop
    assert pyrock.current() is first
    assert pyrock.current_operation() is None
    assert pyrock.current_span() is None
    assert pyrock.current_span_context() is None


@isolated
def test_scopes_and_live_objects_are_singletons() -> None:
    assert pyrock.operation("a") is pyrock.operation("b", attrs={"k": "v"})
    assert pyrock.step("a") is pyrock.step("b")
    assert pyrock.source("a") is pyrock.source("b")
    with pyrock.operation("a") as first:
        pass
    with pyrock.operation("b") as second:
        pass
    assert first is second
    assert pyrock.start_operation("a") is pyrock.start_operation("b")
    with pyrock.step("a") as step_one, pyrock.step("b") as step_two:
        assert step_one is step_two
    with pyrock.source("a") as src_one, pyrock.source("b") as src_two:
        assert src_one is src_two


@isolated
def test_metric_helpers_return_singletons() -> None:
    assert pyrock.counter("a") is pyrock.counter("b", "help", labels=["x"])
    assert pyrock.gauge("a") is pyrock.gauge("b")
    assert pyrock.histogram("a") is pyrock.histogram("b", buckets=[1, 2])
    counter = pyrock.counter("a", labels=["x"])
    assert counter.labels(x="1") is counter
    gauge = pyrock.gauge("a", labels=["x"])
    assert gauge.labels({"x.y": "1"}) is gauge
    histogram = pyrock.histogram("a", labels=["x"])
    assert histogram.labels(x="1") is histogram


@pytest.mark.parametrize("fn", FUNCTIONS, ids=FUNCTION_IDS)
@isolated
def test_decorators_return_the_function_unchanged(fn: object) -> None:
    assert pyrock.operation("op")(fn) is fn  # type: ignore[type-var]
    assert pyrock.step("step")(fn) is fn  # type: ignore[type-var]
    assert pyrock.source("src")(fn) is fn  # type: ignore[type-var]


@isolated
def test_decorated_functions_still_work() -> None:
    assert pyrock.operation("op")(_sync)(1) == 2
    assert run(pyrock.step("step")(_coro)(1)) == 2
    assert list(pyrock.source("src")(_gen)(3)) == [0, 1, 2]


@isolated
def test_noop_operation_methods_do_nothing_and_never_raise() -> None:
    with pyrock.operation("op", metric_labels=["a"], attrs={"a": "1"}) as op:
        op.register(status="ok")
        op.register({"dotted.key": 1}, pyrock.Event("evt", n=1), ValueError("x"))
        op.event("evt", {"k": "v"}, extra=1)
        op.fail(ValueError("x"))
        op.fail()
        assert op.failed is False
        op.succeed()
        op.end(error=ValueError("x"))
        op.end(success=False)
        op.done()
        assert op.trace_id is None
        assert op.span_id is None
        assert op.span_context is None
        token = op.activate()
        op.deactivate(token)
    pyrock.register(anything=1)


@isolated
def test_noop_step_and_source_methods_never_raise() -> None:
    with pyrock.step("s", attrs={"k": 1}) as step:
        step.register(n=1)
        step.event("e")
        step.done()
        assert step.trace_id is None
    with pyrock.source("src", attrs={"k": "v"}, metric_labels=["k"]) as src:
        src.sum("n")
        src.sum("n", 5)
        src.gauge("g", 1.5)
        src.histogram("h", 2.5)
        src.done()


@isolated
def test_noop_metrics_accept_every_update() -> None:
    counter = pyrock.counter("c", labels=["x"])
    counter.inc()
    counter.add(2)
    counter.labels(x="1").inc(3)
    gauge = pyrock.gauge("g")
    gauge.set(1)
    gauge.inc()
    gauge.dec()
    gauge.add(5)
    gauge.sub(2)
    histogram = pyrock.histogram("h")
    histogram.observe(1.0)
    with histogram.time():
        pass

    @histogram.time()
    def timed() -> int:
        return 7

    assert timed() == 7


@isolated
def test_noop_log_helpers_never_raise() -> None:
    pyrock.debug("d", k=1)
    pyrock.info("i", {"a.b": 1})
    pyrock.warning("w")
    pyrock.warn("w")
    pyrock.error("e", ValueError("x"))
    pyrock.log("info", "l")
    pyrock.log(20, "l")


@isolated
def test_exceptions_pass_through_noop_scopes_untouched() -> None:
    original = KeyError("k")
    with pytest.raises(KeyError) as excinfo, pyrock.operation("op"), pyrock.step("s"):
        raise original
    assert excinfo.value is original


@isolated
def test_async_noop_scopes() -> None:
    async def main() -> str:
        async with pyrock.operation("op") as op, pyrock.step("s"), pyrock.source("src"):
            await asyncio.sleep(0)
            return "done" if op.trace_id is None else "traced"

    assert run(main()) == "done"


@isolated
def test_inject_headers_adds_nothing_without_a_trace() -> None:
    headers: dict[str, str] = {"accept": "*/*"}
    assert pyrock.inject_headers(headers) is headers
    assert headers == {"accept": "*/*"}


@isolated
def test_noop_steady_state_does_not_allocate() -> None:
    def hot_path() -> None:
        with pyrock.operation("noop.hot") as op:
            op.register(status="ok")
            with pyrock.step("noop.step"):
                pass
        pyrock.counter("noop_counter").inc()

    for _ in range(200):  # warm caches and any lazily created singletons
        hot_path()
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        for _ in range(5_000):
            hot_path()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    # Only allocations made by pyrock's own source files count (not this test).
    pyrock_only = [tracemalloc.Filter(True, str(Path(pyrock.__file__).parent / "*"))]
    stats = after.filter_traces(pyrock_only).compare_to(
        before.filter_traces(pyrock_only), "filename"
    )
    retained = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
    # Nothing per call is kept: 5 000 iterations retaining even 1 byte each would show.
    assert retained < 1024, [str(stat) for stat in stats[:5]]
