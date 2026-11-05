"""``Span``, ``Tracer``, ID generation and the current-span context var (DESIGN §7.1)."""

from __future__ import annotations

import contextvars
import os
import threading
import time
from collections.abc import Sequence

import pytest

from pyrock.trace import (
    INVALID_SPAN_ID,
    INVALID_TRACE_ID,
    Span,
    SpanContext,
    SpanEvent,
    SpanKind,
    SpanProcessor,
    StatusCode,
    TraceState,
    Tracer,
    current_span,
    current_span_context,
    new_span_id,
    new_trace_id,
    use_span,
)
from pyrock.trace.sampler import (
    AlwaysOff,
    AlwaysOn,
    Decision,
    ParentBased,
    SamplingResult,
)

TID = bytes.fromhex("0af7651916cd43dd8448eb211c80319c")
SID = bytes.fromhex("b7ad6b7169203331")


class RecordingProcessor:
    def __init__(self) -> None:
        self.ended: list[Span] = []
        self.flushes = 0
        self.shutdowns = 0
        self._lock = threading.Lock()

    def on_end(self, span: Span, /) -> None:
        with self._lock:
            self.ended.append(span)

    def force_flush(self, *, timeout: float | None = None) -> bool:
        self.flushes += 1
        return True

    def shutdown(self, *, timeout: float | None = None) -> None:
        self.shutdowns += 1


class RecordingSampler:
    def __init__(self, decision: Decision = Decision.RECORD_AND_SAMPLE) -> None:
        self.decision = decision
        self.calls: list[tuple[bytes, str, bool | None]] = []

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        self.calls.append((trace_id, name, parent_sampled))
        return SamplingResult(self.decision)


def _tracer(
    sampler: object | None = None, processor: RecordingProcessor | None = None
) -> tuple[Tracer, RecordingProcessor]:
    proc = processor or RecordingProcessor()
    return Tracer(service="svc", sampler=sampler, processor=proc), proc  # type: ignore[arg-type]


def _span(**kwargs: object) -> Span:
    return Span("op", SpanContext(TID, SID, 1), **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------- IDs


def test_new_trace_id_shape() -> None:
    trace_id = new_trace_id()
    assert isinstance(trace_id, bytes)
    assert len(trace_id) == 16
    assert trace_id != INVALID_TRACE_ID


def test_new_span_id_shape() -> None:
    span_id = new_span_id()
    assert isinstance(span_id, bytes)
    assert len(span_id) == 8
    assert span_id != INVALID_SPAN_ID


def test_ids_are_unique() -> None:
    assert len({new_trace_id() for _ in range(2000)}) == 2000
    assert len({new_span_id() for _ in range(2000)}) == 2000


@pytest.mark.parametrize(("fn", "size"), [(new_trace_id, 16), (new_span_id, 8)])
def test_ids_never_zero_even_if_urandom_returns_zeros(
    monkeypatch: pytest.MonkeyPatch, fn: object, size: int
) -> None:
    real = os.urandom
    calls = {"n": 0}

    def fake(n: int) -> bytes:
        calls["n"] += 1
        return bytes(n) if calls["n"] <= 2 else real(n)

    monkeypatch.setattr(os, "urandom", fake)
    value = fn()  # type: ignore[operator]
    if calls["n"] == 0:
        pytest.skip("IDs are not drawn from os.urandom at call time")
    assert len(value) == size
    assert value != bytes(size)


# ------------------------------------------------------------------ Span


def test_span_basic_properties() -> None:
    parent = SpanContext(TID, b"\x01" * 8, 1)
    span = Span(
        "op",
        SpanContext(TID, SID, 1),
        parent=parent,
        kind=SpanKind.SERVER,
        attrs={"a": 1},
        start_time_ns=1_000,
    )
    assert span.name == "op"
    assert span.context == SpanContext(TID, SID, 1)
    assert span.parent == parent
    assert span.kind is SpanKind.SERVER
    assert span.start_time_ns == 1_000
    assert span.end_time_ns is None
    assert span.ended is False
    assert span.duration is None
    assert dict(span.attrs) == {"a": 1}
    assert list(span.events) == []
    assert span.status == (StatusCode.UNSET, "")
    assert span.is_recording is True


def test_span_defaults() -> None:
    before = time.time_ns()
    span = Span("op", SpanContext(TID, SID, 1))
    after = time.time_ns()
    assert span.parent is None
    assert span.kind is SpanKind.INTERNAL
    assert before <= span.start_time_ns <= after


def test_set_attr_and_overwrite() -> None:
    span = _span()
    span.set_attr("a", 1)
    span.set_attr("b", "x")
    span.set_attr("a", 2)
    assert dict(span.attrs) == {"a": 2, "b": "x"}


def test_set_attrs_merges() -> None:
    span = _span(attrs={"a": 1})
    span.set_attrs({"b": True, "a": 3.5})
    assert dict(span.attrs) == {"a": 3.5, "b": True}


def test_attrs_is_a_snapshot() -> None:
    span = _span()
    span.set_attr("a", 1)
    snapshot = span.attrs
    span.set_attr("b", 2)
    assert dict(snapshot) == {"a": 1}


def test_constructor_attrs_are_copied() -> None:
    attrs = {"a": 1}
    span = _span(attrs=attrs)
    attrs["b"] = 2
    assert dict(span.attrs) == {"a": 1}


def test_add_event_with_explicit_timestamp() -> None:
    span = _span()
    span.add_event("cache.miss", {"key": "k"}, timestamp_ns=1234)
    (event,) = span.events
    assert isinstance(event, SpanEvent)
    assert event.name == "cache.miss"
    assert event.timestamp_ns == 1234
    assert dict(event.attrs) == {"key": "k"}


def test_add_event_default_timestamp_and_order() -> None:
    span = _span()
    before = time.time_ns()
    span.add_event("first")
    span.add_event("second", {"n": 2})
    after = time.time_ns()
    names = [e.name for e in span.events]
    assert names == ["first", "second"]
    for event in span.events:
        assert before <= event.timestamp_ns <= after
    assert span.events[0].timestamp_ns <= span.events[1].timestamp_ns
    assert dict(span.events[0].attrs) == {}


def test_events_is_a_snapshot() -> None:
    span = _span()
    span.add_event("a")
    snapshot = span.events
    span.add_event("b")
    assert [e.name for e in snapshot] == ["a"]


def test_record_exception_adds_exception_event() -> None:
    span = _span()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        span.record_exception(exc)
    (event,) = span.events
    assert event.name == "exception"


def test_set_status() -> None:
    span = _span()
    span.set_status(StatusCode.ERROR, "boom")
    assert span.status == (StatusCode.ERROR, "boom")


def test_set_status_ok() -> None:
    span = _span()
    span.set_status(StatusCode.OK)
    assert span.status[0] is StatusCode.OK


def test_end_sets_time_and_duration() -> None:
    span = _span(start_time_ns=1_000_000_000)
    span.end(end_time_ns=2_500_000_000)
    assert span.ended is True
    assert span.end_time_ns == 2_500_000_000
    assert span.duration == pytest.approx(1.5)
    assert span.is_recording is False


def test_end_is_idempotent() -> None:
    tracer, proc = _tracer()
    span = tracer.start_span("op", start_time_ns=10)
    span.end(end_time_ns=20)
    span.end(end_time_ns=30)
    span.end()
    assert span.end_time_ns == 20
    assert proc.ended == [span]


def test_end_default_time_is_after_start() -> None:
    span = _span()
    span.end()
    assert span.end_time_ns is not None
    assert span.end_time_ns >= span.start_time_ns
    assert span.duration is not None
    assert span.duration >= 0


def test_end_time_is_monotonic_despite_wall_clock_going_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = Span("op", SpanContext(TID, SID, 1))
    start = span.start_time_ns
    monkeypatch.setattr(time, "time_ns", lambda: start - 10**12)
    monkeypatch.setattr(time, "time", lambda: (start - 10**12) / 1e9)
    span.end()
    assert span.end_time_ns is not None
    assert span.end_time_ns >= start
    assert span.duration is not None
    assert span.duration >= 0


def test_mutations_after_end_are_ignored() -> None:
    span = _span(attrs={"a": 1})
    span.end()
    span.set_attr("b", 2)
    span.set_attrs({"c": 3})
    span.add_event("late")
    span.set_status(StatusCode.ERROR, "late")
    assert dict(span.attrs) == {"a": 1}
    assert list(span.events) == []
    assert span.status == (StatusCode.UNSET, "")


def test_non_recording_span() -> None:
    span = Span("op", SpanContext(TID, SID, 0), recording=False)
    assert span.is_recording is False


def test_concurrent_attribute_sets_are_all_kept() -> None:
    span = _span()
    threads_n, per_thread = 16, 200
    barrier = threading.Barrier(threads_n)

    def work(t: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            span.set_attr(f"t{t}.k{i}", i)
            if i % 10 == 0:
                span.add_event(f"t{t}.e{i}")
            _ = span.attrs

    threads = [threading.Thread(target=work, args=(t,)) for t in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    attrs = span.attrs
    assert len(attrs) == threads_n * per_thread
    assert attrs["t3.k199"] == 199
    assert len(span.events) == threads_n * (per_thread // 10)


def test_concurrent_end_hands_off_once() -> None:
    tracer, proc = _tracer()
    span = tracer.start_span("op")
    barrier = threading.Barrier(8)

    def end() -> None:
        barrier.wait()
        span.end()

    threads = [threading.Thread(target=end) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert proc.ended == [span]


# ---------------------------------------------------------------- Tracer


def test_tracer_properties() -> None:
    proc = RecordingProcessor()
    tracer = Tracer(service="svc", resource={"env": "prod"}, processor=proc)
    assert tracer.service == "svc"
    assert dict(tracer.resource) == {"env": "prod"}
    assert tracer.processor is proc


def test_tracer_defaults() -> None:
    tracer = Tracer(service="svc")
    assert dict(tracer.resource) == {}
    assert tracer.processor is None
    span = tracer.start_span("op")
    assert span.context.sampled is True  # default ParentBased(AlwaysOn)
    span.end()


def test_root_span() -> None:
    tracer, _ = _tracer()
    span = tracer.start_span("root")
    assert span.name == "root"
    assert span.parent is None
    assert span.kind is SpanKind.INTERNAL
    assert span.context.is_valid
    assert span.context.is_remote is False
    assert span.context.sampled is True


def test_root_spans_start_new_traces() -> None:
    tracer, _ = _tracer()
    spans = [tracer.start_span("r") for _ in range(200)]
    assert len({s.context.trace_id for s in spans}) == 200
    assert len({s.context.span_id for s in spans}) == 200


def test_child_of_local_span() -> None:
    tracer, _ = _tracer()
    parent = tracer.start_span("parent")
    child = tracer.start_span("child", parent=parent)
    assert child.context.trace_id == parent.context.trace_id
    assert child.context.span_id != parent.context.span_id
    assert child.parent == parent.context
    assert child.context.is_remote is False


def test_child_of_span_context() -> None:
    tracer, _ = _tracer()
    remote = SpanContext(TID, SID, 1, TraceState([("a", "1")]), is_remote=True)
    child = tracer.start_span("child", parent=remote, kind=SpanKind.SERVER)
    assert child.context.trace_id == TID
    assert child.context.span_id != SID
    assert child.parent == remote
    assert child.kind is SpanKind.SERVER
    assert child.context.is_remote is False
    assert child.context.trace_state == remote.trace_state


def test_start_span_attrs_and_start_time() -> None:
    tracer, _ = _tracer()
    span = tracer.start_span("op", attrs={"k": "v"}, start_time_ns=42)
    assert dict(span.attrs) == {"k": "v"}
    assert span.start_time_ns == 42


def test_sampler_receives_trace_id_name_and_parent_flag() -> None:
    recording = RecordingSampler()
    tracer, _ = _tracer(recording)
    root = tracer.start_span("root")
    tracer.start_span("child", parent=root)
    tracer.start_span("remote", parent=SpanContext(TID, SID, 0, is_remote=True))
    assert recording.calls[0] == (root.context.trace_id, "root", None)
    assert recording.calls[1] == (root.context.trace_id, "child", True)
    assert recording.calls[2] == (TID, "remote", False)


def test_sampled_spans_are_handed_to_processor_on_end() -> None:
    tracer, proc = _tracer(AlwaysOn())
    span = tracer.start_span("op")
    assert proc.ended == []
    span.end()
    assert proc.ended == [span]


def test_unsampled_spans_are_created_but_not_handed_off() -> None:
    tracer, proc = _tracer(AlwaysOff())
    span = tracer.start_span("op")
    assert span.context.is_valid
    assert span.context.sampled is False
    assert span.is_recording is False
    span.end()
    assert proc.ended == []


@pytest.mark.parametrize(
    ("root", "parent", "expected"),
    [
        (AlwaysOn(), "remote-sampled", True),
        (AlwaysOff(), "remote-sampled", True),
        (AlwaysOn(), "remote-unsampled", False),
        (AlwaysOn(), "local-sampled", True),
        (AlwaysOn(), "local-unsampled", False),
        (AlwaysOn(), "none", True),
        (AlwaysOff(), "none", False),
    ],
)
def test_parent_based_through_tracer(root: object, parent: str, expected: bool) -> None:
    tracer, proc = _tracer(ParentBased(root))  # type: ignore[arg-type]
    parent_arg: Span | SpanContext | None
    if parent == "remote-sampled":
        parent_arg = SpanContext(TID, SID, 1, is_remote=True)
    elif parent == "remote-unsampled":
        parent_arg = SpanContext(TID, SID, 0, is_remote=True)
    elif parent == "local-sampled":
        parent_arg = Tracer(service="x", sampler=AlwaysOn()).start_span("p")
    elif parent == "local-unsampled":
        parent_arg = Tracer(service="x", sampler=AlwaysOff()).start_span("p")
    else:
        parent_arg = None
    span = tracer.start_span("child", parent=parent_arg)
    assert span.context.sampled is expected
    span.end()
    assert len(proc.ended) == (1 if expected else 0)


def test_children_of_unsampled_root_share_trace_and_stay_unsampled() -> None:
    tracer, proc = _tracer(ParentBased(AlwaysOff()))
    root = tracer.start_span("root")
    child = tracer.start_span("child", parent=root)
    assert child.context.trace_id == root.context.trace_id
    assert child.context.sampled is False
    child.end()
    root.end()
    assert proc.ended == []


def test_set_processor_replaces_processor() -> None:
    tracer, first = _tracer()
    second = RecordingProcessor()
    tracer.set_processor(second)
    assert tracer.processor is second
    tracer.start_span("op").end()
    assert first.ended == []
    assert len(second.ended) == 1
    tracer.set_processor(None)
    tracer.start_span("op").end()
    assert len(second.ended) == 1


def test_tracer_shutdown_shuts_processor_down() -> None:
    tracer, proc = _tracer()
    tracer.shutdown()
    assert proc.shutdowns == 1


def test_processor_protocols() -> None:
    assert isinstance(RecordingProcessor(), SpanProcessor)


def test_concurrent_span_creation_has_unique_ids() -> None:
    tracer, proc = _tracer()
    barrier = threading.Barrier(8)
    created: list[list[Span]] = [[] for _ in range(8)]

    def work(i: int) -> None:
        barrier.wait()
        for _ in range(250):
            span = tracer.start_span("op")
            created[i].append(span)
            span.end()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    spans: Sequence[Span] = [s for chunk in created for s in chunk]
    assert len({s.context.span_id for s in spans}) == 2000
    assert len(proc.ended) == 2000


# ---------------------------------------------------------- current span


def test_current_span_defaults_to_none() -> None:
    def run() -> tuple[object, object]:
        return current_span(), current_span_context()

    assert contextvars.copy_context().run(run) == (None, None)


def test_use_span_sets_and_token_resets() -> None:
    def run() -> None:
        span = _span()
        token = use_span(span)
        assert current_span() is span
        assert current_span_context() == span.context
        inner = Span("inner", SpanContext(TID, b"\x02" * 8, 1))
        inner_token = use_span(inner)
        assert current_span() is inner
        token.var.reset(inner_token)
        assert current_span() is span
        token.var.reset(token)
        assert current_span() is None

    contextvars.copy_context().run(run)


def test_use_span_none_clears() -> None:
    def run() -> None:
        use_span(_span())
        use_span(None)
        assert current_span() is None
        assert current_span_context() is None

    contextvars.copy_context().run(run)
