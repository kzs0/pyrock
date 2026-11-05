"""The hand-written protobuf encoder (``pyrock.trace.otlp._pb``).

Expectations are built with an independent reference encoder (``_pbref``). The
OTLP request layout follows Go's ``trace/otlp/proto.go``: fields in ascending
order, proto3 defaults omitted at the schema level (no parent for roots, no
empty trace state, no status when UNSET), events and status as nested messages.
"""

from __future__ import annotations

import datetime
import math
import struct

import pytest

from pyrock.trace import Span, SpanContext, SpanKind, StatusCode, TraceState
from pyrock.trace.otlp import _pb
from pyrock.trace.otlp._pb import Writer, encode_any_value, encode_key_value, encode_varint
from tests.unit.trace_otlp import _pbref as ref

TID = bytes(range(1, 17))
SID = bytes(range(1, 9))
PID = bytes(range(0x10, 0x18))


# ------------------------------------------------------------- constants


def test_wire_types() -> None:
    assert (_pb.WIRE_VARINT, _pb.WIRE_FIXED64, _pb.WIRE_LEN, _pb.WIRE_FIXED32) == (0, 1, 2, 5)


# ---------------------------------------------------------------- varint

VARINTS = [
    (0, "00"),
    (1, "01"),
    (127, "7f"),
    (128, "8001"),
    (150, "9601"),
    (300, "ac02"),
    (16383, "ff7f"),
    (16384, "808001"),
    (2**32 - 1, "ffffffff0f"),
    (2**63 - 1, "ffffffffffffffff7f"),
    (2**63, "80808080808080808001"),
    (2**64 - 1, "ffffffffffffffffff01"),
    (-1, "ffffffffffffffffff01"),
    (-2, "feffffffffffffffff01"),
    (-(2**63), "80808080808080808001"),
]


@pytest.mark.parametrize(("value", "expected"), VARINTS)
def test_encode_varint(value: int, expected: str) -> None:
    assert encode_varint(value).hex() == expected


def test_negative_varints_are_ten_bytes() -> None:
    for value in (-1, -150, -(2**31), -(2**63)):
        assert len(encode_varint(value)) == 10


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1])
def test_encode_varint_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        encode_varint(value)


def test_encode_varint_matches_reference_on_many_values() -> None:
    values = [2**k + d for k in range(64) for d in (-1, 0, 1) if 0 <= 2**k + d < 2**64]
    for value in values:
        assert encode_varint(value) == ref.varint(value), value


# ---------------------------------------------------------------- Writer


def test_writer_empty() -> None:
    w = Writer()
    assert w.getvalue() == b""
    assert len(w) == 0


def test_writer_varint_field() -> None:
    w = Writer()
    w.varint(1, 150)
    assert w.getvalue().hex() == "089601"
    assert len(w) == 3


def test_writer_writes_default_values() -> None:
    w = Writer()
    w.varint(1, 0)
    w.bool_(2, False)
    w.string(3, "")
    w.bytes_(4, b"")
    assert w.getvalue().hex() == "0800" + "1000" + "1a00" + "2200"


def test_writer_negative_int64_varint() -> None:
    w = Writer()
    w.varint(3, -1)
    assert w.getvalue() == ref.tag(3, 0) + bytes.fromhex("ffffffffffffffffff01")


@pytest.mark.parametrize(
    ("value", "zigzag"),
    [(0, 0), (-1, 1), (1, 2), (-2, 3), (2, 4), (2**31 - 1, 2**32 - 2), (-(2**31), 2**32 - 1)],
)
def test_writer_sint_zigzag(value: int, zigzag: int) -> None:
    w = Writer()
    w.sint(1, value)
    assert w.getvalue() == ref.tag(1, 0) + ref.varint(zigzag)


def test_writer_sint_int64_extremes() -> None:
    w = Writer()
    w.sint(1, -(2**63))
    w.sint(2, 2**63 - 1)
    assert w.getvalue() == ref.vfield(1, 2**64 - 1) + ref.vfield(2, 2**64 - 2)


def test_writer_bool() -> None:
    w = Writer()
    w.bool_(1, True)
    w.bool_(2, False)
    assert w.getvalue().hex() == "0801" + "1000"


def test_writer_fixed64_little_endian() -> None:
    w = Writer()
    w.fixed64(1, 1)
    w.fixed64(2, 2**64 - 1)
    w.fixed64(3, 0x0102030405060708)
    assert w.getvalue() == (
        bytes.fromhex("09" + "0100000000000000")
        + bytes.fromhex("11" + "ff" * 8)
        + bytes.fromhex("19" + "0807060504030201")
    )


def test_writer_fixed32_little_endian() -> None:
    w = Writer()
    w.fixed32(1, 1)
    w.fixed32(2, 0x01020304)
    assert w.getvalue() == bytes.fromhex("0d01000000" + "1504030201")


@pytest.mark.parametrize("value", [0.0, 1.0, -1.5, 1e300, math.inf, -math.inf])
def test_writer_double(value: float) -> None:
    w = Writer()
    w.double(4, value)
    assert w.getvalue() == ref.tag(4, 1) + struct.pack("<d", value)


def test_writer_double_nan() -> None:
    w = Writer()
    w.double(1, math.nan)
    data = w.getvalue()
    assert data[:1] == ref.tag(1, 1)
    assert math.isnan(struct.unpack("<d", data[1:])[0])


def test_writer_string_is_utf8_length_delimited() -> None:
    w = Writer()
    w.string(2, "testing")
    w.string(3, "hé")
    assert w.getvalue() == bytes.fromhex("120774657374696e67") + b"\x1a\x03h\xc3\xa9"


def test_writer_bytes() -> None:
    w = Writer()
    w.bytes_(4, b"\x00\xff")
    assert w.getvalue().hex() == "220200ff"


def test_writer_long_length_prefix() -> None:
    w = Writer()
    w.bytes_(1, b"x" * 300)
    assert w.getvalue() == b"\x0a" + bytes.fromhex("ac02") + b"x" * 300


def test_writer_nested_message_from_writer_and_bytes() -> None:
    inner = Writer()
    inner.varint(1, 150)
    outer = Writer()
    outer.message(3, inner)
    outer.message(4, b"\x08\x01")
    outer.message(5, b"")
    assert outer.getvalue().hex() == "1a03089601" + "22020801" + "2a00"


def test_writer_deeply_nested_messages() -> None:
    level3 = Writer()
    level3.string(1, "x" * 200)
    level2 = Writer()
    level2.message(1, level3)
    level1 = Writer()
    level1.message(2, level2)
    expected = ref.ld(2, ref.ld(1, ref.ld(1, "x" * 200)))
    assert level1.getvalue() == expected


def test_writer_packed_varints() -> None:
    w = Writer()
    w.packed_varints(4, [3, 270, 86942])
    assert w.getvalue().hex() == "2206038e029ea705"


def test_writer_packed_varints_negative() -> None:
    w = Writer()
    w.packed_varints(1, [-1])
    assert w.getvalue() == ref.ld(1, ref.varint(-1))


def test_writer_large_field_numbers() -> None:
    w = Writer()
    w.varint(16, 1)
    w.varint(2047, 1)
    w.varint(2**29 - 1, 1)
    assert w.getvalue() == ref.vfield(16, 1) + ref.vfield(2047, 1) + ref.vfield(2**29 - 1, 1)
    assert w.getvalue()[:3] == bytes.fromhex("800101")


def test_writer_preserves_call_order() -> None:
    w = Writer()
    w.varint(3, 1)
    w.string(1, "a")
    w.varint(2, 2)
    assert w.getvalue() == ref.vfield(3, 1) + ref.ld(1, "a") + ref.vfield(2, 2)


def test_writer_getvalue_is_repeatable() -> None:
    w = Writer()
    w.varint(1, 1)
    first = w.getvalue()
    w.varint(2, 2)
    assert first == ref.vfield(1, 1)
    assert w.getvalue() == ref.vfield(1, 1) + ref.vfield(2, 2)


# ------------------------------------------------------- AnyValue/KeyValue

ANY_VALUES = [
    pytest.param("v", ref.any_str("v"), id="str"),
    pytest.param("", ref.any_str(""), id="empty-str"),
    pytest.param(True, ref.any_bool(True), id="bool-true"),
    pytest.param(False, ref.any_bool(False), id="bool-false"),
    pytest.param(7, ref.any_int(7), id="int"),
    pytest.param(0, ref.any_int(0), id="int-zero"),
    pytest.param(-1, ref.any_int(-1), id="int-negative"),
    pytest.param(-(2**63), ref.any_int(-(2**63)), id="int64-min"),
    pytest.param(2**63 - 1, ref.any_int(2**63 - 1), id="int64-max"),
    pytest.param(2**63, ref.any_str(str(2**63)), id="int-above-int64-is-string"),
    pytest.param(2**70, ref.any_str(str(2**70)), id="huge-int-is-string"),
    pytest.param(-(2**63) - 1, ref.any_str(str(-(2**63) - 1)), id="int-below-int64-is-string"),
    pytest.param(1.5, ref.any_double(1.5), id="float"),
    pytest.param(b"\x00\x01", ref.any_bytes(b"\x00\x01"), id="bytes"),
    pytest.param(
        datetime.timedelta(seconds=5), ref.any_double(5000.0), id="timedelta-is-double-ms"
    ),
    pytest.param(["a", "b"], ref.any_array(ref.any_str("a"), ref.any_str("b")), id="str-sequence"),
    pytest.param((1, 2), ref.any_array(ref.any_int(1), ref.any_int(2)), id="int-tuple"),
    pytest.param(
        [True, False], ref.any_array(ref.any_bool(True), ref.any_bool(False)), id="bool-list"
    ),
    pytest.param([0.5], ref.any_array(ref.any_double(0.5)), id="float-list"),
]


@pytest.mark.parametrize(("value", "expected"), ANY_VALUES)
def test_encode_any_value(value: object, expected: bytes) -> None:
    assert encode_any_value(value) == expected  # type: ignore[arg-type]


def test_encode_any_value_datetime_is_rfc3339_string() -> None:
    moment = datetime.datetime(2026, 11, 5, 12, 0, 1, 250000, tzinfo=datetime.UTC)
    encoded = encode_any_value(moment)
    assert encoded is not None
    ((field, wire, text),) = ref.parse(encoded)
    assert (field, wire) == (1, ref.LEN)
    assert isinstance(text, bytes)
    parsed = datetime.datetime.fromisoformat(text.decode().replace("Z", "+00:00"))
    assert parsed == moment


@pytest.mark.parametrize("value", [object(), None, {"a": 1}, {1, 2}])
def test_encode_any_value_unsupported_is_none(value: object) -> None:
    assert encode_any_value(value) is None  # type: ignore[arg-type]


def test_encode_key_value() -> None:
    assert encode_key_value("k", "v") == ref.kv("k", ref.any_str("v"))
    assert encode_key_value("n", 3) == ref.kv("n", ref.any_int(3))
    assert encode_key_value("k", "v") == bytes.fromhex("0a016b12030a0176")


def test_encode_key_value_unsupported_is_none() -> None:
    assert encode_key_value("k", object()) is None  # type: ignore[arg-type]


# ----------------------------------------------- ExportTraceServiceRequest


def _golden_span() -> Span:
    span = Span(
        "op",
        SpanContext(TID, SID, 1),
        parent=SpanContext(TID, PID, 1, is_remote=True),
        kind=SpanKind.SERVER,
        attrs={"k": "v", "n": 7},
        start_time_ns=1_000_000_000,
    )
    span.add_event("ev", {"b": True}, timestamp_ns=1_500_000_000)
    span.set_status(StatusCode.ERROR, "boom")
    span.end(end_time_ns=2_000_000_000)
    return span


def _expected_golden_span() -> bytes:
    return b"".join(
        [
            ref.ld(1, TID),
            ref.ld(2, SID),
            ref.ld(4, PID),
            ref.ld(5, "op"),
            ref.vfield(6, 2),
            ref.f64(7, 1_000_000_000),
            ref.f64(8, 2_000_000_000),
            ref.ld(9, ref.kv("k", ref.any_str("v"))),
            ref.ld(9, ref.kv("n", ref.any_int(7))),
            ref.ld(
                11,
                ref.f64(1, 1_500_000_000)
                + ref.ld(2, "ev")
                + ref.ld(3, ref.kv("b", ref.any_bool(True))),
            ),
            ref.ld(15, ref.ld(2, "boom") + ref.vfield(3, 2)),
        ]
    )


def _wrap(spans: bytes, *, version: str = "1.2.3", resource: bytes = b"") -> bytes:
    resource_msg = ref.ld(1, ref.kv("service.name", ref.any_str("svc"))) + resource
    scope = ref.ld(1, "pyrock") + (ref.ld(2, version) if version else b"")
    scope_spans = ref.ld(1, scope) + spans
    resource_spans = ref.ld(1, resource_msg) + ref.ld(2, scope_spans)
    return ref.ld(1, resource_spans)


def test_export_trace_request_golden_bytes() -> None:
    data = _pb.encode_export_trace_request(
        [_golden_span()],
        service="svc",
        resource={"env": "prod"},
        scope_name="pyrock",
        scope_version="1.2.3",
    )
    expected = _wrap(
        ref.ld(2, _expected_golden_span()),
        resource=ref.ld(1, ref.kv("env", ref.any_str("prod"))),
    )
    assert data.hex() == expected.hex()


def test_export_trace_request_root_span_minimal() -> None:
    span = Span("root", SpanContext(TID, SID, 1), start_time_ns=5, kind=SpanKind.INTERNAL)
    span.end(end_time_ns=9)
    data = _pb.encode_export_trace_request([span], service="svc", scope_version="")
    expected_span = (
        ref.ld(1, TID) + ref.ld(2, SID) + ref.ld(5, "root") + ref.vfield(6, 1)
        + ref.f64(7, 5) + ref.f64(8, 9)
    )  # fmt: skip
    assert data.hex() == _wrap(ref.ld(2, expected_span), version="").hex()


def test_export_trace_request_trace_state_field_3() -> None:
    state = TraceState([("a", "1"), ("b", "2")])
    span = Span("s", SpanContext(TID, SID, 1, state), start_time_ns=1)
    span.end(end_time_ns=2)
    data = _pb.encode_export_trace_request([span], service="svc")
    span_fields = _span_fields(data)[0]
    assert (3, ref.LEN, b"a=1,b=2") in span_fields


def test_export_trace_request_many_spans_keep_order() -> None:
    spans = []
    for i in range(3):
        span = Span(f"s{i}", SpanContext(TID, bytes([i + 1]) * 8, 1), start_time_ns=1)
        span.end(end_time_ns=2)
        spans.append(span)
    data = _pb.encode_export_trace_request(spans, service="svc")
    names = [_field(fields, 5) for fields in _span_fields(data)]
    assert names == [b"s0", b"s1", b"s2"]


def test_export_trace_request_drops_unsupported_attributes() -> None:
    span = Span("s", SpanContext(TID, SID, 1), attrs={"ok": 1, "bad": object()}, start_time_ns=1)  # type: ignore[dict-item]
    span.end(end_time_ns=2)
    data = _pb.encode_export_trace_request([span], service="svc")
    keys = [ref.parse(v)[0][2] for f, _, v in _span_fields(data)[0] if f == 9]  # type: ignore[arg-type]
    assert keys == [b"ok"]


def _field(fields: list[tuple[int, int, int | bytes]], number: int) -> int | bytes:
    (value,) = [v for f, _, v in fields if f == number]
    return value


def _span_fields(data: bytes) -> list[list[tuple[int, int, int | bytes]]]:
    resource_spans = _field(ref.parse(data), 1)
    assert isinstance(resource_spans, bytes)
    scope_spans = _field(ref.parse(resource_spans), 2)
    assert isinstance(scope_spans, bytes)
    return [ref.parse(v) for f, _, v in ref.parse(scope_spans) if f == 2]  # type: ignore[arg-type]


@pytest.mark.compat
def test_export_trace_request_decodes_with_official_schema() -> None:
    from tests.support.otlp_decode import decode_trace_request_pb

    data = _pb.encode_export_trace_request(
        [_golden_span()], service="svc", resource={"env": "prod"}, scope_version="1.2.3"
    )
    (span,) = decode_trace_request_pb(data)
    assert span["name"] == "op"
    assert span["trace_id"] == TID.hex()
    assert span["span_id"] == SID.hex()
    assert span["parent_span_id"] == PID.hex()
    assert span["kind"] == 2
    assert span["start_time_unix_nano"] == 1_000_000_000
    assert span["end_time_unix_nano"] == 2_000_000_000
    assert span["attributes"] == {"k": "v", "n": 7}
    assert span["events"] == [
        {"name": "ev", "time_unix_nano": 1_500_000_000, "attributes": {"b": True}}
    ]
    assert span["status_code"] == 2
    assert span["status_message"] == "boom"
    assert span["resource"] == {"service.name": "svc", "env": "prod"}
    assert span["scope_name"] == "pyrock"
    assert span["scope_version"] == "1.2.3"


@pytest.mark.compat
def test_typed_attributes_decode_with_official_schema() -> None:
    from tests.support.otlp_decode import decode_trace_request_pb

    attrs = {
        "s": "x",
        "b": True,
        "i": -(2**63),
        "f": 0.25,
        "raw": b"\x00\xff",
        "td": datetime.timedelta(milliseconds=1500),
        "tags": ["a", "b"],
        "big": 2**64,
    }
    span = Span("typed", SpanContext(TID, SID, 1), attrs=attrs, start_time_ns=1)  # type: ignore[arg-type]
    span.end(end_time_ns=2)
    (decoded,) = decode_trace_request_pb(_pb.encode_export_trace_request([span], service="svc"))
    assert decoded["attributes"] == {
        "s": "x",
        "b": True,
        "i": -(2**63),
        "f": 0.25,
        "raw": b"\x00\xff",
        "td": 1500.0,
        "tags": ["a", "b"],
        "big": str(2**64),
    }
