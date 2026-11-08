"""``Span._finish`` equals ``set_attrs`` + ``set_status`` + ``end``; trusted span
contexts and one-draw root IDs behave like the validated ones (FINDINGS
P1-11 c, d, e)."""

from __future__ import annotations

import os
import threading
from typing import Any

import pytest

from pyrock.privacy import Policy
from pyrock.trace import (
    Span,
    SpanContext,
    StatusCode,
    Tracer,
)
from pyrock.trace.sampler import AlwaysOff, AlwaysOn, Decision, SamplingResult


class _Processor:
    def __init__(self) -> None:
        self.ended: list[Span] = []
        self._lock = threading.Lock()

    def on_end(self, span: Span, /) -> None:
        with self._lock:
            self.ended.append(span)

    def force_flush(self, *, timeout: float | None = None) -> bool:
        return True

    def shutdown(self, *, timeout: float | None = None) -> None:
        pass


class _RecordOnly:
    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        return SamplingResult(Decision.RECORD_ONLY)


def _pair(sampler: Any = None, policy: Policy | None = None) -> tuple[Span, Span, _Processor]:
    processor = _Processor()
    tracer = Tracer(service="s", sampler=sampler, processor=processor, policy=policy)
    attrs: dict[str, Any] = {"a": 1, "password": "x"}
    return (
        tracer.start_span("one", attrs=attrs),
        tracer.start_span("two", attrs=attrs),
        processor,
    )


_ATTRS: dict[str, Any] = {
    "a": 2,
    "b": "text",
    "password": "hunter2",
    "error.message": "secret detail",
    "http.url": "https://user:pw@h/p?q=1",
    "nested": {"token": "t", "ok": [1, 2]},
    "obj": object(),
}


@pytest.mark.parametrize("sampler", [None, AlwaysOn(), AlwaysOff(), _RecordOnly()])
@pytest.mark.parametrize(
    ("code", "message"),
    [(StatusCode.UNSET, ""), (StatusCode.OK, ""), (StatusCode.ERROR, "x" * 2000)],
)
def test_finish_equals_the_three_calls(sampler: Any, code: StatusCode, message: str) -> None:
    policy = Policy(max_string_length=16)
    one, two, processor = _pair(sampler, policy)
    one.set_status(StatusCode.ERROR, "earlier")
    two.set_status(StatusCode.ERROR, "earlier")

    one.set_attrs(_ATTRS)
    one.set_status(code, message)
    one.end()
    two._finish(_ATTRS, code, message)

    assert dict(two.attrs) == dict(one.attrs)
    assert list(two.attrs) == list(one.attrs)
    assert two.status == one.status
    assert two.ended
    assert one.ended
    assert two.is_recording is one.is_recording is False
    assert two.end_time_ns is not None
    assert two.end_time_ns >= two.start_time_ns
    assert [s.name for s in processor.ended] == (["one", "two"] if one.context.sampled else [])


def test_finish_is_idempotent_and_ignored_after_end() -> None:
    _, span, processor = _pair()
    span.end()
    end = span.end_time_ns
    span._finish({"late": 1}, StatusCode.ERROR, "late")
    assert "late" not in span.attrs
    assert span.status == (StatusCode.UNSET, "")
    assert span.end_time_ns == end
    assert len(processor.ended) == 1

    _, other, processor = _pair()
    other._finish({}, StatusCode.OK)
    other._finish({"x": 1}, StatusCode.ERROR, "again")
    other.end()
    assert other.status == (StatusCode.OK, "")
    assert len(processor.ended) == 1


def test_finish_without_a_tracer_copies_attrs_unfiltered() -> None:
    span = Span("bare", SpanContext(b"\x01" * 16, b"\x02" * 8, 1))
    span._finish({"password": "kept"}, StatusCode.ERROR, "m")
    assert dict(span.attrs) == {"password": "kept"}
    assert span.status == (StatusCode.ERROR, "m")


def test_finish_races_with_end_hand_off_once() -> None:
    for _ in range(200):
        _, span, processor = _pair()
        barrier = threading.Barrier(2)

        def finish(span: Span = span, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            span._finish({"k": 1}, StatusCode.OK)

        def end(span: Span = span, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            span.end()

        threads = [threading.Thread(target=finish), threading.Thread(target=end)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(processor.ended) == 1


# ------------------------------------------------------------ span contexts


def test_tracer_contexts_equal_validated_ones() -> None:
    tracer = Tracer(service="s")
    root = tracer.start_span("root")
    child = tracer.start_span("child", parent=root)
    for span in (root, child):
        ctx = span.context
        rebuilt = SpanContext(ctx.trace_id, ctx.span_id, ctx.trace_flags, ctx.trace_state)
        assert ctx == rebuilt
        assert hash(ctx) == hash(rebuilt)
        assert ctx.trace_id_hex == ctx.trace_id.hex()
        assert len(ctx.trace_id_hex) == 32
        assert ctx.span_id_hex == ctx.span_id.hex()
        assert len(ctx.span_id_hex) == 16
        assert ctx.is_valid
        assert not ctx.is_remote
        assert len(ctx.trace_id) == 16
        assert len(ctx.span_id) == 8
        assert repr(ctx) == repr(rebuilt)
    assert child.context.trace_id == root.context.trace_id
    assert child.context.span_id != root.context.span_id


def test_root_ids_come_from_one_draw_and_are_never_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.urandom
    sizes: list[int] = []

    def fake(n: int) -> bytes:
        sizes.append(n)
        return bytes(n) if len(sizes) == 1 else real(n)

    monkeypatch.setattr(os, "urandom", fake)
    span = Tracer(service="s").start_span("root")
    assert sizes[0] == 24
    assert span.context.trace_id != bytes(16)
    assert span.context.span_id != bytes(8)

    sizes.clear()

    def record(n: int) -> bytes:
        sizes.append(n)
        return real(n)

    monkeypatch.setattr(os, "urandom", record)
    Tracer(service="s").start_span("root")
    assert sizes == [24]
