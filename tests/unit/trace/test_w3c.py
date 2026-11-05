"""W3C Trace Context: ``traceparent`` parse/format and ``tracestate`` grammar.

Mirrors Go's ``trace/w3c`` rules (see the module docstring of
:mod:`pyrock.trace.w3c`): lowercase hex only, non-zero IDs, version ``ff``
forbidden, future versions parsed by their first four fields, tracestate keys
``simple`` or ``tenant@system``, values printable ASCII without ``,``/``=``
and no trailing space, at most 32 members, duplicates invalid.
"""

from __future__ import annotations

import pytest

from pyrock.trace import SpanContext, TraceState
from pyrock.trace import w3c
from pyrock.trace.w3c import InvalidTraceparent, InvalidTracestate

TID = "0af7651916cd43dd8448eb211c80319c"
SID = "b7ad6b7169203331"
TID_B = bytes.fromhex(TID)
SID_B = bytes.fromhex(SID)
ZERO_TID = "0" * 32
ZERO_SID = "0" * 16


# ------------------------------------------------------------- constants


def test_constants_match_go() -> None:
    assert w3c.TRACEPARENT_HEADER == "traceparent"
    assert w3c.TRACESTATE_HEADER == "tracestate"
    assert w3c.MAX_TRACESTATE_ENTRIES == 32
    assert w3c.SAMPLED_FLAG == 0x01


def test_error_classes_are_value_errors() -> None:
    assert issubclass(InvalidTraceparent, ValueError)
    assert issubclass(InvalidTracestate, ValueError)
    assert not issubclass(InvalidTraceparent, InvalidTracestate)


# ------------------------------------------------------------- traceparent

VALID_TRACEPARENTS = [
    pytest.param(f"00-{TID}-{SID}-01", 0x01, id="v00-sampled"),
    pytest.param(f"00-{TID}-{SID}-00", 0x00, id="v00-unsampled"),
    pytest.param(f"00-{TID}-{SID}-09", 0x09, id="v00-unknown-flag-bits-kept"),
    pytest.param(f"00-{TID}-{SID}-ff", 0xFF, id="v00-all-flags"),
    pytest.param(f"01-{TID}-{SID}-01", 0x01, id="future-version"),
    pytest.param(f"fe-{TID}-{SID}-00", 0x00, id="future-version-fe"),
    pytest.param(
        f"cc-{TID}-{SID}-01-what-the-future-will-be-like", 0x01, id="future-version-extra-fields"
    ),
    pytest.param(f"01-{TID}-{SID}-01-x", 0x01, id="future-version-one-extra-field"),
]


@pytest.mark.parametrize(("value", "flags"), VALID_TRACEPARENTS)
def test_parse_traceparent_valid(value: str, flags: int) -> None:
    trace_id, span_id, got_flags = w3c.parse_traceparent(value)
    assert trace_id == TID_B
    assert span_id == SID_B
    assert got_flags == flags
    assert isinstance(trace_id, bytes)
    assert isinstance(span_id, bytes)
    assert isinstance(got_flags, int)


INVALID_TRACEPARENTS = [
    pytest.param("", id="empty"),
    pytest.param("garbage", id="garbage"),
    pytest.param(f"00-{TID}-{SID}", id="missing-flags"),
    pytest.param(f"00-{TID}-{SID}-", id="empty-flags"),
    pytest.param(f"00-{TID}", id="only-trace-id"),
    # version
    pytest.param(f"ff-{TID}-{SID}-01", id="version-ff-forbidden"),
    pytest.param(f"0-{TID}-{SID}-01", id="version-too-short"),
    pytest.param(f"000-{TID}-{SID}-01", id="version-too-long"),
    pytest.param(f"0A-{TID}-{SID}-01", id="version-uppercase"),
    pytest.param(f"zz-{TID}-{SID}-01", id="version-not-hex"),
    pytest.param(f"00-{TID}-{SID}-01-extra", id="v00-extra-field"),
    pytest.param(f"00-{TID}-{SID}-01-", id="v00-trailing-dash"),
    # trace id
    pytest.param(f"00-{TID.upper()}-{SID}-01", id="trace-id-uppercase"),
    pytest.param(f"00-{TID[:-1]}-{SID}-01", id="trace-id-31"),
    pytest.param(f"00-{TID}0-{SID}-01", id="trace-id-33"),
    pytest.param(f"00-{ZERO_TID}-{SID}-01", id="trace-id-zero"),
    pytest.param(f"00-{TID[:-1]}g-{SID}-01", id="trace-id-not-hex"),
    pytest.param(f"01-{ZERO_TID}-{SID}-01", id="future-version-zero-trace-id"),
    # span id
    pytest.param(f"00-{TID}-{SID.upper()}-01", id="span-id-uppercase"),
    pytest.param(f"00-{TID}-{SID[:-1]}-01", id="span-id-15"),
    pytest.param(f"00-{TID}-{SID}0-01", id="span-id-17"),
    pytest.param(f"00-{TID}-{ZERO_SID}-01", id="span-id-zero"),
    pytest.param(f"00-{TID}-{SID[:-1]}x-01", id="span-id-not-hex"),
    pytest.param(f"01-{TID}-{ZERO_SID}-01", id="future-version-zero-span-id"),
    # flags
    pytest.param(f"00-{TID}-{SID}-0A", id="flags-uppercase"),
    pytest.param(f"00-{TID}-{SID}-1", id="flags-one-char"),
    pytest.param(f"00-{TID}-{SID}-001", id="flags-three-chars"),
    pytest.param(f"00-{TID}-{SID}-0g", id="flags-not-hex"),
    pytest.param(f"01-{TID}-{SID}-01x", id="future-version-flags-not-delimited"),
    # separators and whitespace
    pytest.param(f"00_{TID}_{SID}_01", id="wrong-separator"),
    pytest.param(f" 00-{TID}-{SID}-01", id="leading-space"),
    pytest.param(f"00-{TID}-{SID}-01 ", id="trailing-space"),
    pytest.param(f"00-{TID}-{SID}--01", id="double-dash"),
]


@pytest.mark.parametrize("value", INVALID_TRACEPARENTS)
def test_parse_traceparent_invalid(value: str) -> None:
    with pytest.raises(InvalidTraceparent):
        w3c.parse_traceparent(value)


def test_format_traceparent_sampled() -> None:
    sc = SpanContext(TID_B, SID_B, 0x01)
    assert w3c.format_traceparent(sc) == f"00-{TID}-{SID}-01"


def test_format_traceparent_unsampled() -> None:
    sc = SpanContext(TID_B, SID_B, 0x00)
    assert w3c.format_traceparent(sc) == f"00-{TID}-{SID}-00"


def test_format_traceparent_is_lowercase_hex() -> None:
    sc = SpanContext(b"\xab" * 16, b"\xcd" * 8, 1)
    value = w3c.format_traceparent(sc)
    assert value == value.lower()
    assert value == "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"


def test_format_parse_round_trip() -> None:
    sc = SpanContext(TID_B, SID_B, 0x01)
    assert w3c.parse_traceparent(w3c.format_traceparent(sc)) == (TID_B, SID_B, 0x01)


def test_span_context_from_traceparent_is_remote() -> None:
    sc = w3c.span_context_from_traceparent(f"00-{TID}-{SID}-01")
    assert isinstance(sc, SpanContext)
    assert sc.trace_id == TID_B
    assert sc.span_id == SID_B
    assert sc.sampled is True
    assert sc.is_remote is True
    assert len(sc.trace_state) == 0


def test_span_context_from_traceparent_unsampled() -> None:
    sc = w3c.span_context_from_traceparent(f"00-{TID}-{SID}-00")
    assert sc.sampled is False
    assert sc.is_remote is True


def test_span_context_from_traceparent_with_tracestate() -> None:
    sc = w3c.span_context_from_traceparent(f"00-{TID}-{SID}-01", "a=1,t@s=2")
    assert sc.trace_state.entries() == (("a", "1"), ("t@s", "2"))


def test_span_context_from_traceparent_ignores_invalid_tracestate() -> None:
    sc = w3c.span_context_from_traceparent(f"00-{TID}-{SID}-01", "INVALID KEY=1")
    assert sc.trace_id == TID_B
    assert len(sc.trace_state) == 0


def test_span_context_from_traceparent_empty_tracestate() -> None:
    sc = w3c.span_context_from_traceparent(f"00-{TID}-{SID}-01", "")
    assert len(sc.trace_state) == 0


def test_span_context_from_traceparent_rejects_invalid() -> None:
    with pytest.raises(InvalidTraceparent):
        w3c.span_context_from_traceparent(f"00-{ZERO_TID}-{SID}-01", "a=1")


# -------------------------------------------------------- tracestate keys

VALID_KEYS = [
    "a",
    "foo",
    "vendor",
    "a0",
    "a_-*/b",
    "z" * 256,
    "tenant@system",
    "0tenant@sys",
    "t@s",
    "9@a",
    "t" * 241 + "@" + "s" * 14,
    "t@s_-*/0",
]


@pytest.mark.parametrize("key", VALID_KEYS)
def test_valid_tracestate_keys(key: str) -> None:
    assert w3c.is_valid_tracestate_key(key) is True


INVALID_KEYS = [
    pytest.param("", id="empty"),
    pytest.param("A", id="uppercase"),
    pytest.param("fOo", id="mixed-case"),
    pytest.param("1abc", id="simple-digit-first"),
    pytest.param("_abc", id="simple-underscore-first"),
    pytest.param("a.b", id="dot"),
    pytest.param("a b", id="space"),
    pytest.param("a=b", id="equals"),
    pytest.param("a,b", id="comma"),
    pytest.param("z" * 257, id="simple-257"),
    pytest.param("a@b@c", id="two-ats"),
    pytest.param("tenant@", id="empty-system"),
    pytest.param("@sys", id="empty-tenant"),
    pytest.param("t@" + "s" * 15, id="system-15"),
    pytest.param("t" * 242 + "@s", id="tenant-242"),
    pytest.param("t@1sys", id="system-digit-first"),
    pytest.param("T@sys", id="tenant-uppercase"),
    pytest.param("t@Sys", id="system-uppercase"),
    pytest.param("_t@sys", id="tenant-symbol-first"),
    pytest.param("café", id="non-ascii"),
]


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_invalid_tracestate_keys(key: str) -> None:
    assert w3c.is_valid_tracestate_key(key) is False


# ------------------------------------------------------ tracestate values

VALID_VALUES = [
    "1",
    "value",
    "hello world",
    " leading-space-is-data",
    "!\"#$%&'()*+-./:;<>?@[\\]^_`{|}~",
    "x" * 256,
    "UPPER",
]


@pytest.mark.parametrize("value", VALID_VALUES)
def test_valid_tracestate_values(value: str) -> None:
    assert w3c.is_valid_tracestate_value(value) is True


INVALID_VALUES = [
    pytest.param("", id="empty"),
    pytest.param("a,b", id="comma"),
    pytest.param("a=b", id="equals"),
    pytest.param("trailing ", id="trailing-space"),
    pytest.param("tab\tinside", id="tab"),
    pytest.param("nl\n", id="newline"),
    pytest.param("del\x7f", id="del"),
    pytest.param("café", id="non-ascii"),
    pytest.param("x" * 257, id="257-chars"),
]


@pytest.mark.parametrize("value", INVALID_VALUES)
def test_invalid_tracestate_values(value: str) -> None:
    assert w3c.is_valid_tracestate_value(value) is False


# ------------------------------------------------------ tracestate parse

PARSE_CASES = [
    pytest.param("a=1", (("a", "1"),), id="single"),
    pytest.param("a=1,b=2", (("a", "1"), ("b", "2")), id="order-preserved"),
    pytest.param("b=2,a=1", (("b", "2"), ("a", "1")), id="order-preserved-reverse"),
    pytest.param("a=1 , b=2", (("a", "1"), ("b", "2")), id="ows-around-comma"),
    pytest.param(" a=1,\tb=2\t", (("a", "1"), ("b", "2")), id="ows-around-members"),
    pytest.param("a=1,,b=2", (("a", "1"), ("b", "2")), id="empty-member"),
    pytest.param("a=1, ,b=2", (("a", "1"), ("b", "2")), id="blank-member"),
    pytest.param(",a=1,", (("a", "1"),), id="leading-trailing-commas"),
    pytest.param("t@s=x,0t@sys=y", (("t@s", "x"), ("0t@sys", "y")), id="tenant-keys"),
    pytest.param("a=hello world", (("a", "hello world"),), id="space-inside-value"),
    pytest.param("a= x", (("a", " x"),), id="leading-space-in-value"),
    pytest.param("a=1 ", (("a", "1"),), id="trailing-ows-trimmed"),
    pytest.param("k=" + "v" * 256, (("k", "v" * 256),), id="value-256"),
]


@pytest.mark.parametrize(("header", "entries"), PARSE_CASES)
def test_parse_tracestate(header: str, entries: tuple[tuple[str, str], ...]) -> None:
    state = w3c.parse_tracestate(header)
    assert isinstance(state, TraceState)
    assert state.entries() == entries


def test_parse_tracestate_empty() -> None:
    assert len(w3c.parse_tracestate("")) == 0


def test_parse_tracestate_32_entries_ok() -> None:
    header = ",".join(f"k{i}=v{i}" for i in range(32))
    state = w3c.parse_tracestate(header)
    assert len(state) == 32
    assert state.entries()[0] == ("k0", "v0")
    assert state.entries()[-1] == ("k31", "v31")


def test_parse_tracestate_33_entries_rejected() -> None:
    header = ",".join(f"k{i}=v{i}" for i in range(33))
    with pytest.raises(InvalidTracestate):
        w3c.parse_tracestate(header)


INVALID_TRACESTATES = [
    pytest.param("a", id="no-equals"),
    pytest.param("=1", id="empty-key"),
    pytest.param("a=", id="empty-value"),
    pytest.param("A=1", id="uppercase-key"),
    pytest.param("1a=1", id="digit-first-simple-key"),
    pytest.param("a=b=c", id="equals-in-value"),
    pytest.param("a=1,a=2", id="duplicate-key"),
    pytest.param("a =1", id="space-before-equals"),
    pytest.param("a=1,b", id="second-member-malformed"),
    pytest.param("a@b@c=1", id="two-ats"),
    pytest.param("k=" + "v" * 257, id="value-257"),
    pytest.param("z" * 257 + "=1", id="key-257"),
    pytest.param("a=x\ty", id="tab-in-value"),
]


@pytest.mark.parametrize("header", INVALID_TRACESTATES)
def test_parse_tracestate_invalid(header: str) -> None:
    with pytest.raises(InvalidTracestate):
        w3c.parse_tracestate(header)


# ----------------------------------------------------- tracestate format


def test_format_tracestate_from_state() -> None:
    state = TraceState([("a", "1"), ("t@s", "2")])
    assert w3c.format_tracestate(state) == "a=1,t@s=2"


def test_format_tracestate_from_pairs() -> None:
    assert w3c.format_tracestate([("b", "2"), ("a", "1")]) == "b=2,a=1"


def test_format_tracestate_empty() -> None:
    assert w3c.format_tracestate(TraceState()) == ""
    assert w3c.format_tracestate([]) == ""


def test_format_tracestate_skips_invalid_entries() -> None:
    pairs = [("a", "1"), ("BAD", "2"), ("b", "x,y"), ("c", "3")]
    assert w3c.format_tracestate(pairs) == "a=1,c=3"


def test_format_tracestate_first_duplicate_wins() -> None:
    assert w3c.format_tracestate([("a", "1"), ("b", "2"), ("a", "3")]) == "a=1,b=2"


def test_format_tracestate_caps_at_32_entries() -> None:
    pairs = [(f"k{i}", "v") for i in range(40)]
    out = w3c.format_tracestate(pairs)
    members = out.split(",")
    assert len(members) == 32
    assert members[0] == "k0=v"
    assert members[-1] == "k31=v"


def test_format_parse_tracestate_round_trip() -> None:
    state = TraceState([("congo", "t61rcWkgMzE"), ("rojo", "00f067aa0ba902b7"), ("t@s", "x y")])
    assert w3c.parse_tracestate(w3c.format_tracestate(state)) == state
