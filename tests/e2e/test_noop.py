"""Noop behaviour when no ``Bedrock`` is active (DESIGN.md §4.8 as amended by PLAN.md E1-1).

Every test runs in a brand-new, empty context so nothing another test
activated can leak in.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import tracemalloc
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

import pyrock
from pyrock.testing import capture
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
def test_live_noop_objects_are_singletons() -> None:
    # Scopes resolve the instance when entered (PLAN.md decision E1-1), so only
    # the live objects they yield are guaranteed to be preallocated singletons.
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


class _Boom(Exception):
    pass


def _raises() -> None:
    """Raises _Boom."""
    raise _Boom("from the function")


SCOPES = [
    pytest.param(lambda: pyrock.operation("op"), id="operation"),
    pytest.param(lambda: pyrock.step("step"), id="step"),
    pytest.param(lambda: pyrock.source("src"), id="source"),
]


@pytest.mark.parametrize("make_scope", SCOPES)
@pytest.mark.parametrize("fn", FUNCTIONS, ids=FUNCTION_IDS)
@isolated
def test_decorators_wrap_with_function_metadata(make_scope: Any, fn: Any) -> None:
    wrapped = make_scope()(fn)
    assert wrapped.__name__ == fn.__name__
    assert wrapped.__qualname__ == fn.__qualname__
    assert wrapped.__doc__ == fn.__doc__
    assert wrapped.__module__ == fn.__module__
    assert wrapped.__wrapped__ is fn
    assert inspect.signature(wrapped) == inspect.signature(fn)
    assert inspect.iscoroutinefunction(wrapped) == inspect.iscoroutinefunction(fn)
    assert inspect.isgeneratorfunction(wrapped) == inspect.isgeneratorfunction(fn)
    assert inspect.isasyncgenfunction(wrapped) == inspect.isasyncgenfunction(fn)


@pytest.mark.parametrize("make_scope", SCOPES)
@isolated
def test_decorated_function_without_init_returns_and_raises_like_the_original(
    make_scope: Any,
) -> None:
    assert make_scope()(_sync)(1) == 2
    with pytest.raises(_Boom, match="from the function"):
        make_scope()(_raises)()


@pytest.mark.parametrize("fn", FUNCTIONS, ids=FUNCTION_IDS)
@isolated
def test_explicit_noop_instance_returns_the_function_unchanged(fn: Any) -> None:
    noop = pyrock.current()
    assert noop.is_noop
    assert pyrock.operation("op", bedrock=noop)(fn) is fn
    assert pyrock.step("step", bedrock=noop)(fn) is fn
    assert pyrock.source("src", bedrock=noop)(fn) is fn


@isolated
def test_function_decorated_before_init_is_instrumented_once_active() -> None:
    @pyrock.operation("early.sync")
    def early(x: int) -> int:
        return x * 2

    @pyrock.operation("early.async")
    async def early_async() -> str:
        await asyncio.sleep(0)
        return "ok"

    @pyrock.step("early.step")
    def early_step() -> None:
        pass

    # Before init: the wrappers just run the functions.
    assert early(1) == 2
    assert run(early_async()) == "ok"
    with capture() as t:
        assert early(2) == 4
        assert run(early_async()) == "ok"
        with pyrock.operation("parent"):
            early_step()
    assert t.metrics.value("early_sync_count") == 1
    assert t.metrics.value("early_async_count") == 1
    assert t.spans.one(name="early.step").parent_span_id == t.spans.one(name="parent").span_id
    # After the capture ends the instance is gone again.
    assert early(3) == 6


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
