"""Typed environment-variable parsing for dataclasses (Go: ``env.Parse[T]()``).

.. code-block:: python

    @dataclass
    class AppConfig:
        bedrock: pyrock.Config = field(default_factory=pyrock.Config.from_env)
        port: Annotated[int, Env("PORT")] = 8080
        database_url: Annotated[str, Env("DATABASE_URL", required=True)] = ""
        allowed_hosts: Annotated[list[str], Env("ALLOWED_HOSTS", sep=",")] = field(
            default_factory=list
        )

    cfg = parse(AppConfig)  # raises ParseError listing every failure

Supported field types: ``str``, ``int``, ``float``, ``bool``
(``1/0/true/false/yes/no/on/off``), ``timedelta`` (Go duration syntax),
``list[T]`` / ``tuple[T, ...]``, ``Literal[...]``, ``enum.Enum``,
``Optional[T]`` and nested dataclasses.

This is a leaf module: it imports nothing from pyrock.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import os
import re
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = ["Env", "ParseError", "parse", "parse_bool", "parse_duration"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Env:
    """``Annotated`` metadata naming the environment variable for a field.

    ``name`` is the variable; ``required`` makes a missing or empty variable an
    error; ``sep`` splits list/tuple values (whitespace around items is
    stripped).
    """

    name: str
    required: bool = False
    sep: str = ","

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Env name must be non-empty")
        if not self.sep:
            raise ValueError("Env sep must be non-empty")


class ParseError(ValueError):
    """Raised by :func:`parse` with every problem found, not just the first."""

    problems: list[str]

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) or "environment parse error")


# -- scalar parsers ----------------------------------------------------------

_NS_PER_UNIT = {
    "ns": 1,
    "us": 1_000,
    "µs": 1_000,  # micro sign
    "μs": 1_000,  # Greek mu
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}
_NUMBER = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_UNIT = "|".join(sorted(map(re.escape, _NS_PER_UNIT), key=len, reverse=True))
_BARE_SECONDS = re.compile(_NUMBER)
_DURATION = re.compile(f"(?:{_NUMBER}(?:{_UNIT}))+")
_DURATION_PART = re.compile(f"([0-9]*)(?:\\.([0-9]*))?({_UNIT})")
_INT = re.compile(r"[+-]?[0-9]+")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def parse_duration(value: str, /) -> float:
    """Parse a Go duration (``10s``, ``500ms``, ``1m30s``, ``1h``) into seconds.

    A bare number is read as seconds. Units: ``ns``, ``us``/``µs``, ``ms``,
    ``s``, ``m``, ``h``. Raises ``ValueError`` on malformed input.

    Like Go, one leading sign is allowed and units may repeat in any order;
    exponents, ``nan`` and ``inf`` are rejected. Fractions are summed exactly.
    """
    sign, body = (-1, value[1:]) if value[:1] == "-" else (1, value.removeprefix("+"))
    if _BARE_SECONDS.fullmatch(body):
        return sign * float(body)
    if not _DURATION.fullmatch(body):
        raise ValueError("invalid duration (expected Go syntax such as 10s, 500ms or 1m30s)")
    # Sum in integer units of 10**-places ns; int / int division rounds once.
    parts = _DURATION_PART.findall(body)
    places = max(len(frac) for _, frac, _ in parts)
    total: int = sum(
        int(whole + frac or "0") * _NS_PER_UNIT[unit] * 10 ** (places - len(frac))
        for whole, frac, unit in parts
    )
    denominator: int = 10**places * 1_000_000_000
    try:
        return sign * (total / denominator)
    except OverflowError:
        raise ValueError("duration out of range") from None


def parse_bool(value: str, /) -> bool:
    """Parse ``1/0/true/false/yes/no/on/off`` (case-insensitive)."""
    folded = value.lower()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise ValueError("expected a boolean (1/0, true/false, yes/no, on/off)")


def _parse_int(value: str, /) -> int:
    """Parse a decimal integer with an optional sign (no spaces, no underscores)."""
    if not _INT.fullmatch(value):
        raise ValueError("expected an integer")
    return int(value)


def _parse_float(value: str, /) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError("expected a number") from None


def _split(value: str, sep: str) -> list[str]:
    return [item.strip() for item in value.split(sep)]


# -- type-directed conversion ------------------------------------------------

_SCALARS: Mapping[type, Callable[[str], Any]] = {
    str: str,
    bool: parse_bool,
    int: _parse_int,
    float: _parse_float,
    datetime.timedelta: lambda raw: datetime.timedelta(seconds=parse_duration(raw)),
}


def _optional_inner(tp: Any) -> Any:
    """``T`` for ``T | None`` / ``Optional[T]``; ``tp`` itself otherwise."""
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(tp) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _convert(tp: Any, raw: str, sep: str) -> Any:
    """Convert ``raw`` to ``tp``; ``ValueError`` for bad input, ``TypeError`` for bad types."""
    tp = _optional_inner(tp)
    scalar = _SCALARS.get(tp)
    if scalar is not None:
        return scalar(raw)
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    homogeneous_tuple = origin is tuple and len(args) == 2 and args[1] is Ellipsis
    if (origin is list and len(args) == 1) or homogeneous_tuple:
        items = [_convert_item(args[0], i, item) for i, item in enumerate(_split(raw, sep))]
        return tuple(items) if homogeneous_tuple else items
    if origin is typing.Literal:
        return _convert_literal(args, raw)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return _convert_enum(tp, raw)
    raise TypeError(f"pyrock.env.parse does not support fields of type {tp!r}")


def _convert_item(tp: Any, index: int, raw: str) -> Any:
    try:
        return _convert(tp, raw, ",")
    except ValueError as exc:
        raise ValueError(f"item {index + 1}: {exc}") from None


def _convert_literal(choices: tuple[Any, ...], raw: str) -> Any:
    for choice in choices:
        if choice == raw or (not isinstance(choice, str) and _matches(type(choice), raw, choice)):
            return choice
    raise ValueError("expected one of " + ", ".join(map(repr, choices)))


def _matches(tp: type, raw: str, expected: object) -> bool:
    try:
        return bool(_convert(tp, raw, ",") == expected)
    except (TypeError, ValueError):
        return False


def _convert_enum(tp: type[enum.Enum], raw: str) -> enum.Enum:
    for member in tp:
        if member.value == raw or (
            not isinstance(member.value, str) and _matches(type(member.value), raw, member.value)
        ):
            return member
    by_name = tp.__members__.get(raw)
    if by_name is None:
        raise ValueError("expected one of " + ", ".join(repr(m.value) for m in tp))
    return by_name


# -- dataclasses ---------------------------------------------------------------


def _env_marker(hint: Any) -> tuple[Env | None, Any]:
    """Split ``Annotated[T, ..., Env(...)]`` into the marker and ``T``."""
    if typing.get_origin(hint) is typing.Annotated:
        tp, *metadata = typing.get_args(hint)
        for item in metadata:
            if isinstance(item, Env):
                return item, tp
        return None, tp
    return None, hint


def _build_nested(tp: type, environ: Mapping[str, str], problems: list[str]) -> Any:
    """Build a nested dataclass; a type with ``from_env(environ)`` builds itself.

    ``from_env`` is how ``pyrock.Config`` reads its ``BEDROCK_*`` variables
    from the same mapping. Its ``ValueError`` (with ``problems`` when present)
    is folded into ``problems``.
    """
    from_env = getattr(tp, "from_env", None)
    if not callable(from_env):
        return _build(tp, environ, problems)
    try:
        return from_env(environ)
    except ValueError as exc:
        problems.extend(getattr(exc, "problems", None) or [str(exc)])
        return None


def _build(cls: type[T], environ: Mapping[str, str], problems: list[str]) -> T | None:
    """Instantiate ``cls``, appending every problem; ``None`` when any was found."""
    before = len(problems)
    hints = typing.get_type_hints(cls, include_extras=True)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if not f.init:
            continue
        marker, tp = _env_marker(hints[f.name])
        if marker is None:
            if dataclasses.is_dataclass(tp) and isinstance(tp, type):
                kwargs[f.name] = _build_nested(tp, environ, problems)
            continue
        raw = environ.get(marker.name, "")
        if not raw:
            if marker.required:
                problems.append(f"{marker.name}: required variable is not set")
            continue
        try:
            kwargs[f.name] = _convert(tp, raw, marker.sep)
        except ValueError as exc:
            problems.append(f"{marker.name}: {exc}")
    if len(problems) > before:
        return None
    return cls(**kwargs)


def parse(cls: type[T], /, *, environ: Mapping[str, str] | None = None) -> T:
    """Build an instance of the dataclass ``cls`` from environment variables.

    Fields annotated with :class:`Env` read their variable; nested dataclass
    fields are parsed recursively; fields with defaults keep them when the
    variable is unset. ``environ`` defaults to :data:`os.environ`.

    An empty variable counts as unset. Problems name the variable but never
    echo its value, which may be a secret.

    Raises :class:`ParseError` listing every failure, and ``TypeError`` when
    ``cls`` is not a dataclass or uses an unsupported field type.
    """
    target: object = cls
    if not (isinstance(target, type) and dataclasses.is_dataclass(target)):
        raise TypeError("pyrock.env.parse expects a dataclass type")
    problems: list[str] = []
    result = _build(cls, os.environ if environ is None else environ, problems)
    if problems or result is None:
        raise ParseError(problems)
    return result
