"""Interpreter feature detection and small validation helpers.

A leaf module: it imports nothing from pyrock. Newer stdlib APIs are used only
behind feature detection here, so the rest of the package stays valid on 3.11.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any

__all__ = [
    "ConfigError",
    "gil_enabled",
    "is_free_threaded_build",
    "is_nonempty_str",
    "is_number",
    "is_positive_int",
    "is_positive_number",
    "seconds",
]


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


def is_free_threaded_build() -> bool:
    """Return whether this interpreter was built with ``--disable-gil``."""
    import sysconfig

    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


# -- small validation helpers shared by the config dataclasses -------------
# They take ``object`` on purpose: they validate values whose static type a
# caller may not have respected.


def seconds(value: object) -> Any:
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
