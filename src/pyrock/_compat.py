"""Interpreter feature detection and small validation helpers.

A leaf module: it imports nothing from pyrock. Newer stdlib APIs are used only
behind feature detection here, so the rest of the package stays valid on 3.11.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, TypeVar, overload

if TYPE_CHECKING:
    import datetime

__all__ = [
    "LOG_LEVELS",
    "LOG_LEVEL_CHOICES",
    "MISSING_LABEL",
    "ConfigError",
    "gil_enabled",
    "is_nonempty_str",
    "is_number",
    "is_positive_int",
    "is_positive_number",
    "log_level_number",
    "seconds",
]

T = TypeVar("T")

MISSING_LABEL: Final = "_"
"""Label value of a declared metric label with no value (shared by ``attr`` and ``privacy``)."""

LOG_LEVELS: Mapping[str, int] = MappingProxyType(
    {
        "debug": 10,
        "info": 20,
        "warn": 30,
        "warning": 30,
        "error": 40,
        "critical": 50,
    }
)
"""Accepted level names (lowercase; matched case-insensitively) -> :mod:`logging` levels.

The one level table used by both ``Config.log_level`` validation and
``logs.parse_level``. The numbers equal ``logging.DEBUG`` ... ``logging.CRITICAL``;
they are spelled out so this module need not import :mod:`logging`.
"""

LOG_LEVEL_CHOICES = ", ".join(LOG_LEVELS)
"""The accepted names, for problem messages."""


def log_level_number(level: object) -> int | None:
    """The numeric level for a :data:`LOG_LEVELS` name (any case) or an ``int``; else ``None``."""
    if isinstance(level, int) and not isinstance(level, bool):
        return level
    if isinstance(level, str):
        return LOG_LEVELS.get(level.lower())
    return None


class ConfigError(ValueError):
    """Invalid configuration. :attr:`problems` lists every problem, not just the first.

    Raised by every pyrock configuration type (``pyrock.Config``,
    ``server.Config``, ``CloudConfig``, ``ProfileExportConfig``). It lives in this
    utility module so each of them can raise it without import cycles.
    """

    problems: list[str]

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = list(problems)
        super().__init__("invalid pyrock configuration: " + "; ".join(self.problems))


def gil_enabled() -> bool:
    """Return whether the GIL is enabled right now (always ``True`` before 3.13)."""
    check = getattr(sys, "_is_gil_enabled", None)
    if check is None:
        return True
    return bool(check())


# -- small validation helpers shared by the config dataclasses -------------
# They take ``object`` on purpose: they validate values whose static type a
# caller may not have respected.


@overload
def seconds(value: datetime.timedelta) -> float: ...


@overload
def seconds(value: T) -> T: ...


def seconds(value: object) -> object:
    """Normalize a :class:`datetime.timedelta` to float seconds; return others unchanged."""
    import datetime

    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    return value


def is_number(value: object) -> bool:
    """A finite ``int``/``float`` that is not a ``bool``."""
    import math

    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


def is_positive_number(value: object) -> bool:
    return is_number(value) and value > 0  # type: ignore[operator]


def is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)
