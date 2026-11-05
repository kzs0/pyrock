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

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

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


def parse_duration(value: str, /) -> float:
    """Parse a Go duration (``10s``, ``500ms``, ``1m30s``, ``1h``) into seconds.

    A bare number is read as seconds. Units: ``ns``, ``us``/``µs``, ``ms``,
    ``s``, ``m``, ``h``. Raises ``ValueError`` on malformed input.
    """
    raise NotImplementedError


def parse_bool(value: str, /) -> bool:
    """Parse ``1/0/true/false/yes/no/on/off`` (case-insensitive)."""
    raise NotImplementedError


def parse(cls: type[T], /, *, environ: Mapping[str, str] | None = None) -> T:
    """Build an instance of the dataclass ``cls`` from environment variables.

    Fields annotated with :class:`Env` read their variable; nested dataclass
    fields are parsed recursively; fields with defaults keep them when the
    variable is unset. ``environ`` defaults to :data:`os.environ`.

    Raises :class:`ParseError` listing every failure.
    """
    _ = os.environ if environ is None else environ
    raise NotImplementedError
