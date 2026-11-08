"""Context propagation: asyncio tasks, threads, executors and the explicit helpers.

DESIGN.md §2.1, §4.6, §4.7; PLAN.md decision 2 (no monkey-patching of threads).
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
from typing import Any

import pytest

import pyrock
from pyrock.testing import capture
from tests.e2e._core_helpers import isolated, run

THREADS_INHERIT_CONTEXT = bool(getattr(sys.flags, "thread_inherit_context", 0))
"""3.14 free-threaded builds start threads with a copy of the caller's context."""


def _observe() -> dict[str, Any]:
    """What a piece of code sees of pyrock's context where it runs."""
    op = pyrock.current_operation()
    return {
        "noop": pyrock.current().is_noop,
        "operation": None if op is None else op.name,
        "trace_id": None if op is None else op.trace_id,
    }


def _child_operation(name: str) -> None:
    with pyrock.operation(name):
        pass


# -- current() / current_operation() / current_span() -------------------------------


@isolated
def test_current_is_noop_outside_and_the_instance_inside_a_capture() -> None:
    assert pyrock.current().is_noop
    with capture() as t:
        assert pyrock.current() is t.bedrock
        assert not pyrock.current().is_noop
    assert pyrock.current().is_noop


def test_current_operation_is_the_innermost_operation() -> None:
    with capture():
        assert pyrock.current_operation() is None
        with pyrock.operation("outer") as outer:
            assert pyrock.current_operation() is outer
            with pyrock.operation("inner") as inner:
                assert pyrock.current_operation() is inner
                with pyrock.step("helper"):
                    # Steps are not operations.
                    assert pyrock.current_operation() is inner
            assert pyrock.current_operation() is outer
        assert pyrock.current_operation() is None


def test_current_span_is_the_innermost_live_span() -> None:
    with capture():
        assert pyrock.current_span() is None
        with pyrock.operation("outer") as outer:
            span = pyrock.current_span()
            assert span is not None
            assert span.context.span_id_hex == outer.span_id
            with pyrock.step("helper") as step:
                step_span = pyrock.current_span()
                assert step_span is not None
                assert step_span.context.span_id_hex == step.span_id
            with pyrock.operation("hot", no_trace=True):
                hot_span = pyrock.current_span()
                assert hot_span is not None
                assert hot_span.context.span_id_hex == outer.span_id
        assert pyrock.current_span() is None


def test_current_span_context_includes_a_remote_parent() -> None:
    remote = pyrock.SpanContext.from_hex(
        "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", is_remote=True
    )
    with capture(), pyrock.operation("consume", remote_parent=remote) as op:
        context = pyrock.current_span_context()
        assert context is not None
        assert context.trace_id_hex == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert context.span_id_hex == op.span_id


# -- asyncio ------------------------------------------------------------------


def test_asyncio_tasks_inherit_the_current_operation() -> None:
    async def main() -> None:
        async with pyrock.operation("parent"):
            await asyncio.create_task(_child_async("child"))

    with capture() as t:
        run(main())
    assert t.spans.one(name="child").parent_span_id == t.spans.one(name="parent").span_id


async def _child_async(name: str) -> None:
    async with pyrock.operation(name):
        await asyncio.sleep(0)


def test_concurrent_tasks_are_isolated_from_each_other() -> None:
    seen: dict[str, str | None] = {}

    async def worker(name: str, entered: asyncio.Event, other: asyncio.Event) -> None:
        async with pyrock.operation(name):
            entered.set()
            await other.wait()  # both operations are open at the same time here
            current = pyrock.current_operation()
            seen[name] = None if current is None else current.name

    async def main() -> None:
        a_in, b_in = asyncio.Event(), asyncio.Event()
        async with pyrock.operation("parent"):
            await asyncio.gather(worker("a", a_in, b_in), worker("b", b_in, a_in))
            current = pyrock.current_operation()
            seen["parent"] = None if current is None else current.name

    with capture() as t:
        run(main())
    assert seen == {"a": "a", "b": "b", "parent": "parent"}
    parent = t.spans.one(name="parent")
    assert t.spans.one(name="a").parent_span_id == parent.span_id
    assert t.spans.one(name="b").parent_span_id == parent.span_id


def test_task_group_siblings_do_not_leak_attrs_or_operations() -> None:
    async def first(ready: asyncio.Event) -> None:
        async with pyrock.operation("first"):
            pyrock.register(only_first=True)
            ready.set()
            await asyncio.sleep(0)

    async def second(ready: asyncio.Event) -> None:
        await ready.wait()
        assert pyrock.current_operation() is not None
        assert pyrock.current_operation().name == "group"  # type: ignore[union-attr]
        async with pyrock.operation("second"):
            pyrock.register(only_second=True)

    async def main() -> None:
        ready = asyncio.Event()
        async with pyrock.operation("group"), asyncio.TaskGroup() as tg:
            tg.create_task(first(ready))
            tg.create_task(second(ready))

    with capture() as t:
        run(main())
    group = t.spans.one(name="group")
    first_span = t.spans.one(name="first")
    second_span = t.spans.one(name="second")
    assert first_span.parent_span_id == group.span_id
    assert second_span.parent_span_id == group.span_id
    assert "only_second" not in first_span.attrs
    assert "only_first" not in second_span.attrs
    assert "only_first" not in group.attrs


def test_sibling_tasks_under_one_parent_are_numbered() -> None:
    async def main() -> None:
        async with pyrock.operation("parent"):
            await asyncio.gather(*(_child_async("fetch") for _ in range(3)))

    with capture() as t:
        run(main())
    assert {"fetch", "fetch[1]", "fetch[2]"} <= set(t.spans.names())
    assert t.metrics.value("fetch_count") == 3


def test_asyncio_to_thread_carries_context() -> None:
    async def main() -> dict[str, Any]:
        async with pyrock.operation("parent"):
            return await asyncio.to_thread(_observe)

    with capture() as t:
        seen = run(main())
    assert seen["operation"] == "parent"
    assert seen["trace_id"] == t.spans.one(name="parent").trace_id


# -- threads ------------------------------------------------------------------


@pytest.mark.skipif(THREADS_INHERIT_CONTEXT, reason="this build's threads inherit context")
def test_plain_thread_does_not_inherit_context() -> None:
    seen: dict[str, Any] = {}

    def body() -> None:
        seen.update(_observe())
        _child_operation("orphan")

    with capture() as t, pyrock.operation("parent"):
        thread = threading.Thread(target=body)
        thread.start()
        thread.join(5)
    assert seen == {"noop": True, "operation": None, "trace_id": None}
    assert t.spans.find(name="orphan") == []
    assert t.metrics.value("orphan_count") is None


def test_bind_carries_context_into_a_plain_thread() -> None:
    seen: dict[str, Any] = {}

    def body() -> None:
        seen.update(_observe())
        _child_operation("bound_child")

    with capture() as t, pyrock.operation("parent"):
        thread = threading.Thread(target=pyrock.bind(body))
        thread.start()
        thread.join(5)
    parent = t.spans.one(name="parent")
    assert seen == {"noop": False, "operation": "parent", "trace_id": parent.trace_id}
    assert t.spans.one(name="bound_child").parent_span_id == parent.span_id


def test_bind_prebinds_arguments_like_partial() -> None:
    def add(a: int, b: int, *, scale: int = 1) -> int:
        return (a + b) * scale

    with capture():
        bound = pyrock.bind(add, 1, 2, scale=10)
        assert bound() == 30
        assert pyrock.bind(add)(2, 3) == 5


def test_bind_runs_each_call_in_a_fresh_copy_of_the_captured_context() -> None:
    leaked: list[str | None] = []

    def body() -> None:
        current = pyrock.current_operation()
        leaked.append(None if current is None else current.name)
        # Activate without deactivating: must not be visible to the next call.
        pyrock.start_operation("dangling").activate()

    with capture(), pyrock.operation("parent"):
        bound = pyrock.bind(body)
        bound()
        bound()
    assert leaked == ["parent", "parent"]


def test_bind_captures_context_at_bind_time() -> None:
    with capture():
        with pyrock.operation("at_bind"):
            bound = pyrock.bind(_observe)
        with pyrock.operation("at_call"):
            seen = bound()
    assert seen["operation"] == "at_bind"


def test_context_thread_pool_executor_copies_context_on_submit_and_map() -> None:
    with (
        capture() as t,
        pyrock.operation("parent"),
        pyrock.ContextThreadPoolExecutor(max_workers=2) as pool,
    ):
        submitted = pool.submit(_observe).result(5)
        mapped = list(pool.map(_child_operation, ["mapped"] * 3))
    parent = t.spans.one(name="parent")
    assert submitted["operation"] == "parent"
    assert submitted["trace_id"] == parent.trace_id
    assert len(mapped) == 3
    children = [span for span in t.spans if span.name.startswith("mapped")]
    assert sorted(child.name for child in children) == ["mapped", "mapped[1]", "mapped[2]"]
    assert all(child.parent_span_id == parent.span_id for child in children)


def test_pyrock_thread_runs_target_in_creators_context() -> None:
    seen: dict[str, Any] = {}

    def body(label: str) -> None:
        seen.update(_observe(), label=label)

    with capture(), pyrock.operation("parent"):
        thread = pyrock.Thread(target=body, args=("x",))
        assert isinstance(thread, threading.Thread)
        thread.start()
        thread.join(5)
    assert seen["operation"] == "parent"
    assert seen["label"] == "x"


def test_pyrock_thread_accepts_an_explicit_context() -> None:
    seen: dict[str, Any] = {}
    with capture():
        with pyrock.operation("captured"):
            context = contextvars.copy_context()
        thread = pyrock.Thread(target=lambda: seen.update(_observe()), context=context)
        thread.start()
        thread.join(5)
    assert seen["operation"] == "captured"


def test_concurrent_thread_operations_are_all_counted() -> None:
    workers, per_worker = 8, 50
    barrier = threading.Barrier(workers)

    def body() -> None:
        barrier.wait()
        for _ in range(per_worker):
            with pyrock.operation("parallel", metric_labels=["kind"], attrs={"kind": "t"}):
                pass

    with capture() as t:
        threads = [pyrock.Thread(target=body) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    assert t.metrics.value("parallel_count", kind="t") == workers * per_worker
    assert t.metrics.value("parallel_successes", kind="t") == workers * per_worker
    # Root operations are never numbered, whichever thread opens them.
    assert set(t.spans.names()) == {"parallel"}
