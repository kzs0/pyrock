"""Attribute value normalization and text rendering (DESIGN §5 type mapping).

The private seams ``_normalize_value`` / ``_text_value`` are what the OTLP
encoders, the log handler and the metric label resolver share, so their
contract is the type-mapping table.
"""

from __future__ import annotations

import base64
import datetime
import random
import re

import pytest

from pyrock import attr
from pyrock.attr import _normalize_value, _text_value

INT64_MAX = (1 << 63) - 1
INT64_MIN = -(1 << 63)
UTC = datetime.UTC
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class Leaky:
    """An object whose rendering would leak; it records any attempt."""

    rendered = False

    def __repr__(self) -> str:
        type(self).rendered = True
        return "Leaky(secret)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        type(self).rendered = True
        return "secret"


# ---------------------------------------------------------------- normalize


@pytest.mark.parametrize("value", ["", "plain", "ünïcödé", "a" * 5000])
def test_strings_are_unchanged(value: str) -> None:
    out = _normalize_value(value)
    assert out == value
    assert type(out) is str


@pytest.mark.parametrize("value", [True, False])
def test_bool_is_checked_before_int(value: bool) -> None:
    out = _normalize_value(value)
    assert type(out) is bool
    assert out is value


@pytest.mark.parametrize("value", [0, 1, -1, 42, INT64_MAX, INT64_MIN])
def test_int64_range_stays_int(value: int) -> None:
    out = _normalize_value(value)
    assert type(out) is int
    assert out == value


@pytest.mark.parametrize("value", [INT64_MAX + 1, INT64_MIN - 1, 10**30, -(10**30)])
def test_int_outside_int64_becomes_decimal_string(value: int) -> None:
    assert _normalize_value(value) == str(value)


def test_int64_boundaries_property() -> None:
    rng = random.Random(1234)
    for _ in range(500):
        value = rng.randint(-(1 << 70), 1 << 70)
        out = _normalize_value(value)
        if INT64_MIN <= value <= INT64_MAX:
            assert type(out) is int
            assert out == value
        else:
            assert out == str(value)


@pytest.mark.parametrize("value", [0.0, 1.5, -2.25, 1e300, 0.1])
def test_float_is_unchanged(value: float) -> None:
    out = _normalize_value(value)
    assert type(out) is float
    assert out == value


def test_bytes_are_unchanged() -> None:
    assert _normalize_value(b"\x00\xffabc") == b"\x00\xffabc"


@pytest.mark.parametrize(
    ("delta", "ms"),
    [
        (datetime.timedelta(seconds=5), 5000.0),
        (datetime.timedelta(milliseconds=250), 250.0),
        (datetime.timedelta(microseconds=1500), 1.5),
        (datetime.timedelta(0), 0.0),
        (datetime.timedelta(hours=1), 3_600_000.0),
    ],
)
def test_timedelta_becomes_float_milliseconds(delta: datetime.timedelta, ms: float) -> None:
    out = _normalize_value(delta)
    assert type(out) is float
    assert out == pytest.approx(ms)


@pytest.mark.parametrize(
    "moment",
    [
        datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        datetime.datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC),
        datetime.datetime(
            1999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))
        ),
    ],
)
def test_aware_datetime_becomes_rfc3339(moment: datetime.datetime) -> None:
    out = _normalize_value(moment)
    assert isinstance(out, str)
    assert RFC3339.match(out), out
    assert datetime.datetime.fromisoformat(out.replace("Z", "+00:00")) == moment


@pytest.mark.parametrize(
    ("seq", "expected"),
    [
        (["a", "b"], ["a", "b"]),
        (("a", "b"), ["a", "b"]),
        ([True, False], [True, False]),
        ([1, 2, 3], [1, 2, 3]),
        ((1.5, 2.5), [1.5, 2.5]),
    ],
)
def test_homogeneous_sequences_become_lists(seq: object, expected: list[object]) -> None:
    out = _normalize_value(seq)
    assert isinstance(out, list)
    assert out == expected
    assert [type(x) for x in out] == [type(x) for x in expected]


def test_bool_sequence_items_stay_bool() -> None:
    out = _normalize_value((True, False, True))
    assert isinstance(out, list)
    assert all(type(x) is bool for x in out)


@pytest.mark.parametrize(
    "seq",
    [
        ["a", 1],
        [1, True],
        [True, "x"],
        [["nested"]],
        [object()],
        ["a", None],
    ],
    ids=["str-int", "int-bool", "bool-str", "nested", "object", "none"],
)
def test_heterogeneous_or_nested_sequences_are_dropped(seq: list[object]) -> None:
    assert _normalize_value(seq) is None


@pytest.mark.parametrize(
    "value",
    [None, {"a": 1}, {1, 2}, frozenset({"a"}), object(), 1j, bytearray(b"x")],
    ids=["none", "dict", "set", "frozenset", "object", "complex", "bytearray"],
)
def test_unknown_values_are_dropped(value: object) -> None:
    assert _normalize_value(value) is None


def test_unknown_values_are_never_rendered() -> None:
    Leaky.rendered = False
    assert _normalize_value(Leaky()) is None
    assert _normalize_value([Leaky()]) is None
    assert _text_value(Leaky()) == attr.MISSING_LABEL
    assert not Leaky.rendered


def test_exceptions_are_not_plain_values() -> None:
    assert _normalize_value(ValueError("secret")) is None


def test_normalize_is_idempotent_property() -> None:
    rng = random.Random(42)
    pool: list[object] = [
        "s",
        True,
        7,
        -(1 << 64),
        2.5,
        b"b",
        datetime.timedelta(seconds=3),
        datetime.datetime(2020, 5, 6, tzinfo=UTC),
        ["x", "y"],
        (1, 2),
    ]
    for _ in range(200):
        value = rng.choice(pool)
        once = _normalize_value(value)
        assert once is not None
        assert _normalize_value(once) == once


# --------------------------------------------------------------------- text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("as-is", "as-is"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-17, "-17"),
        (1 << 70, str(1 << 70)),
        (0.1, "0.1"),
        (123.45, "123.45"),
        (datetime.timedelta(seconds=5), "5000ms"),
        (datetime.timedelta(milliseconds=250), "250ms"),
    ],
)
def test_text_rendering(value: object, text: str) -> None:
    assert _text_value(value) == text


def test_float_text_is_shortest_repr_property() -> None:
    rng = random.Random(7)
    for _ in range(300):
        value = rng.uniform(-1e6, 1e6)
        assert _text_value(value) == repr(value)


def test_bytes_text_is_base64() -> None:
    raw = bytes(range(32))
    assert _text_value(raw) == base64.b64encode(raw).decode("ascii")


def test_datetime_text_is_rfc3339() -> None:
    moment = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    text = _text_value(moment)
    assert RFC3339.match(text)
    assert datetime.datetime.fromisoformat(text.replace("Z", "+00:00")) == moment


@pytest.mark.parametrize(
    "value",
    [["a", "b"], (1, 2), [], ValueError("x"), None, {"k": "v"}, object()],
    ids=["list", "tuple", "empty", "exception", "none", "dict", "object"],
)
def test_sequences_and_unsupported_values_render_as_missing_label(value: object) -> None:
    assert _text_value(value) == attr.MISSING_LABEL


def test_missing_label_constant() -> None:
    assert attr.MISSING_LABEL == "_"


def test_error_key_constants_match_go() -> None:
    assert (
        attr.ERROR_KEY,
        attr.ERROR_TYPE,
        attr.ERROR_MESSAGE,
        attr.ERROR_FINGERPRINT,
        attr.ERROR_CHAIN,
        attr.ERROR_STACK,
        attr.ERROR_GROUP_COUNT,
        attr.ERROR_TYPES,
    ) == (
        "error",
        "error.type",
        "error.message",
        "error.fingerprint",
        "error.chain",
        "error.stack",
        "error.group.count",
        "error.types",
    )
