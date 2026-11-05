"""Attribute values, events, error normalization and ordered attribute sets.

pyrock uses plain Python values as attributes (``{"user_id": "123", "n": 3}``).
This module covers what plain values cannot express:

* :class:`Event`: a named span event usable with ``register()``
  (Go: ``attr.NewEvent``).
* :func:`error`: explicit error attributes (Go: ``attr.Error`` /
  ``attr.ErrorWithStack``).
* :class:`Attrs`: an ordered, last-write-wins attribute set (Go: ``attr.Set``).

Type mapping (OTLP / Prometheus label / log):

* ``str`` -> ``stringValue`` / as-is
* ``bool`` -> ``boolValue`` / ``"true"``/``"false"`` (checked before ``int``)
* ``int`` -> ``intValue`` (outside int64 -> string) / decimal
* ``float`` -> ``doubleValue`` / shortest ``repr`` form
* ``timedelta`` -> ``doubleValue`` in ms / ``"5000ms"`` in logs
* ``datetime`` -> RFC 3339 string
* ``bytes`` -> ``bytesValue`` / base64
* sequences -> ``arrayValue`` / JSON array; never a metric label (``"_"``)
* ``BaseException`` -> the error attributes below
* anything else -> dropped and counted (never ``str()``-ed)

Error attributes (same keys as Go), with their value types:

* ``error``: ``True`` (marks failure);
* ``error.type``: ``str``, the qualified class name (``builtins.ValueError``);
* ``error.message``: ``str``, only when the privacy policy allows error details;
* ``error.fingerprint``: ``str`` of 16 lowercase hex characters;
* ``error.chain``: ``list[str]`` of types along ``__cause__``/``__context__``;
* ``error.stack``: ``str``, only with ``stack=True`` and ``allow_source_paths``;
* exception groups: ``error.group.count`` (``int``) and ``error.types``
  (``list[str]``).

This is a leaf module: it imports nothing from pyrock.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, TypeAlias

__all__ = [
    "ERROR_CHAIN",
    "ERROR_FINGERPRINT",
    "ERROR_GROUP_COUNT",
    "ERROR_KEY",
    "ERROR_MESSAGE",
    "ERROR_STACK",
    "ERROR_TYPE",
    "ERROR_TYPES",
    "MISSING_LABEL",
    "AttrValue",
    "Attrs",
    "Event",
    "error",
    "fingerprint",
    "qualified_name",
]

AttrValue: TypeAlias = (
    str
    | bool
    | int
    | float
    | bytes
    | datetime.timedelta
    | datetime.datetime
    | BaseException
    | Sequence[str]
    | Sequence[bool]
    | Sequence[int]
    | Sequence[float]
)
"""Every value pyrock accepts as an attribute."""

ERROR_KEY = "error"
ERROR_TYPE = "error.type"
ERROR_MESSAGE = "error.message"
ERROR_FINGERPRINT = "error.fingerprint"
ERROR_CHAIN = "error.chain"
ERROR_STACK = "error.stack"
ERROR_GROUP_COUNT = "error.group.count"
ERROR_TYPES = "error.types"

MISSING_LABEL = "_"
"""Value used for a declared metric label that was never registered."""


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


class Event:
    """A named event, recorded on the span when registered.

    ``Event("retry", attempt=2)`` or ``Event("retry", {"retry.attempt": 2})``.
    Events are immutable value objects.
    """

    __slots__ = ("_attrs", "_name")

    _name: str
    _attrs: Mapping[str, AttrValue]

    def __init__(
        self,
        name: str,
        attrs: Mapping[str, AttrValue] | None = None,
        /,
        **kwargs: AttrValue,
    ) -> None:
        if not _is_nonempty_str(name):
            raise ValueError("event name must be a non-empty string")
        merged: dict[str, AttrValue] = dict(attrs) if attrs else {}
        merged.update(kwargs)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_attrs", merged)

    @property
    def name(self) -> str:
        """The event name."""
        return self._name

    @property
    def attrs(self) -> Mapping[str, AttrValue]:
        """A read-only view of the event attributes."""
        return dict(self._attrs)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Event is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Event):
            return NotImplemented
        return self._name == other._name and dict(self._attrs) == dict(other._attrs)

    def __hash__(self) -> int:
        return hash((self._name, frozenset(self._attrs)))

    def __repr__(self) -> str:
        return f"Event({self._name!r}, keys={list(self._attrs)!r})"


class Attrs(Mapping[str, AttrValue]):
    """An ordered attribute set with last-write-wins semantics (Go ``attr.Set``).

    ``Attrs({"a": 1}, b=2)``. Keys keep their first-insertion position; a later
    write replaces the value in place.
    """

    __slots__ = ("_data",)

    def __init__(
        self,
        items: Mapping[str, AttrValue] | None = None,
        /,
        **kwargs: AttrValue,
    ) -> None:
        raise NotImplementedError

    def set(self, key: str, value: AttrValue, /) -> None:
        """Set ``key`` (last write wins; position of the first write is kept)."""
        raise NotImplementedError

    def update(self, items: Mapping[str, AttrValue] | None = None, /, **kwargs: AttrValue) -> None:
        """Merge ``items`` and ``kwargs`` into the set."""
        raise NotImplementedError

    def merged(self, other: Mapping[str, AttrValue], /) -> Attrs:
        """Return a new set with ``other`` written on top of this one."""
        raise NotImplementedError

    def __getitem__(self, key: str) -> AttrValue:
        raise NotImplementedError

    def __iter__(self) -> Iterator[str]:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError


def _normalize_value(value: object, /) -> AttrValue | None:
    """Return the canonical, export-ready form of a plain attribute value.

    Private seam shared by the OTLP encoders, the log handler and the metric
    label resolver. ``None`` means "drop this value" (the caller counts it).

    * ``bool`` stays ``bool`` (checked before ``int``);
    * ``int`` stays ``int`` inside the signed 64-bit range, otherwise it becomes
      its decimal ``str``;
    * ``str``, ``float`` and ``bytes`` are returned unchanged;
    * ``timedelta`` becomes ``float`` milliseconds;
    * ``datetime`` becomes an RFC 3339 ``str``;
    * a ``list``/``tuple`` whose items are all ``str``, all ``bool``, all
      ``int`` or all ``float`` becomes a ``list`` of the normalized items;
      mixed, nested or other sequences are dropped;
    * ``BaseException`` returns ``None`` here (callers expand it with
      :func:`error`);
    * anything else (``None``, sets, dicts, arbitrary objects) returns ``None``
      without calling ``str()``/``repr()`` on it.
    """
    raise NotImplementedError


def _text_value(value: object, /) -> str:
    """Render a plain attribute value as Prometheus label / log text.

    Private seam. ``str`` as-is; ``bool`` -> ``"true"``/``"false"``; ``int`` ->
    decimal; ``float`` -> shortest ``repr`` form; ``timedelta`` -> milliseconds
    with an ``"ms"`` suffix (``"5000ms"``); ``datetime`` -> RFC 3339; ``bytes``
    -> standard base64. Sequences, exceptions and unsupported values render as
    :data:`MISSING_LABEL` (``"_"``), never via ``str()``/``repr()``.
    """
    raise NotImplementedError


def qualified_name(tp: type[BaseException], /) -> str:
    """Return ``module.QualName`` for an exception type (``builtins.ValueError``)."""
    raise NotImplementedError


def fingerprint(exc: BaseException, /, *, frames: int = 8) -> str:
    """Return a stable hash of the type and the top ``frames`` frames' module/qualname.

    The result is 16 lowercase hex characters.

    Line numbers are excluded so the fingerprint survives unrelated edits.
    """
    raise NotImplementedError


def error(
    exc: BaseException,
    /,
    *,
    stack: bool = False,
    allow_details: bool = False,
    allow_source_paths: bool = False,
) -> dict[str, AttrValue]:
    """Return the normalized error attributes for ``exc``.

    ``allow_details`` gates ``error.message``; ``stack`` together with
    ``allow_source_paths`` adds a full ``error.stack`` (otherwise frames are
    trimmed to ``module:function``). Callers normally pass the active privacy
    policy's flags.
    """
    raise NotImplementedError
