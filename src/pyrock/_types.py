"""Shared protocol-level type aliases (WSGI, ASGI).

A leaf module: it imports nothing from pyrock, so every layer may use it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from types import TracebackType
from typing import Any, TypeAlias

__all__ = [
    "ASGIApp",
    "ASGIMessage",
    "ASGIReceive",
    "ASGIScope",
    "ASGISend",
    "ExcInfo",
    "StartResponse",
    "WSGIApp",
    "WSGIEnviron",
]

ExcInfo: TypeAlias = tuple[type[BaseException], BaseException, TracebackType | None]

WSGIEnviron: TypeAlias = dict[str, Any]
StartResponse: TypeAlias = Callable[..., Callable[[bytes], object]]
"""``start_response(status, headers, exc_info=None) -> write``."""
WSGIApp: TypeAlias = Callable[[WSGIEnviron, StartResponse], Iterable[bytes]]

ASGIScope: TypeAlias = MutableMapping[str, Any]
ASGIMessage: TypeAlias = MutableMapping[str, Any]
ASGIReceive: TypeAlias = Callable[[], Awaitable[ASGIMessage]]
ASGISend: TypeAlias = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp: TypeAlias = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]
