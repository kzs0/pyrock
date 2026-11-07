"""``privacy.Policy`` as a data type: defaults, normalization, validation, constants."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from pyrock import privacy
from pyrock.privacy import Policy


def test_defaults() -> None:
    p = Policy()
    assert p.attribute_keys is None
    assert p.resource_keys == ()
    assert p.allow_error_details is False
    assert p.allow_source_paths is False
    assert p.allow_profile_labels is False
    assert p.max_string_length == privacy.DEFAULT_MAX_STRING_LENGTH == 1024
    assert p.redact is None


def test_constants() -> None:
    assert privacy.MAX_NODES == 4096
    assert privacy.CYCLE_MARKER == "[cycle]"
    assert privacy.ALIAS_MARKER == "[alias]"
    assert privacy.MAX_PROFILE_LABELS == 64
    assert privacy.MAX_PROFILE_LABEL_DEPTH == 8


def test_key_lists_are_stored_as_tuples() -> None:
    p = Policy(attribute_keys=["a", "b.*"], resource_keys=["host.name"])  # type: ignore[arg-type]
    assert p.attribute_keys == ("a", "b.*")
    assert p.resource_keys == ("host.name",)


def test_key_iterables_are_accepted() -> None:
    p = Policy(attribute_keys=(k for k in ["x", "y"]))  # type: ignore[arg-type]
    assert p.attribute_keys == ("x", "y")


def test_empty_attribute_allowlist_is_kept_distinct_from_none() -> None:
    assert Policy(attribute_keys=[]).attribute_keys == ()  # type: ignore[arg-type]


def test_input_list_mutation_does_not_affect_policy() -> None:
    keys = ["a"]
    p = Policy(attribute_keys=keys)  # type: ignore[arg-type]
    keys.append("b")
    assert p.attribute_keys == ("a",)


@pytest.mark.parametrize("field", ["attribute_keys", "resource_keys"])
def test_single_string_is_rejected(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        Policy(**{field: "host.name"})


@pytest.mark.parametrize("field", ["attribute_keys", "resource_keys"])
@pytest.mark.parametrize("bad", [[""], [1], [None], ["ok", ""]])
def test_bad_key_entries_are_rejected(field: str, bad: list[Any]) -> None:
    with pytest.raises(TypeError, match=field):
        Policy(**{field: bad})


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10", None])
def test_max_string_length_must_be_positive_int(bad: Any) -> None:
    with pytest.raises(ValueError, match="max_string_length"):
        Policy(max_string_length=bad)


def test_redact_must_be_callable() -> None:
    with pytest.raises(TypeError, match="redact"):
        Policy(redact="nope")  # type: ignore[arg-type]


def test_policy_is_frozen() -> None:
    p = Policy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.allow_error_details = True  # type: ignore[misc]


def test_policy_uses_slots() -> None:
    assert not hasattr(Policy(), "__dict__")


def test_policy_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        Policy(("a",))  # type: ignore[misc]


def test_equality_ignores_redact_and_is_hashable() -> None:
    def r1(path: str, value: Any) -> tuple[Any, bool]:
        return value, True

    def r2(path: str, value: Any) -> tuple[Any, bool]:
        return None, False

    a = Policy(attribute_keys=["x"], redact=r1)  # type: ignore[arg-type]
    b = Policy(attribute_keys=["x"], redact=r2)  # type: ignore[arg-type]
    assert a == b
    assert hash(a) == hash(b)
    assert a != Policy(attribute_keys=["y"])  # type: ignore[arg-type]
    assert Policy() != Policy(allow_error_details=True)


def test_replace_revalidates() -> None:
    with pytest.raises(ValueError, match="max_string_length"):
        dataclasses.replace(Policy(), max_string_length=0)
