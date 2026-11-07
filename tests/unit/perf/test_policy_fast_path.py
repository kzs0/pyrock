"""The flat-scalar fast path of ``Policy.apply`` / ``Policy.value`` must give
exactly what the full walker gives (FINDINGS P1-11a).

Random attribute dicts mix the cases the fast path must hand back to the
walker (path rules, URLs, long strings, containers, subclasses, odd keys) with
the plain scalars it handles itself.
"""

from __future__ import annotations

import datetime
import enum
import random
from typing import Any

import pytest

from pyrock.privacy import _DROP, MAX_NODES, Policy, _Match, _Walk


class _Str(str):
    def __str__(self) -> str:  # must never be called
        raise AssertionError("__str__ called")


class _Int(enum.IntEnum):
    A = 7


class _Opaque:
    pass


_KEYS = [
    "user.id",
    "http.method",
    "http.route",
    "http.status_code",
    "count",
    "password",
    "db.password",
    "api_key",
    "token",
    "auth.token.value",
    "Authorization",
    "error",
    "error.type",
    "error.message",
    "error.stack",
    "exception.stacktrace",
    "code.filepath",
    "http.url",
    "http.target",
    "url.full",
    "http.user_agent",
    "host.name",
    "a.b.c",
    "",
]

_NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)


def _value(rng: random.Random) -> Any:
    choice = rng.randrange(20)
    if choice < 4:
        return rng.choice(["x", "GET", "", "/users/{id}", "https://u:p@h/p?q=1#f"])
    if choice == 4:
        return "y" * rng.choice([7, 8, 9, 2000])
    if choice == 5:
        return rng.randrange(-(10**20), 10**20)
    if choice == 6:
        return rng.random()
    if choice == 7:
        return rng.random() < 0.5
    if choice == 8:
        return None
    if choice == 9:
        return b"z" * rng.choice([3, 8, 9])
    if choice == 10:
        return _NOW
    if choice == 11:
        return datetime.timedelta(milliseconds=rng.randrange(10_000))
    if choice == 12:
        return _Str("sub")
    if choice == 13:
        return _Int.A
    if choice == 14:
        return {"inner": "v", "password": "p"}
    if choice == 15:
        return ["a", "b"]
    if choice == 16:
        return ("a", 1)
    if choice == 17:
        return _Opaque()
    if choice == 18:
        return ValueError("boom")
    return float("nan")


def _key(rng: random.Random) -> Any:
    choice = rng.randrange(12)
    if choice == 0:
        return f"k{rng.randrange(50)}"
    if choice == 1:
        return _Str("sub.key")
    if choice == 2 and rng.random() < 0.3:
        return 3  # a non-string key: suppressed
    return rng.choice(_KEYS)


def _attrs(rng: random.Random) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    flat = rng.random() < 0.5  # half the cases stay on the fast path
    for _ in range(rng.randrange(10)):
        if flat:
            out[f"k{rng.randrange(30)}"] = rng.choice(["a", 1, 2.5, True, None, b"b", _NOW])
        else:
            out[_key(rng)] = _value(rng)
    return out


_POLICIES = [
    Policy(),
    Policy(max_string_length=8),
    Policy(allow_error_details=True),
    Policy(allow_source_paths=True, allow_error_details=True),
    Policy(resource_keys=("host.name",)),
]


def _walker(policy: Policy, attrs: dict[Any, Any]) -> dict[str, Any]:
    return _Walk(policy).top_level("", attrs, _Match.FULL)


def _same(a: Any, b: Any) -> bool:
    """Equality that treats NaN as equal to NaN and checks exact types."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) and a != a:
        return bool(b != b)
    equal: bool = a == b
    return equal


@pytest.mark.parametrize("seed", range(40))
def test_apply_matches_the_walker(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(50):
        attrs = _attrs(rng)
        for policy in _POLICIES:
            fast = policy.apply(attrs)
            slow = _walker(policy, attrs)
            assert _same(fast, slow), (policy, attrs, fast, slow)


@pytest.mark.parametrize("seed", range(20))
def test_value_matches_the_walker(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(100):
        key = rng.choice(_KEYS) or "k"
        value = _value(rng)
        for policy in _POLICIES:
            slow = _Walk(policy).value(key, value, policy._root_mode(key))
            got = policy.value(key, value)
            if slow is _DROP:
                assert got == (None, False)
            else:
                assert got[1] is True
                assert _same(got[0], slow), (policy, key, value, got, slow)


def test_apply_keeps_order_and_does_not_alias_the_input() -> None:
    attrs = {"b": 1, "a": "x", "c": None}
    out = Policy().apply(attrs)
    assert list(out) == ["b", "a", "c"]
    assert out == attrs
    assert out is not attrs
    out["d"] = 1
    assert "d" not in attrs


def test_apply_truncates_at_the_node_budget_like_the_walker() -> None:
    attrs = {f"k{i}": i for i in range(MAX_NODES + 5)}
    out = Policy().apply(attrs)
    assert out == _walker(Policy(), attrs)
    assert len(out) == MAX_NODES


def test_the_allowlist_and_redact_hook_bypass_the_fast_path() -> None:
    seen: list[str] = []

    def redact(path: str, value: Any) -> tuple[Any, bool]:
        seen.append(path)
        return value, True

    assert Policy(redact=redact).apply({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}
    assert seen == ["a", "b"]
    assert Policy(attribute_keys=("a",)).apply({"a": 1, "b": "x"}) == {"a": 1}
    assert Policy(attribute_keys=("a",)).value("b", 1) == (None, False)


def test_a_qualified_path_uses_the_walker() -> None:
    policy = Policy()
    assert policy.apply({"password": "x"}, path="db") == {"password": "[redacted]"}
    assert policy.apply({"k": "v"}, path="error") == {"k": "[redacted]"}
