"""``attr.Attrs`` (ordered, last-write-wins set) and ``attr.Event``."""

from __future__ import annotations

import random
from collections.abc import Mapping

import pytest

from pyrock.attr import Attrs, Event

# -------------------------------------------------------------------- Attrs


def test_attrs_is_a_mapping() -> None:
    attrs = Attrs({"a": 1}, b=2)
    assert isinstance(attrs, Mapping)
    assert attrs["a"] == 1
    assert attrs["b"] == 2
    assert len(attrs) == 2
    assert "a" in attrs
    assert "z" not in attrs
    assert attrs.get("z") is None


def test_attrs_empty() -> None:
    attrs = Attrs()
    assert len(attrs) == 0
    assert list(attrs) == []
    with pytest.raises(KeyError):
        attrs["missing"]


def test_attrs_keeps_insertion_order() -> None:
    attrs = Attrs({"z": 1, "a": 2, "m": 3})
    assert list(attrs) == ["z", "a", "m"]


def test_kwargs_follow_positional_items() -> None:
    attrs = Attrs({"x": 1}, y=2, z=3)
    assert list(attrs.items()) == [("x", 1), ("y", 2), ("z", 3)]


def test_kwargs_override_positional_items_in_place() -> None:
    attrs = Attrs({"a": 1, "b": 2}, a=10)
    assert list(attrs.items()) == [("a", 10), ("b", 2)]


def test_set_last_write_wins_and_keeps_first_position() -> None:
    attrs = Attrs({"a": 1, "b": 2, "c": 3})
    attrs.set("a", 100)
    attrs.set("d", 4)
    assert list(attrs.items()) == [("a", 100), ("b", 2), ("c", 3), ("d", 4)]


def test_update_merges_mapping_and_kwargs() -> None:
    attrs = Attrs({"a": 1})
    attrs.update({"b": 2, "a": 3}, c=4)
    assert list(attrs.items()) == [("a", 3), ("b", 2), ("c", 4)]


def test_update_with_nothing_is_a_noop() -> None:
    attrs = Attrs({"a": 1})
    attrs.update()
    attrs.update(None)
    assert dict(attrs) == {"a": 1}


def test_merged_returns_new_set_without_mutating() -> None:
    base = Attrs({"a": 1, "b": 2})
    other = {"b": 20, "c": 30}
    out = base.merged(other)
    assert isinstance(out, Attrs)
    assert out is not base
    assert list(out.items()) == [("a", 1), ("b", 20), ("c", 30)]
    assert list(base.items()) == [("a", 1), ("b", 2)]
    assert other == {"b": 20, "c": 30}


def test_merged_accepts_attrs() -> None:
    out = Attrs({"a": 1}).merged(Attrs({"a": 2, "b": 3}))
    assert list(out.items()) == [("a", 2), ("b", 3)]


def test_constructor_copies_input() -> None:
    source = {"a": 1}
    attrs = Attrs(source)
    source["a"] = 2
    source["b"] = 3
    assert dict(attrs) == {"a": 1}


def test_merged_copy_is_independent() -> None:
    base = Attrs({"a": 1})
    out = base.merged({})
    out.set("a", 2)
    assert base["a"] == 1


def test_equality_with_mappings() -> None:
    assert Attrs({"a": 1}, b=2) == {"a": 1, "b": 2}
    assert Attrs({"a": 1}) != {"a": 2}


def test_last_write_wins_property() -> None:
    """Against a dict model: dicts also keep first position on overwrite."""
    rng = random.Random(2024)
    keys = [f"k{i}" for i in range(12)]
    for _ in range(50):
        model: dict[str, int] = {}
        attrs = Attrs()
        for step in range(40):
            op = rng.randrange(3)
            if op == 0:
                key = rng.choice(keys)
                attrs.set(key, step)
                model[key] = step
            elif op == 1:
                batch = {rng.choice(keys): step for _ in range(rng.randrange(1, 4))}
                attrs.update(batch)
                model.update(batch)
            else:
                batch = {rng.choice(keys): -step for _ in range(rng.randrange(1, 4))}
                merged = attrs.merged(batch)
                expected = {**model, **batch}
                assert list(merged.items()) == list(expected.items())
        assert list(attrs.items()) == list(model.items())
        assert len(attrs) == len(model)


# -------------------------------------------------------------------- Event


def test_event_name_and_attrs() -> None:
    event = Event("retry", {"retry.attempt": 2}, backoff=1.5)
    assert event.name == "retry"
    assert dict(event.attrs) == {"retry.attempt": 2, "backoff": 1.5}


def test_event_kwargs_override_mapping() -> None:
    assert dict(Event("e", {"a": 1}, a=2).attrs) == {"a": 2}


def test_event_without_attrs() -> None:
    event = Event("tick")
    assert dict(event.attrs) == {}


@pytest.mark.parametrize("name", ["", None, 3])
def test_event_requires_non_empty_string_name(name: object) -> None:
    with pytest.raises(ValueError, match="name"):
        Event(name)  # type: ignore[arg-type]


def test_event_is_immutable() -> None:
    event = Event("e", a=1)
    with pytest.raises(AttributeError):
        event.name = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        event.extra = 1  # type: ignore[attr-defined]


def test_event_attrs_view_cannot_mutate_event() -> None:
    event = Event("e", a=1)
    view = event.attrs
    if isinstance(view, dict):
        view["a"] = 99
        view["b"] = 2
    assert dict(event.attrs) == {"a": 1}


def test_event_copies_input_mapping() -> None:
    source: dict[str, int] = {"a": 1}
    event = Event("e", source)
    source["a"] = 2
    assert dict(event.attrs) == {"a": 1}


def test_event_equality_and_hash() -> None:
    a = Event("e", {"x": 1})
    b = Event("e", x=1)
    assert a == b
    assert hash(a) == hash(b)
    assert a != Event("e", x=2)
    assert a != Event("f", x=1)
    assert a != "e"
    assert len({a, b}) == 1


def test_event_repr_does_not_render_values() -> None:
    event = Event("login", password="hunter2")
    text = repr(event)
    assert "login" in text
    assert "hunter2" not in text
