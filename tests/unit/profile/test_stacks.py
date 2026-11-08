"""Thread and asyncio task stacks (``pyrock.profile._stacks``)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from pyrock.profile._pprof import Profile
from pyrock.profile._stacks import format_text, thread_profile


async def _pending_task_for_unit_stacks(gate: asyncio.Future[None]) -> None:
    await gate


@dataclass
class LoopInThread:
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task[None]
    thread: threading.Thread


@pytest.fixture
def loop_with_task() -> Iterator[LoopInThread]:
    """A real event loop on its own thread with one task parked on a future."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="loop-for-unit-stacks", daemon=True)
    thread.start()

    async def setup() -> tuple[asyncio.Future[None], asyncio.Task[None]]:
        gate: asyncio.Future[None] = loop.create_future()
        task = asyncio.ensure_future(_pending_task_for_unit_stacks(gate))
        task.set_name("pending-for-unit-stacks")
        await asyncio.sleep(0)  # let the task run up to its await
        return gate, task

    gate, task = asyncio.run_coroutine_threadsafe(setup(), loop).result(5)
    try:
        yield LoopInThread(loop, task, thread)
    finally:
        loop.call_soon_threadsafe(gate.set_result, None)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def column(profile: Profile, type_: str) -> int:
    names = [t.type for t in profile.sample_types]
    assert type_ in names, names
    return names.index(type_)


def samples_with(profile: Profile, function: str) -> list[tuple[list[str], tuple[int, ...]]]:
    out = []
    for sample in profile.samples:
        names = [f.function for f in sample.stack]
        if any(function in n for n in names):
            out.append((names, sample.values))
    return out


# ---------------------------------------------------------------- threads


def test_profile_shape() -> None:
    profile = thread_profile()
    assert [(t.type, t.unit) for t in profile.sample_types] == [
        ("threads", "count"),
        ("tasks", "count"),
    ]
    assert all(len(s.values) == 2 for s in profile.samples)
    assert profile.start_time_ns > 0


def test_threads_appear_with_their_stacks(parked_thread: threading.Thread) -> None:
    profile = thread_profile()
    found = samples_with(profile, "_parked_worker_for_unit_profile")
    assert found
    threads = column(profile, "threads")
    assert sum(values[threads] for _, values in found) >= 1


def test_thread_stacks_are_leaf_first(parked_thread: threading.Thread) -> None:
    for names, _ in samples_with(thread_profile(), "_parked_worker_for_unit_profile"):
        target = next(i for i, n in enumerate(names) if "_parked_worker_for_unit_profile" in n)
        run = max(i for i, n in enumerate(names) if n.endswith("run"))
        assert 0 < target < run, names


def test_calling_thread_is_included() -> None:
    def _calling_frame_for_unit_stacks() -> Profile:
        return thread_profile()

    profile = _calling_frame_for_unit_stacks()
    assert samples_with(profile, "_calling_frame_for_unit_stacks")


def test_finished_threads_disappear() -> None:
    def _short_lived_for_unit_stacks() -> None:
        return None

    thread = threading.Thread(target=_short_lived_for_unit_stacks)
    thread.start()
    thread.join(5)
    assert not samples_with(thread_profile(), "_short_lived_for_unit_stacks")


# ---------------------------------------------------------------- asyncio tasks


def test_pending_tasks_on_tracked_loops_appear(loop_with_task: LoopInThread) -> None:
    profile = thread_profile(loops=[loop_with_task.loop])
    found = samples_with(profile, "_pending_task_for_unit_stacks")
    assert found
    tasks = column(profile, "tasks")
    assert sum(values[tasks] for _, values in found) >= 1


def test_untracked_loops_contribute_no_tasks(loop_with_task: LoopInThread) -> None:
    profile = thread_profile()
    assert not samples_with(profile, "_pending_task_for_unit_stacks")
    tasks = column(profile, "tasks")
    assert all(s.values[tasks] == 0 for s in profile.samples)


def test_loop_thread_itself_is_a_thread_sample(loop_with_task: LoopInThread) -> None:
    profile = thread_profile(loops=[loop_with_task.loop])
    threads = column(profile, "threads")
    assert any(
        "run_forever" in " ".join(names) and values[threads] >= 1
        for names, values in samples_with(profile, "run_forever")
    )


def test_closed_loop_is_ignored() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    profile = thread_profile(loops=[loop])
    assert profile.sample_types


# ---------------------------------------------------------------- text


def test_format_text_lists_threads_by_name(parked_thread: threading.Thread) -> None:
    text = format_text()
    assert "parked-for-unit-profile" in text
    assert "_parked_worker_for_unit_profile" in text
    assert "_helpers.py" in text


def test_format_text_lists_tasks(loop_with_task: LoopInThread) -> None:
    text = format_text(loops=[loop_with_task.loop])
    assert "pending-for-unit-stacks" in text
    assert "_pending_task_for_unit_stacks" in text


def test_format_text_without_loops_has_no_tasks(loop_with_task: LoopInThread) -> None:
    assert "_pending_task_for_unit_stacks" not in format_text()
