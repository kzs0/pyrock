"""OTLP/HTTP JSON exporter (``pyrock.trace.otlp.http``).

Payload expectations follow the OTLP JSON mapping as Go's ``encoder.go``
emits it: lowercase hex IDs, enums as integers, 64-bit integers (timestamps,
``intValue``) as decimal strings. Optional fields that carry only their default
(``traceState: ""``, ``droppedAttributesCount: 0``, ...) may be present or not.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import socket
from typing import Any

import pytest

import pyrock
from pyrock.trace import Span, SpanContext, SpanKind, StatusCode, TraceState
from pyrock.trace.otlp import ExportError, PermanentExportError
from pyrock.trace.otlp.http import HTTPExporter, encode_json, to_otlp_json
from tests.support.collectors import OTLPHTTPCollector

TID = bytes(range(1, 17))
SID = bytes(range(1, 9))
PID = bytes(range(0xA0, 0xA8))

OPTIONAL_SPAN_DEFAULTS: dict[str, Any] = {
    "traceState": "",
    "droppedAttributesCount": 0,
    "droppedEventsCount": 0,
    "droppedLinksCount": 0,
    "links": [],
    "attributes": [],
    "events": [],
    "parentSpanId": "",
}


def _strip_defaults(span: dict[str, Any], *, flags: int) -> dict[str, Any]:
    """Validate and drop optional default-valued fields so the rest can be compared exactly."""
    out = dict(span)
    for key, default in OPTIONAL_SPAN_DEFAULTS.items():
        if key in out and out[key] == default:
            del out[key]
    if "flags" in out:
        assert isinstance(out["flags"], int)
        assert out["flags"] & 0xFF == flags
        del out["flags"]
    if out.get("status") in ({}, {"code": 0}, {"code": 0, "message": ""}):
        del out["status"]
    return out


def _full_span() -> Span:
    span = Span(
        "op",
        SpanContext(TID, SID, 1, TraceState([("a", "1"), ("t@s", "2")])),
        parent=SpanContext(TID, PID, 1, is_remote=True),
        kind=SpanKind.CLIENT,
        attrs={"s": "x", "n": 42},
        start_time_ns=1_000_000_000,
    )
    span.add_event("ev", {"b": True}, timestamp_ns=1_500_000_000)
    span.set_status(StatusCode.ERROR, "boom")
    span.end(end_time_ns=2_000_000_123)
    return span


def _root_span(name: str = "root") -> Span:
    span = Span(name, SpanContext(TID, SID, 1), start_time_ns=5, kind=SpanKind.INTERNAL)
    span.end(end_time_ns=9)
    return span


def _only_span(request: dict[str, Any]) -> dict[str, Any]:
    (resource_spans,) = request["resourceSpans"]
    (scope_spans,) = resource_spans["scopeSpans"]
    (span,) = scope_spans["spans"]
    return span  # type: ignore[no-any-return]


# ------------------------------------------------------------ JSON shape


def test_request_envelope() -> None:
    request = to_otlp_json([_root_span()], service="svc", resource={"env": "prod"})
    assert set(request) == {"resourceSpans"}
    (resource_spans,) = request["resourceSpans"]
    assert set(resource_spans) - {"schemaUrl"} == {"resource", "scopeSpans"}
    resource = resource_spans["resource"]
    assert resource["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "svc"}},
        {"key": "env", "value": {"stringValue": "prod"}},
    ]
    assert set(resource) - {"droppedAttributesCount"} == {"attributes"}
    (scope_spans,) = resource_spans["scopeSpans"]
    assert set(scope_spans) - {"schemaUrl"} == {"scope", "spans"}
    assert scope_spans["scope"] == {"name": "pyrock", "version": pyrock.__version__}


def test_full_span_shape_is_exact() -> None:
    span = _only_span(to_otlp_json([_full_span()], service="svc"))
    assert _strip_defaults(span, flags=1) == {
        "traceId": TID.hex(),
        "spanId": SID.hex(),
        "parentSpanId": PID.hex(),
        "traceState": "a=1,t@s=2",
        "name": "op",
        "kind": 3,
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000123",
        "attributes": [
            {"key": "s", "value": {"stringValue": "x"}},
            {"key": "n", "value": {"intValue": "42"}},
        ],
        "events": [
            {
                "timeUnixNano": "1500000000",
                "name": "ev",
                "attributes": [{"key": "b", "value": {"boolValue": True}}],
            }
        ],
        "status": {"code": 2, "message": "boom"},
    }


def test_root_span_shape_is_exact() -> None:
    span = _only_span(to_otlp_json([_root_span()], service="svc"))
    assert _strip_defaults(span, flags=1) == {
        "traceId": TID.hex(),
        "spanId": SID.hex(),
        "name": "root",
        "kind": 1,
        "startTimeUnixNano": "5",
        "endTimeUnixNano": "9",
    }


def test_event_without_attributes() -> None:
    span = Span("e", SpanContext(TID, SID, 1), start_time_ns=1)
    span.add_event("plain", timestamp_ns=3)
    span.end(end_time_ns=4)
    (event,) = _only_span(to_otlp_json([span], service="svc"))["events"]
    assert event.get("attributes", []) == []
    assert {
        k: v for k, v in event.items() if k not in ("attributes", "droppedAttributesCount")
    } == {
        "timeUnixNano": "3",
        "name": "plain",
    }


@pytest.mark.parametrize(
    ("kind", "number"),
    [
        (SpanKind.INTERNAL, 1),
        (SpanKind.SERVER, 2),
        (SpanKind.CLIENT, 3),
        (SpanKind.PRODUCER, 4),
        (SpanKind.CONSUMER, 5),
    ],
)
def test_kind_is_integer_enum(kind: SpanKind, number: int) -> None:
    span = Span("k", SpanContext(TID, SID, 1), kind=kind, start_time_ns=1)
    span.end(end_time_ns=2)
    value = _only_span(to_otlp_json([span], service="svc"))["kind"]
    assert value == number
    assert type(value) is int


def test_ok_status() -> None:
    span = Span("k", SpanContext(TID, SID, 1), start_time_ns=1)
    span.set_status(StatusCode.OK)
    span.end(end_time_ns=2)
    status = _only_span(to_otlp_json([span], service="svc"))["status"]
    assert status.get("code") == 1
    assert status.get("message", "") == ""


def test_ids_are_lowercase_hex() -> None:
    span = Span("k", SpanContext(b"\xab" * 16, b"\xcd" * 8, 1), start_time_ns=1)
    span.end(end_time_ns=2)
    out = _only_span(to_otlp_json([span], service="svc"))
    assert out["traceId"] == "ab" * 16
    assert out["spanId"] == "cd" * 8


def test_spans_keep_order() -> None:
    request = to_otlp_json([_root_span(f"s{i}") for i in range(5)], service="svc")
    names = [s["name"] for s in request["resourceSpans"][0]["scopeSpans"][0]["spans"]]
    assert names == [f"s{i}" for i in range(5)]


ATTRIBUTE_CASES = [
    pytest.param("hello", {"stringValue": "hello"}, id="str"),
    pytest.param(True, {"boolValue": True}, id="bool"),
    pytest.param(False, {"boolValue": False}, id="bool-false"),
    pytest.param(42, {"intValue": "42"}, id="int"),
    pytest.param(-(2**63), {"intValue": str(-(2**63))}, id="int64-min"),
    pytest.param(2**63 - 1, {"intValue": str(2**63 - 1)}, id="int64-max"),
    pytest.param(2**70, {"stringValue": str(2**70)}, id="out-of-range-int"),
    pytest.param(1.5, {"doubleValue": 1.5}, id="float"),
    pytest.param(datetime.timedelta(seconds=5), {"doubleValue": 5000.0}, id="timedelta-ms"),
    pytest.param(
        b"\x00\x01\xffpy",
        {"bytesValue": base64.b64encode(b"\x00\x01\xffpy").decode()},
        id="bytes-base64",
    ),
    pytest.param(
        ["a", "b"],
        {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}},
        id="str-list",
    ),
    pytest.param(
        (1, 2),
        {"arrayValue": {"values": [{"intValue": "1"}, {"intValue": "2"}]}},
        id="int-tuple",
    ),
    pytest.param(
        [True, False],
        {"arrayValue": {"values": [{"boolValue": True}, {"boolValue": False}]}},
        id="bool-list",
    ),
]


@pytest.mark.parametrize(("value", "expected"), ATTRIBUTE_CASES)
def test_attribute_value_mapping(value: object, expected: dict[str, Any]) -> None:
    span = Span("t", SpanContext(TID, SID, 1), attrs={"k": value}, start_time_ns=1)  # type: ignore[dict-item]
    span.end(end_time_ns=2)
    (attribute,) = _only_span(to_otlp_json([span], service="svc"))["attributes"]
    assert attribute == {"key": "k", "value": expected}


def test_datetime_attribute_is_rfc3339() -> None:
    moment = datetime.datetime(2026, 11, 5, 12, 0, 1, 250000, tzinfo=datetime.UTC)
    span = Span("t", SpanContext(TID, SID, 1), attrs={"at": moment}, start_time_ns=1)
    span.end(end_time_ns=2)
    (attribute,) = _only_span(to_otlp_json([span], service="svc"))["attributes"]
    text = attribute["value"]["stringValue"]
    assert datetime.datetime.fromisoformat(text.replace("Z", "+00:00")) == moment


def test_unsupported_attribute_is_dropped() -> None:
    attrs = {"ok": 1, "bad": object()}
    span = Span("t", SpanContext(TID, SID, 1), attrs=attrs, start_time_ns=1)  # type: ignore[arg-type]
    span.end(end_time_ns=2)
    keys = [a["key"] for a in _only_span(to_otlp_json([span], service="svc"))["attributes"]]
    assert keys == ["ok"]


def test_encode_json_matches_object_and_is_utf8() -> None:
    spans = [_full_span()]
    body = encode_json(spans, service="své", resource={"env": "prod"})
    assert isinstance(body, bytes)
    assert json.loads(body.decode("utf-8")) == to_otlp_json(
        spans, service="své", resource={"env": "prod"}
    )


def test_encode_json_is_valid_for_collector_decoder() -> None:
    from tests.support.otlp_decode import flatten_spans

    (flat,) = flatten_spans(json.loads(encode_json([_full_span()], service="svc")))
    assert flat["name"] == "op"
    assert flat["parent_span_id"] == PID.hex()
    assert flat["attributes"] == {"s": "x", "n": 42}
    assert flat["status_code"] == 2


# ---------------------------------------------------------------- export


def test_export_posts_json(otlp_collector: OTLPHTTPCollector) -> None:
    exporter = HTTPExporter(
        otlp_collector.traces_url,
        service="svc",
        resource={"env": "prod"},
        headers={"X-Api-Key": "secret", "x-tenant": "t1"},
    )
    spans = [_full_span(), _root_span()]
    try:
        exporter.export(spans)
    finally:
        exporter.shutdown()
    (request,) = otlp_collector.trace_requests()
    assert request.method == "POST"
    assert request.path == "/v1/traces"
    assert request.header("content-type") == "application/json"
    assert request.header("x-api-key") == "secret"
    assert request.header("x-tenant") == "t1"
    assert request.json() == to_otlp_json(spans, service="svc", resource={"env": "prod"})
    assert [s["name"] for s in otlp_collector.spans()] == ["op", "root"]


def test_endpoint_property(otlp_collector: OTLPHTTPCollector) -> None:
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc")
    try:
        assert exporter.endpoint == otlp_collector.traces_url
    finally:
        exporter.shutdown()


def test_export_empty_batch_sends_nothing(otlp_collector: OTLPHTTPCollector) -> None:
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc")
    try:
        exporter.export([])
    finally:
        exporter.shutdown()
    assert otlp_collector.requests == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_4xx_is_permanent(otlp_collector: OTLPHTTPCollector, status: int) -> None:
    otlp_collector.fail_next(1, status=status, body=b"rejected")
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc")
    try:
        with pytest.raises(PermanentExportError) as info:
            exporter.export([_root_span()])
    finally:
        exporter.shutdown()
    assert str(status) in str(info.value)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_429_and_5xx_are_retryable(otlp_collector: OTLPHTTPCollector, status: int) -> None:
    otlp_collector.fail_next(1, status=status)
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc")
    try:
        with pytest.raises(ExportError) as info:
            exporter.export([_root_span()])
        exporter.export([_root_span()])  # the next one succeeds
    finally:
        exporter.shutdown()
    assert not isinstance(info.value, PermanentExportError)
    assert str(status) in str(info.value)
    assert len(otlp_collector.trace_requests()) == 2


@pytest.mark.parametrize("status", [200, 202, 204])
def test_2xx_is_success(otlp_collector: OTLPHTTPCollector, status: int) -> None:
    otlp_collector.fail_next(1, status=status)
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc")
    try:
        exporter.export([_root_span()])
    finally:
        exporter.shutdown()


def test_timeout_is_retryable(otlp_collector: OTLPHTTPCollector) -> None:
    otlp_collector.hang()
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc", timeout=30.0)
    try:
        with pytest.raises(ExportError) as info:
            exporter.export([_root_span()], timeout=0.3)
    finally:
        otlp_collector.release()
        exporter.shutdown()
    assert not isinstance(info.value, PermanentExportError)


def test_constructor_timeout_is_default(otlp_collector: OTLPHTTPCollector) -> None:
    otlp_collector.hang()
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc", timeout=0.3)
    try:
        with pytest.raises(ExportError):
            exporter.export([_root_span()])
    finally:
        otlp_collector.release()
        exporter.shutdown()


def test_connection_refused_is_retryable() -> None:
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    exporter = HTTPExporter(f"http://127.0.0.1:{port}/v1/traces", service="svc", timeout=2.0)
    try:
        with pytest.raises(ExportError) as info:
            exporter.export([_root_span()])
    finally:
        exporter.shutdown()
    assert not isinstance(info.value, PermanentExportError)


def test_export_after_shutdown_sends_nothing(otlp_collector: OTLPHTTPCollector) -> None:
    exporter = HTTPExporter(otlp_collector.traces_url, service="svc")
    exporter.shutdown()
    exporter.shutdown()
    with contextlib.suppress(ExportError):
        exporter.export([_root_span()])
    assert otlp_collector.requests == []
