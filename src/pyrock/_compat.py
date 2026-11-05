"""Interpreter feature detection and small import-time helpers.

A leaf module: it imports nothing from pyrock. Newer stdlib APIs are used only
behind feature detection here, so the rest of the package stays valid on 3.11.
"""

from __future__ import annotations

import sys
import types
from typing import Any

__all__ = ["CallableModule", "gil_enabled", "is_free_threaded_build", "make_module_callable"]

CALL_TARGET_ATTR = "__pyrock_call__"


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


class CallableModule(types.ModuleType):
    """A module that can also be called.

    ``pyrock.log`` and ``pyrock.cloud`` are both submodules and top-level
    functions (``pyrock.log(logging.INFO, "msg")``, ``pyrock.cloud("key")``).
    Importing a submodule rebinds the parent's attribute to the module, so the
    function would be lost. Making the module itself callable keeps both
    spellings working: calls are forwarded to the module attribute named by
    ``CALL_TARGET_ATTR``.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        target = self.__dict__.get(CALL_TARGET_ATTR)
        if target is None:
            raise TypeError(f"module {self.__name__!r} is not callable")
        return target(*args, **kwargs)


def make_module_callable(module_name: str, target: Any) -> None:
    """Make ``sys.modules[module_name]`` callable, forwarding to ``target``."""
    module = sys.modules[module_name]
    module.__dict__[CALL_TARGET_ATTR] = target
    if not isinstance(module, CallableModule):
        module.__class__ = CallableModule


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
