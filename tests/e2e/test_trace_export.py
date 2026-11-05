"""OTLP trace export end to end: what a collector receives over HTTP/JSON and gRPC.

Covers DESIGN §5 (attribute type mapping), §7 (export, batching, samplers) and
the batch-processor statistics.
"""

from __future__ import annotations

import base64
import datetime
import re
import time
from typing import Any

import pytest

from tests.e2e._export_helpers import export_one, prom, raw_attr, running, start
from tests.support.collectors import GRPCCollector, OTLPHTTPCollector
from tests.support.otlp_decode import flatten_spans

pytestmark = pytest.mark.usefixtures("clean_env")

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")

# Trace IDs whose low 8 bytes are tiny / huge: any deterministic ratio rule
# (big- or little-endian) samples the first and drops the second at ratio 0.5.
LOW_TRACE_ID = "0123456789abcdef" + "0000000000000001"
HIGH_TRACE_ID = "0123456789abcdef" + "ffffffffffffffff"
REMOTE_SPAN_ID = "00f067aa0ba902b7"


def _remote(trace_id: str, *, sampled: bool) -> Any:
    from pyrock import SpanContext

    return SpanContext.from_hex(trace_id, REMOTE_SPAN_ID, sampled=sampled, is_remote=True)


# --------------------------------------------------------------- HTTP/JSON wire


def test_http_export_uses_json_content_type(otlp_collector: OTLPHTTPCollector) -> None:
    export_one(otlp_collector)
    req = otlp_collector.trace_requests()[0]
    assert req.method == "POST"
    assert (req.header("content-type") or "").startswith("application/json")


def test_resource_carries_service_name(otlp_collector: OTLPHTTPCollector) -> None:
    span = export_one(otlp_collector, service="users-api")
    assert span["resource"]["service.name"] == "users-api"


def test_scope_name_is_set(otlp_collector: OTLPHTTPCollector) -> None:
    span = export_one(otlp_collector)
    assert span["scope_name"]


def test_span_ids_are_lowercase_hex(otlp_collector: OTLPHTTPCollector) -> None:
    span = export_one(otlp_collector)
    assert HEX32.match(span["trace_id"])
    assert HEX16.match(span["span_id"])
    assert span["parent_span_id"] is None


def test_child_span_links_to_parent(otlp_collector: OTLPHTTPCollector) -> None:
    with (
        running(trace_url=otlp_collector.traces_url) as b,
        b.operation("parent"),
        b.operation("child"),
    ):
        pass
    parent = otlp_collector.span("parent")
    child = otlp_collector.span("child")
    assert child["trace_id"] == parent["trace_id"]
    assert child["parent_span_id"] == parent["span_id"]


def test_step_is_child_of_operation(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url) as b, b.operation("op"), b.step("helper"):
        pass
    assert otlp_collector.span("helper")["parent_span_id"] == otlp_collector.span("op")["span_id"]


def test_span_timestamps_are_ordered(otlp_collector: OTLPHTTPCollector) -> None:
    before = time.time_ns()
    span = export_one(otlp_collector)
    after = time.time_ns()
    assert before <= span["start_time_unix_nano"] <= span["end_time_unix_nano"] <= after


def test_default_kind_is_internal(otlp_collector: OTLPHTTPCollector) -> None:
    assert export_one(otlp_collector)["kind"] == 1


def test_kind_is_exported(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock import SpanKind

    with (
        running(trace_url=otlp_collector.traces_url) as b,
        b.operation("consume", kind=SpanKind.CONSUMER),
    ):
        pass
    assert otlp_collector.span("consume")["kind"] == 5


def test_successful_operation_has_ok_status(otlp_collector: OTLPHTTPCollector) -> None:
    assert export_one(otlp_collector)["status_code"] == 1


def test_failed_operation_has_error_status(otlp_collector: OTLPHTTPCollector) -> None:
    with (
        pytest.raises(ValueError, match="boom"),
        running(trace_url=otlp_collector.traces_url) as b,
        b.operation("broken"),
    ):
        raise ValueError("boom")
    span = otlp_collector.span("broken")
    assert span["status_code"] == 2
    assert span["attributes"]["error.type"] == "builtins.ValueError"


def test_remote_parent_continues_trace(otlp_collector: OTLPHTTPCollector) -> None:
    with (
        running(trace_url=otlp_collector.traces_url) as b,
        b.operation("handle", remote_parent=_remote(LOW_TRACE_ID, sampled=True)),
    ):
        pass
    span = otlp_collector.span("handle")
    assert span["trace_id"] == LOW_TRACE_ID
    assert span["parent_span_id"] == REMOTE_SPAN_ID


def test_event_is_exported_with_attributes(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url) as b, b.operation("op") as op:
        op.event("cache_miss", key="u:123")
    events = otlp_collector.span("op")["events"]
    assert [e["name"] for e in events] == ["cache_miss"]
    assert events[0]["attributes"] == {"key": "u:123"}
    assert events[0]["time_unix_nano"] > 0


# ------------------------------------------------------- attribute type mapping


def _register_and_get(collector: OTLPHTTPCollector, key: str, value: Any) -> dict[str, Any]:
    with running(trace_url=collector.traces_url) as b, b.operation("typed") as op:
        op.register({key: value})
    return raw_attr(collector, "typed", key)


def test_str_attr_is_string_value(otlp_collector: OTLPHTTPCollector) -> None:
    assert _register_and_get(otlp_collector, "s", "hello") == {"stringValue": "hello"}


def test_bool_attr_is_bool_value_not_int(otlp_collector: OTLPHTTPCollector) -> None:
    assert _register_and_get(otlp_collector, "flag", True) == {"boolValue": True}


def test_int_attr_is_int_value(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "count", 42)
    assert set(value) == {"intValue"}
    assert int(value["intValue"]) == 42


def test_negative_int64_edge_is_int_value(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "min", -(2**63))
    assert int(value["intValue"]) == -(2**63)


def test_out_of_range_int_becomes_string(otlp_collector: OTLPHTTPCollector) -> None:
    assert _register_and_get(otlp_collector, "big", 2**70) == {"stringValue": str(2**70)}


def test_float_attr_is_double_value(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "ratio", 1.5)
    assert set(value) == {"doubleValue"}
    assert float(value["doubleValue"]) == 1.5


def test_timedelta_attr_is_double_milliseconds(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "timeout", datetime.timedelta(seconds=5))
    assert set(value) == {"doubleValue"}
    assert float(value["doubleValue"]) == 5000.0


def test_datetime_attr_is_rfc3339_string(otlp_collector: OTLPHTTPCollector) -> None:
    moment = datetime.datetime(2026, 11, 5, 12, 0, 1, 250000, tzinfo=datetime.UTC)
    value = _register_and_get(otlp_collector, "at", moment)
    assert set(value) == {"stringValue"}
    text = value["stringValue"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$", text)
    assert datetime.datetime.fromisoformat(text.replace("Z", "+00:00")) == moment


def test_bytes_attr_is_base64_bytes_value(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "blob", b"\x00\x01\xffpy")
    assert set(value) == {"bytesValue"}
    assert base64.b64decode(value["bytesValue"]) == b"\x00\x01\xffpy"


def test_str_sequence_is_array_value(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "tags", ["a", "b"])
    assert value == {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}}


def test_int_sequence_is_array_of_int_values(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "ids", (1, 2, 3))
    items = value["arrayValue"]["values"]
    assert [int(v["intValue"]) for v in items] == [1, 2, 3]


def test_bool_sequence_is_array_of_bool_values(otlp_collector: OTLPHTTPCollector) -> None:
    value = _register_and_get(otlp_collector, "bits", [True, False])
    assert value["arrayValue"]["values"] == [{"boolValue": True}, {"boolValue": False}]


def test_static_attrs_are_resource_attributes(otlp_collector: OTLPHTTPCollector) -> None:
    span = export_one(otlp_collector, static_attrs={"env": "production"})
    assert span["resource"]["env"] == "production"


# --------------------------------------------------------- protocol selection


def test_url_with_scheme_selects_http(otlp_collector: OTLPHTTPCollector) -> None:
    export_one(otlp_collector)
    assert len(otlp_collector.trace_requests()) == 1


@pytest.mark.compat
def test_bare_host_port_selects_grpc(grpc_collector: GRPCCollector) -> None:
    with (
        running(trace_url=grpc_collector.endpoint, trace_insecure=True, service="grpc-svc") as b,
        b.operation("over_grpc"),
    ):
        pass
    spans = grpc_collector.wait_for_spans(1)
    assert [s["name"] for s in spans] == ["over_grpc"]
    assert spans[0]["resource"]["service.name"] == "grpc-svc"


@pytest.mark.compat
def test_grpc_export_preserves_parent_links(grpc_collector: GRPCCollector) -> None:
    with (
        running(trace_url=grpc_collector.endpoint, trace_insecure=True) as b,
        b.operation("parent"),
        b.operation("child"),
    ):
        pass
    spans = {s["name"]: s for s in grpc_collector.wait_for_spans(2)}
    assert spans["child"]["parent_span_id"] == spans["parent"]["span_id"]
    assert spans["child"]["trace_id"] == spans["parent"]["trace_id"]
    assert HEX32.match(spans["parent"]["trace_id"])


@pytest.mark.compat
def test_grpc_export_typed_attributes(grpc_collector: GRPCCollector) -> None:
    with (
        running(trace_url=grpc_collector.endpoint, trace_insecure=True) as b,
        b.operation("typed") as op,
    ):
        op.register({"flag": True, "count": 7, "ratio": 0.25, "name": "x", "blob": b"\x01"})
    attrs = grpc_collector.wait_for_spans(1)[0]["attributes"]
    assert attrs["flag"] is True
    assert attrs["count"] == 7
    assert attrs["ratio"] == 0.25
    assert attrs["name"] == "x"
    assert attrs["blob"] == b"\x01"


@pytest.mark.compat
def test_explicit_grpc_protocol_with_insecure_h2c(grpc_collector: GRPCCollector) -> None:
    with (
        running(trace_url=grpc_collector.endpoint, trace_protocol="grpc", trace_insecure=True) as b,
        b.operation("forced"),
    ):
        pass
    assert grpc_collector.wait_for_calls(1) >= 1


@pytest.mark.compat
def test_grpc_error_status_counts_failure_without_crash(grpc_collector: GRPCCollector) -> None:
    grpc_collector.fail_next(1, code="INVALID_ARGUMENT")
    with (
        running(trace_url=grpc_collector.endpoint, trace_insecure=True) as bedrock,
        bedrock.operation("rejected"),
    ):
        pass
    assert bedrock.trace_export_stats().failed >= 1


# ----------------------------------------------------------------- batching


def test_shutdown_flushes_before_schedule_delay(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url) as b, b.operation("quick"):
        pass
    # Shutdown returned well before the 5 s schedule delay, and the span is there.
    assert otlp_collector.span_names() == ["quick"]


def test_many_spans_are_split_into_batches(otlp_collector: OTLPHTTPCollector) -> None:
    total = 1200
    with running(trace_url=otlp_collector.traces_url) as b:
        for i in range(total):
            with b.operation(f"bulk_{i % 3}"):
                pass
    requests = otlp_collector.trace_requests()
    per_request = [len(flatten_spans(r.json())) for r in requests]
    assert sum(per_request) == total
    assert len(requests) >= 3
    assert max(per_request) <= 512


def test_export_stats_count_exported_spans(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url) as bedrock:
        for _ in range(5):
            with bedrock.operation("counted"):
                pass
    stats = bedrock.trace_export_stats()
    assert stats.exported == 5
    assert stats.dropped == 0
    assert stats.failed == 0
    assert stats.queue_len == 0


def test_full_queue_drops_and_counts(otlp_collector: OTLPHTTPCollector) -> None:
    total = 5000
    otlp_collector.hang()  # the first export blocks, so the bounded queue fills up
    bedrock = start(trace_url=otlp_collector.traces_url)
    try:
        for _ in range(total):
            with bedrock.operation("flood"):
                pass
        dropped_while_blocked = bedrock.trace_export_stats().dropped
    finally:
        otlp_collector.release()
        bedrock.shutdown(timeout=10)
    assert dropped_while_blocked > 0
    stats = bedrock.trace_export_stats()
    assert stats.exported + stats.dropped == total
    assert len(otlp_collector.spans()) == stats.exported


def test_collector_5xx_counts_failure_without_raising(otlp_collector: OTLPHTTPCollector) -> None:
    otlp_collector.set_default_status(503)
    with running(trace_url=otlp_collector.traces_url) as bedrock, bedrock.operation("unlucky"):
        pass
    stats = bedrock.trace_export_stats()
    assert stats.failed >= 1
    assert stats.exported == 0
    assert otlp_collector.trace_requests()


def test_operations_keep_working_while_collector_fails(otlp_collector: OTLPHTTPCollector) -> None:
    otlp_collector.set_default_status(500)
    with running(trace_url=otlp_collector.traces_url) as b:
        for _ in range(3):
            with b.operation("resilient") as op:
                op.register(ok=True)
        assert b.trace_export_stats().exported == 0


def test_no_trace_url_exports_nothing(otlp_collector: OTLPHTTPCollector) -> None:
    with running() as b, b.operation("local_only"):
        pass
    assert otlp_collector.requests == []


# ------------------------------------------------------------------ samplers


def test_sample_rate_zero_exports_nothing(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url, trace_sample_rate=0.0) as b:
        for _ in range(10):
            with b.operation("unsampled"):
                pass
    assert otlp_collector.spans() == []


def test_sample_rate_zero_still_records_metrics(otlp_collector: OTLPHTTPCollector) -> None:
    with running(trace_url=otlp_collector.traces_url, trace_sample_rate=0.0) as b:
        with b.operation("unsampled"):
            pass
        assert prom(b).value("unsampled_count") == 1


def test_always_off_sampler_exports_nothing(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with (
        running(trace_url=otlp_collector.traces_url, trace_sampler=sampler.AlwaysOff()) as b,
        b.operation("dropped"),
    ):
        pass
    assert otlp_collector.spans() == []


def test_ratio_sampler_samples_low_trace_ids(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with running(trace_url=otlp_collector.traces_url, trace_sampler=sampler.Ratio(0.5)) as b:
        for _ in range(5):
            with b.operation("low", remote_parent=_remote(LOW_TRACE_ID, sampled=True)):
                pass
    assert otlp_collector.span_names() == ["low"] * 5


def test_ratio_sampler_drops_high_trace_ids(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with running(trace_url=otlp_collector.traces_url, trace_sampler=sampler.Ratio(0.5)) as b:
        for _ in range(5):
            with b.operation("high", remote_parent=_remote(HIGH_TRACE_ID, sampled=True)):
                pass
    assert otlp_collector.spans() == []


def test_ratio_one_samples_every_root(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with running(trace_url=otlp_collector.traces_url, trace_sampler=sampler.Ratio(1.0)) as b:
        for _ in range(20):
            with b.operation("all"):
                pass
    assert len(otlp_collector.spans()) == 20


def test_parent_based_follows_sampled_remote_parent(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with (
        running(
            trace_url=otlp_collector.traces_url,
            trace_sampler=sampler.ParentBased(sampler.AlwaysOff()),
        ) as b,
        b.operation("continued", remote_parent=_remote(HIGH_TRACE_ID, sampled=True)),
    ):
        pass
    assert otlp_collector.span_names() == ["continued"]


def test_parent_based_follows_unsampled_remote_parent(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with (
        running(
            trace_url=otlp_collector.traces_url,
            trace_sampler=sampler.ParentBased(sampler.AlwaysOn()),
        ) as b,
        b.operation("not_continued", remote_parent=_remote(LOW_TRACE_ID, sampled=False)),
    ):
        pass
    assert otlp_collector.spans() == []


def test_parent_based_uses_root_sampler_for_roots(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with (
        running(
            trace_url=otlp_collector.traces_url,
            trace_sampler=sampler.ParentBased(sampler.AlwaysOff()),
        ) as b,
        b.operation("root"),
    ):
        pass
    assert otlp_collector.spans() == []


def test_children_follow_sampled_root(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.trace import sampler

    with (
        running(
            trace_url=otlp_collector.traces_url,
            trace_sampler=sampler.ParentBased(sampler.AlwaysOn()),
        ) as b,
        b.operation("root"),
        b.operation("child"),
        b.step("leaf"),
    ):
        pass
    assert sorted(otlp_collector.span_names()) == ["child", "leaf", "root"]
