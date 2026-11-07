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
* anything else -> dropped, never ``str()``-ed (OTLP export reports it in
  ``droppedAttributesCount``; logs omit it; a metric label becomes ``"_"``)

Error attributes (same keys as Go), with their value types:

* ``error``: ``True`` (marks failure);
* ``error.type``: ``str``, the qualified class name (``builtins.ValueError``);
* ``error.message``: ``str``, only when the privacy policy allows error details;
* ``error.fingerprint``: ``str`` of 16 lowercase hex characters;
* ``error.chain``: ``list[str]`` of types along ``__cause__``/``__context__``;
* ``error.stack``: ``str``, only with ``stack=True`` and ``allow_source_paths``;
* exception groups: ``error.group.count`` (``int``) and ``error.types``
  (``list[str]``).

This is a leaf module: it imports only the :mod:`pyrock._compat` utilities.
"""

from __future__ import annotations

import base64
import collections
import datetime
from collections.abc import Iterator, Mapping, Sequence
from types import TracebackType
from typing import Any, TypeAlias

from pyrock._compat import is_nonempty_str

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
        if not is_nonempty_str(name):
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
        """A copy of the event attributes, as given (values are not yet normalized).

        Mutating the returned mapping does not change the event.
        """
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

    _data: dict[str, AttrValue]

    def __init__(
        self,
        items: Mapping[str, AttrValue] | None = None,
        /,
        **kwargs: AttrValue,
    ) -> None:
        self._data = dict(items) if items else {}
        if kwargs:
            self._data.update(kwargs)

    def set(self, key: str, value: AttrValue, /) -> None:
        """Set ``key`` (last write wins; position of the first write is kept)."""
        self._data[key] = value

    def update(self, items: Mapping[str, AttrValue] | None = None, /, **kwargs: AttrValue) -> None:
        """Merge ``items`` and ``kwargs`` into the set."""
        if items:
            self._data.update(items)
        if kwargs:
            self._data.update(kwargs)

    def merged(self, other: Mapping[str, AttrValue], /) -> Attrs:
        """Return a new set with ``other`` written on top of this one."""
        out = Attrs(self._data)
        out._data.update(other)
        return out

    def __getitem__(self, key: str) -> AttrValue:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data


# -- plain value normalization ----------------------------------------------

_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1

_Scalar: TypeAlias = str | bool | int | float


def _int_value(value: int) -> int | str | None:
    """An exact ``int`` inside int64, else its decimal text (``None`` if too long)."""
    if _INT64_MIN <= value <= _INT64_MAX:
        return value
    return _decimal(value)


def _decimal(value: int) -> str | None:
    try:
        return str(value)
    except ValueError:  # above sys.get_int_max_str_digits()
        return None


def _timedelta_us(value: datetime.timedelta) -> int:
    return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds


def _rfc3339(value: datetime.datetime) -> str:
    """RFC 3339 text; naive datetimes are local time (Python's convention)."""
    aware = value if value.tzinfo is not None else value.astimezone()
    if aware.utcoffset() == datetime.timedelta(0):
        return datetime.datetime.isoformat(aware.replace(tzinfo=None)) + "Z"
    return datetime.datetime.isoformat(aware)


def _coerce_subclass(value: object) -> object | None:
    """Reduce an instance of a supported type's subclass to the base type.

    Base-class methods are called explicitly so overridden ``__str__`` and
    friends never run. Exceptions and unsupported types give ``None``.
    """
    if isinstance(value, BaseException):
        return None
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):  # includes IntEnum/IntFlag; bool is final
        return int.__int__(value)
    if isinstance(value, float):
        return float.__float__(value)
    if isinstance(value, bytes):
        return bytes.__getitem__(value, slice(None))
    if isinstance(value, datetime.datetime | datetime.timedelta):
        return value
    if isinstance(value, list | tuple):
        return list(value)
    return None


def _normalize_scalar(value: object) -> _Scalar | None:
    """Normalize one sequence item (only ``str``/``bool``/``int``/``float``)."""
    tp = type(value)
    if tp is str or tp is bool or tp is float:
        return value  # type: ignore[return-value]
    if tp is int:
        return _int_value(value)  # type: ignore[arg-type]
    coerced = _coerce_subclass(value)
    if type(coerced) in (str, int, float):
        return _normalize_scalar(coerced)
    return None


def _normalize_sequence(items: Sequence[object]) -> AttrValue | None:
    """A ``list`` of normalized items if they all share one type, else ``None``."""
    out: list[_Scalar] = []
    kind: type | None = None
    for item in items:
        normalized = _normalize_scalar(item)
        if normalized is None or (kind is not None and type(normalized) is not kind):
            return None
        kind = type(normalized)
        out.append(normalized)
    return out  # type: ignore[return-value]  # homogeneous, so a list[str], list[int], ...


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

    Subclasses of the supported types are reduced to the base type first.
    """
    tp = type(value)
    if tp is str or tp is bool or tp is float or tp is bytes:
        return value  # type: ignore[return-value]
    if tp is int:
        return _int_value(value)  # type: ignore[arg-type]
    if tp is list or tp is tuple:
        return _normalize_sequence(value)  # type: ignore[arg-type]
    if tp is datetime.timedelta:
        return _timedelta_us(value) / 1000  # type: ignore[arg-type]
    if tp is datetime.datetime:
        return _rfc3339(value)  # type: ignore[arg-type]
    coerced = _coerce_subclass(value)
    return None if coerced is None else _normalize_value(coerced)


def _text_value(value: object, /) -> str:
    """Render a plain attribute value as Prometheus label / log text.

    Private seam. ``str`` as-is; ``bool`` -> ``"true"``/``"false"``; ``int`` ->
    decimal; ``float`` -> shortest ``repr`` form; ``timedelta`` -> milliseconds
    with an ``"ms"`` suffix (``"5000ms"``); ``datetime`` -> RFC 3339; ``bytes``
    -> standard base64. Sequences, exceptions and unsupported values render as
    :data:`MISSING_LABEL` (``"_"``), never via ``str()``/``repr()``.
    """
    tp = type(value)
    if tp is str:
        return value  # type: ignore[return-value]
    if tp is bool:
        return "true" if value else "false"
    if tp is int:
        return _decimal(value) or MISSING_LABEL  # type: ignore[arg-type]
    if tp is float:
        return repr(value)
    if tp is datetime.timedelta:
        micros = _timedelta_us(value)  # type: ignore[arg-type]
        millis = micros // 1000 if micros % 1000 == 0 else micros / 1000
        return f"{millis}ms"
    if tp is datetime.datetime:
        return _rfc3339(value)  # type: ignore[arg-type]
    if tp is bytes:
        return base64.b64encode(value).decode("ascii")  # type: ignore[arg-type]
    if tp is list or tp is tuple:
        return MISSING_LABEL
    coerced = _coerce_subclass(value)
    return MISSING_LABEL if coerced is None else _text_value(coerced)


# -- errors ----------------------------------------------------------------

_MAX_CHAIN = 32
"""Longest ``error.chain`` emitted; longer chains are cut (cycles end earlier)."""


def qualified_name(tp: type[BaseException], /) -> str:
    """Return ``module.QualName`` for an exception type (``builtins.ValueError``)."""
    return f"{tp.__module__}.{tp.__qualname__}"


def _tracebacks(exc: BaseException) -> Iterator[TracebackType]:
    tb = exc.__traceback__
    while tb is not None:
        yield tb
        tb = tb.tb_next


def _frame_id(tb: TracebackType) -> str:
    """``module:qualname`` of a traceback entry (no file path, no line)."""
    frame = tb.tb_frame
    module = frame.f_globals.get("__name__")
    return f"{module if isinstance(module, str) else '?'}:{frame.f_code.co_qualname}"


def fingerprint(exc: BaseException, /, *, frames: int = 8) -> str:
    """Return a stable hash of the type and the top ``frames`` frames' module/qualname.

    The result is 16 lowercase hex characters. The top frames are the
    innermost ones, where the exception was raised.

    Line numbers are excluded so the fingerprint survives unrelated edits.
    """
    import hashlib  # deferred: only error paths need it (keeps ``import pyrock`` fast)

    top = collections.deque(_tracebacks(exc), maxlen=max(frames, 0))
    digest = hashlib.blake2b(qualified_name(type(exc)).encode(), digest_size=8)
    for tb in top:
        digest.update(b"\x00")
        digest.update(_frame_id(tb).encode())
    return digest.hexdigest()


def _chain(exc: BaseException) -> list[str]:
    """Types along ``__cause__``/``__context__``, outermost first, cycle-safe."""
    seen = {id(exc)}
    chain = [qualified_name(type(exc))]
    current: BaseException | None = exc
    while current is not None and len(chain) < _MAX_CHAIN:
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        chain.append(qualified_name(type(current)))
    return chain


def _group_leaves(group: BaseExceptionGroup[BaseException]) -> list[str]:
    """Qualified types of the leaf exceptions of a (nested) group, in order."""
    types: list[str] = []
    pending: list[BaseException] = [group]
    while pending:
        exc = pending.pop()
        if isinstance(exc, BaseExceptionGroup):
            pending.extend(reversed(exc.exceptions))
        else:
            types.append(qualified_name(type(exc)))
    return types


def _message(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return ""


def _stack(exc: BaseException, *, source_paths: bool, details: bool) -> str:
    """Format the traceback, most recent call last, never with source lines.

    With ``source_paths`` each frame shows its file and line; otherwise only
    ``module:function``. The final line carries the message only with
    ``details``.
    """
    lines = ["Traceback (most recent call last):"]
    for tb in _tracebacks(exc):
        if source_paths:
            code = tb.tb_frame.f_code
            lines.append(f'  File "{code.co_filename}", line {tb.tb_lineno}, in {code.co_qualname}')
        else:
            lines.append(f"  {_frame_id(tb)}")
    last = qualified_name(type(exc))
    lines.append(f"{last}: {_message(exc)}" if details else last)
    return "\n".join(lines)


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
    policy's flags. ``error.chain`` is present only when ``exc`` wraps another
    exception.
    """
    out: dict[str, AttrValue] = {ERROR_KEY: True, ERROR_TYPE: qualified_name(type(exc))}
    if allow_details:
        out[ERROR_MESSAGE] = _message(exc)
    out[ERROR_FINGERPRINT] = fingerprint(exc)
    chain = _chain(exc)
    if len(chain) > 1:
        out[ERROR_CHAIN] = chain
    if isinstance(exc, BaseExceptionGroup):
        leaves = _group_leaves(exc)
        out[ERROR_GROUP_COUNT] = len(leaves)
        out[ERROR_TYPES] = leaves
    if stack:
        out[ERROR_STACK] = _stack(exc, source_paths=allow_source_paths, details=allow_details)
    return out
