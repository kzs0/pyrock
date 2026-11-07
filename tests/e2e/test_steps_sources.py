"""Steps (lightweight spans) and sources (long-lived prefixes with aggregates).

DESIGN.md §4.3, §4.4; Go: ``Step``/``StepFromContext``, ``Source``, ``Sum``/``Gauge``/``Histogram``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

import pyrock
from pyrock.testing import capture
from tests.e2e._core_helpers import isolated, prom_text, quiet, run
from tests.support.collectors import OTLPHTTPCollector

# -- steps --------------------------------------------------------------------


def test_step_is_a_child_span_of_the_operation() -> None:
    with (
        capture() as t,
        pyrock.operation("handle") as op,
        pyrock.step("parse_payload") as step,
    ):
        assert step.trace_id == op.trace_id
        assert step.span_id != op.span_id
    step_span = t.spans.one(name="parse_payload")
    op_span = t.spans.one(name="handle")
    assert step_span.parent_span_id == op_span.span_id
    assert step_span.trace_id == op_span.trace_id


def test_step_attrs_propagate_to_the_parent_operation() -> None:
    with (
        capture() as t,
        pyrock.operation("handle"),
        pyrock.step("validate", attrs={"rules": 12}) as step,
    ):
        step.register(bytes=10)
        pyrock.register({"payload.kind": "json"})
    for name in ("validate", "handle"):
        attrs = t.spans.one(name=name).attrs
        assert attrs["rules"] == 12, name
        assert attrs["bytes"] == 10, name
        assert attrs["payload.kind"] == "json", name


def test_step_attrs_can_fill_parent_metric_labels() -> None:
    with (
        capture() as t,
        pyrock.operation("lookup", metric_labels=["cache"]),
        pyrock.step("cache_probe") as step,
    ):
        step.register(cache="hit")
    assert t.metrics.value("lookup_count", cache="hit") == 1


def test_step_creates_no_metrics() -> None:
    with capture() as t, pyrock.operation("handle"), pyrock.step("parse_payload"):
        pass
    exposition = prom_text(t)
    assert exposition.value("handle_count") == 1
    assert not any(name.startswith("parse_payload") for name in exposition.names())


def test_step_event_is_recorded_on_the_step_span() -> None:
    with capture() as t, pyrock.operation("handle"), pyrock.step("validate") as step:
        step.event("rule_failed", rule="email")
    events = t.spans.one(name="validate").events
    assert [(event.name, dict(event.attrs)) for event in events] == [
        ("rule_failed", {"rule": "email"})
    ]


def test_exception_escaping_step_marks_step_span_and_propagates() -> None:
    with (
        capture() as t,
        pyrock.operation("handle"),
        pytest.raises(ValueError, match="bad"),
        pyrock.step("parse_payload"),
    ):
        raise ValueError("bad payload")
    step_span = t.spans.one(name="parse_payload")
    assert step_span.failed
    assert "exception" in [event.name for event in step_span.events]
    # The operation caught it, so the operation itself succeeded.
    assert not t.spans.one(name="handle").failed
    assert t.metrics.value("handle_successes") == 1


def test_exception_escaping_step_and_operation_fails_the_operation() -> None:
    with (
        capture() as t,
        pytest.raises(KeyError),
        pyrock.operation("handle"),
        pyrock.step("parse_payload"),
    ):
        raise KeyError("missing")
    assert t.spans.one(name="parse_payload").failed
    handle = t.spans.one(name="handle")
    assert handle.failed
    assert handle.attrs["error.type"] == "builtins.KeyError"


def test_expected_exception_does_not_mark_step_span() -> None:
    with (
        capture() as t,
        pyrock.operation("handle"),
        contextlib.suppress(KeyError),
        pyrock.step("probe", expected=(KeyError,)),
    ):
        raise KeyError("miss")
    assert not t.spans.one(name="probe").failed


def test_duplicate_steps_are_numbered() -> None:
    with capture() as t, pyrock.operation("handle"):
        for _ in range(2):
            with pyrock.step("validate"):
                pass
    assert {"validate", "validate[1]"} <= set(t.spans.names())


def test_step_no_trace_creates_no_span_but_keeps_parent_span() -> None:
    with (
        capture() as t,
        pyrock.operation("handle"),
        pyrock.step("hot_helper", no_trace=True) as step,
    ):
        step.register(n=1)
    assert t.spans.find(name="hot_helper") == []
    assert t.spans.names() == ["handle"]


def test_step_without_operation_or_trace_is_a_harmless_noop() -> None:
    with capture() as t:
        with pyrock.step("lonely") as step:
            step.register(n=1)
            step.event("tick")
        step.done()
    assert t.spans.find(name="lonely") == []


def test_step_done_is_idempotent() -> None:
    with capture() as t, pyrock.operation("handle"), pyrock.step("helper") as step:
        step.done()
        step.done()
    assert len(t.spans.find(name="helper")) == 1


def test_decorated_sync_and_async_steps() -> None:
    with capture() as t:

        @pyrock.step("parse")
        def parse(raw: bytes) -> int:
            pyrock.register(size=len(raw))
            return len(raw)

        @pyrock.step("fetch")
        async def fetch() -> str:
            await asyncio.sleep(0)
            return "ok"

        async def handler() -> tuple[int, str]:
            async with pyrock.operation("handle"):
                return parse(b"abcd"), await fetch()

        assert run(handler()) == (4, "ok")
    handle = t.spans.one(name="handle")
    assert t.spans.one(name="parse").parent_span_id == handle.span_id
    assert t.spans.one(name="fetch").parent_span_id == handle.span_id
    assert handle.attrs["size"] == 4


@isolated
def test_steps_reach_otlp_collector_under_their_operation(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    with (
        pyrock.init(**quiet(trace_url=otlp_collector.traces_url)),
        pyrock.operation("handle"),
        pyrock.step("helper", attrs={"rows": 7}),
    ):
        pass
    otlp_collector.wait_for_spans(2)
    helper = otlp_collector.span("helper")
    handle = otlp_collector.span("handle")
    assert helper["parent_span_id"] == handle["span_id"]
    assert helper["attributes"]["rows"] == 7
    assert handle["attributes"]["rows"] == 7


# -- sources ------------------------------------------------------------------


def test_source_prefixes_child_operation_names() -> None:
    with (
        capture() as t,
        pyrock.source("background.worker"),
        pyrock.operation("process") as op,
    ):
        assert op.name == "background.worker.process"
    assert t.spans.names() == ["background.worker.process"]
    assert t.metrics.value("background_worker_process_count") == 1


def test_source_shares_attrs_and_labels_with_children() -> None:
    with (
        capture() as t,
        pyrock.source(
            "background.worker", attrs={"worker.type": "async"}, metric_labels=["worker.type"]
        ),
        pyrock.operation("process", metric_labels=["status"]) as op,
    ):
        op.register(status="ok")
    span = t.spans.one(name="background.worker.process")
    assert span.attrs["worker.type"] == "async"
    labels = prom_text(t).label_sets("background_worker_process_count")
    assert labels == [{"worker_type": "async", "status": "ok"}]


def test_source_aggregates_use_prefixed_names_and_source_labels() -> None:
    with (
        capture(static_attrs={"env": "test"}) as t,
        pyrock.source(
            "background.worker", attrs={"worker.type": "async"}, metric_labels=["worker.type"]
        ) as src,
    ):
        src.sum("jobs_processed", 1)
        src.sum("jobs_processed", 2)
        src.gauge("queue_depth", 42)
        src.histogram("latency_ms", 52.3)
    exposition = prom_text(t)
    labels = {"worker_type": "async", "env": "test"}
    assert exposition.type("background_worker_jobs_processed") == "counter"
    assert exposition.value("background_worker_jobs_processed", labels) == 3
    assert exposition.type("background_worker_queue_depth") == "gauge"
    assert exposition.value("background_worker_queue_depth", labels) == 42
    assert exposition.type("background_worker_latency_ms") == "histogram"
    assert exposition.value("background_worker_latency_ms_count", labels) == 1
    assert exposition.value("background_worker_latency_ms_sum", labels) == pytest.approx(52.3)
    # pyrock.testing filters accept a positional mapping for dotted keys.
    assert t.metrics.value("background_worker_jobs_processed", {"worker.type": "async"}) == 3


def test_direct_children_of_a_source_are_never_numbered() -> None:
    with capture() as t, pyrock.source("consumer"):
        for _ in range(3):
            with pyrock.operation("handle"):
                pass
    assert t.spans.names() == ["consumer.handle"] * 3
    assert t.metrics.value("consumer_handle_count") == 3


def test_source_sum_defaults_to_one() -> None:
    with capture() as t, pyrock.source("consumer") as src:
        src.sum("messages")
        src.sum("messages")
    assert t.metrics.value("consumer_messages") == 2


def test_source_creates_no_span_of_its_own() -> None:
    with capture() as t, pyrock.source("consumer"):
        pass
    assert len(t.spans) == 0


def test_source_accessors() -> None:
    with (
        capture(),
        pyrock.source("consumer", attrs={"queue": "jobs"}, metric_labels=["queue"]) as src,
    ):
        assert src.name == "consumer"
        assert dict(src.attrs) == {"queue": "jobs"}
        assert src.metric_labels == ("queue",)
        src.done()


def test_leaving_source_restores_unprefixed_names() -> None:
    with capture() as t:
        with pyrock.source("consumer"):
            pass
        with pyrock.operation("after"):
            pass
    assert t.spans.names() == ["after"]


def test_source_decorator_on_sync_function() -> None:
    with capture() as t:

        @pyrock.source("consumer")
        def run_consumer() -> None:
            with pyrock.operation("handle"):
                pass

        run_consumer()
    assert t.spans.names() == ["consumer.handle"]


def test_source_decorator_on_coroutine_function() -> None:
    with capture() as t:

        @pyrock.source("consumer", attrs={"queue": "jobs"})
        async def run_consumer() -> None:
            for _ in range(2):
                async with pyrock.operation("handle"):
                    await asyncio.sleep(0)

        run(run_consumer())
    spans = t.spans.find(name="consumer.handle")
    assert len(spans) == 2
    assert all(span.attrs["queue"] == "jobs" for span in spans)
    assert t.metrics.value("consumer_handle_count") == 2
