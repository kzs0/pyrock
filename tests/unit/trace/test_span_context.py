"""Pure data types: ``TraceState``, ``SpanContext``, ``SpanKind``, ``StatusCode``, ``SpanEvent``."""

from __future__ import annotations

import dataclasses

import pytest

from pyrock.trace import (
    INVALID_SPAN_ID,
    INVALID_TRACE_ID,
    SpanContext,
    SpanEvent,
    SpanKind,
    StatusCode,
    TraceState,
)

TID = bytes.fromhex("0af7651916cd43dd8448eb211c80319c")
SID = bytes.fromhex("b7ad6b7169203331")


# ------------------------------------------------------------ TraceState


def test_trace_state_is_ordered_mapping() -> None:
    state = TraceState([("b", "2"), ("a", "1")])
    assert list(state) == ["b", "a"]
    assert state["a"] == "1"
    assert len(state) == 2
    assert dict(state) == {"b": "2", "a": "1"}
    assert state.entries() == (("b", "2"), ("a", "1"))


def test_trace_state_empty_default() -> None:
    state = TraceState()
    assert len(state) == 0
    assert state.entries() == ()
    with pytest.raises(KeyError):
        state["missing"]


def test_trace_state_dedupes_first_occurrence_wins() -> None:
    state = TraceState([("a", "1"), ("b", "2"), ("a", "3")])
    assert state.entries() == (("a", "1"), ("b", "2"))


def test_trace_state_with_entry_new_key_goes_first() -> None:
    state = TraceState([("a", "1"), ("b", "2")]).with_entry("c", "3")
    assert state.entries() == (("c", "3"), ("a", "1"), ("b", "2"))


def test_trace_state_updated_key_moves_to_front() -> None:
    state = TraceState([("a", "1"), ("b", "2"), ("c", "3")]).with_entry("b", "new")
    assert state.entries() == (("b", "new"), ("a", "1"), ("c", "3"))


def test_trace_state_with_entry_returns_copy() -> None:
    original = TraceState([("a", "1")])
    updated = original.with_entry("a", "2")
    assert original.entries() == (("a", "1"),)
    assert updated.entries() == (("a", "2"),)


def test_trace_state_with_entry_at_cap_drops_oldest() -> None:
    full = TraceState([(f"k{i}", "v") for i in range(32)])
    updated = full.with_entry("new", "x")
    assert len(updated) == 32
    assert updated.entries()[0] == ("new", "x")
    assert "k31" not in updated
    assert "k30" in updated


def test_trace_state_update_at_cap_keeps_all_others() -> None:
    full = TraceState([(f"k{i}", "v") for i in range(32)])
    updated = full.with_entry("k31", "x")
    assert len(updated) == 32
    assert updated.entries()[0] == ("k31", "x")
    assert "k30" in updated


def test_trace_state_without() -> None:
    state = TraceState([("a", "1"), ("b", "2"), ("c", "3")])
    assert state.without("b").entries() == (("a", "1"), ("c", "3"))
    assert state.without("missing") == state


def test_trace_state_32_ok_33_rejected() -> None:
    assert len(TraceState([(f"k{i}", "v") for i in range(32)])) == 32
    with pytest.raises(ValueError, match="32"):
        TraceState([(f"k{i}", "v") for i in range(33)])


def test_trace_state_requires_str() -> None:
    with pytest.raises(TypeError):
        TraceState([("a", 1)])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        TraceState([(b"a", "1")])  # type: ignore[list-item]


def test_trace_state_is_immutable() -> None:
    state = TraceState([("a", "1")])
    with pytest.raises(AttributeError):
        state._entries = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        state["a"] = "2"  # type: ignore[index]


def test_trace_state_equality_and_hash_respect_order() -> None:
    a = TraceState([("a", "1"), ("b", "2")])
    b = TraceState([("a", "1"), ("b", "2")])
    c = TraceState([("b", "2"), ("a", "1")])
    assert a == b
    assert hash(a) == hash(b)
    assert a != c
    assert len({a, b, c}) == 2


def test_trace_state_repr_lists_entries() -> None:
    assert "('a', '1')" in repr(TraceState([("a", "1")]))


# ----------------------------------------------------------- SpanContext


def test_span_context_fields_and_defaults() -> None:
    sc = SpanContext(TID, SID)
    assert sc.trace_id == TID
    assert sc.span_id == SID
    assert sc.trace_flags == 0
    assert sc.trace_state == TraceState()
    assert sc.is_remote is False
    assert sc.sampled is False


def test_span_context_hex_is_lowercase_and_cached() -> None:
    sc = SpanContext(b"\xab" * 16, b"\xcd" * 8, 1)
    assert sc.trace_id_hex == "ab" * 16
    assert sc.span_id_hex == "cd" * 8
    assert sc.trace_id_hex is sc.trace_id_hex


def test_span_context_sampled_is_bit_zero() -> None:
    assert SpanContext(TID, SID, 0x01).sampled is True
    assert SpanContext(TID, SID, 0x03).sampled is True
    assert SpanContext(TID, SID, 0x02).sampled is False


@pytest.mark.parametrize(
    ("trace_id", "span_id"),
    [
        (TID[:15], SID),
        (TID + b"\x00", SID),
        (TID, SID[:7]),
        (TID, SID + b"\x00"),
        (TID.hex(), SID),
        (TID, SID.hex()),
    ],
)
def test_span_context_rejects_wrong_id_sizes(trace_id: object, span_id: object) -> None:
    with pytest.raises(ValueError, match="_id"):
        SpanContext(trace_id, span_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("flags", [-1, 256])
def test_span_context_rejects_out_of_range_flags(flags: int) -> None:
    with pytest.raises(ValueError, match="trace_flags"):
        SpanContext(TID, SID, flags)


def test_span_context_validity() -> None:
    assert SpanContext(TID, SID).is_valid is True
    assert SpanContext(INVALID_TRACE_ID, SID).is_valid is False
    assert SpanContext(TID, INVALID_SPAN_ID).is_valid is False
    assert bytes(16) == INVALID_TRACE_ID
    assert bytes(8) == INVALID_SPAN_ID


def test_span_context_is_frozen_and_hashable() -> None:
    sc = SpanContext(TID, SID, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sc.trace_flags = 0  # type: ignore[misc]
    assert hash(sc) == hash(SpanContext(TID, SID, 1))
    assert sc == SpanContext(TID, SID, 1)
    assert sc != SpanContext(TID, SID, 0)


def test_span_context_equality_ignores_hex_cache() -> None:
    assert SpanContext(TID, SID) == SpanContext(bytes(TID), bytes(SID))


def test_span_context_from_hex() -> None:
    state = TraceState([("a", "1")])
    sc = SpanContext.from_hex(TID.hex(), SID.hex(), trace_state=state, is_remote=True)
    assert sc.trace_id == TID
    assert sc.span_id == SID
    assert sc.sampled is True
    assert sc.trace_state == state
    assert sc.is_remote is True
    assert SpanContext.from_hex(TID.hex(), SID.hex(), sampled=False).trace_flags == 0


@pytest.mark.parametrize(
    ("trace_id", "span_id"),
    [(TID.hex()[:-1], SID.hex()), (TID.hex(), SID.hex() + "0"), ("g" * 32, SID.hex())],
)
def test_span_context_from_hex_rejects_malformed(trace_id: str, span_id: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - fromhex raises a bare ValueError
        SpanContext.from_hex(trace_id, span_id)


# ------------------------------------------------------------ enums, events


def test_span_kind_values_match_otlp() -> None:
    assert [(k.name, int(k)) for k in SpanKind] == [
        ("INTERNAL", 1),
        ("SERVER", 2),
        ("CLIENT", 3),
        ("PRODUCER", 4),
        ("CONSUMER", 5),
    ]


def test_status_code_values_match_otlp() -> None:
    assert (int(StatusCode.UNSET), int(StatusCode.OK), int(StatusCode.ERROR)) == (0, 1, 2)


def test_span_event_is_frozen() -> None:
    event = SpanEvent("e", 123, {"k": "v"})
    assert (event.name, event.timestamp_ns, dict(event.attrs)) == ("e", 123, {"k": "v"})
    assert dict(SpanEvent("e", 1).attrs) == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.name = "x"  # type: ignore[misc]
