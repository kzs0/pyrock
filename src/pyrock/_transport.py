"""Small transport helpers shared by the exporters, pushers and the server.

A utility module: it imports nothing from pyrock, so any module may use it.
Network modules (``urllib.request``) are imported lazily, keeping
``import pyrock`` cheap.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl
    import urllib.request

__all__ = [
    "HEADER_NAME",
    "forward_fork",
    "header_problems",
    "no_redirect_opener",
    "remaining",
]

#: An HTTP header field name (RFC 9110 ``token``).
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_BAD_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def header_problems(headers: Mapping[Any, Any], *, field: str = "headers") -> list[str]:
    """Return one problem per invalid header; empty when all are valid.

    Names must be RFC 9110 tokens and values must not contain control
    characters other than horizontal tab, which rules out header injection.
    Messages name the header but never echo its value (it may be a secret).
    """
    problems: list[str] = []
    for name, value in headers.items():
        if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
            problems.append(f"{field}: invalid header name {name!r}")
        elif not isinstance(value, str) or _BAD_VALUE.search(value):
            problems.append(f"{field}: invalid value for header {name!r}")
    return problems


def remaining(deadline: float | None) -> float | None:
    """Seconds left until a ``time.monotonic()`` deadline (never negative), or ``None``."""
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def forward_fork(obj: object) -> None:
    """Call ``obj._after_fork_in_child()`` when it defines one."""
    reset = getattr(obj, "_after_fork_in_child", None)
    if callable(reset):
        reset()


def no_redirect_opener(
    ssl_context: ssl.SSLContext | None = None,
) -> urllib.request.OpenerDirector:
    """A urllib opener that never follows redirects.

    Exporter requests carry credentials in headers, so a redirect must surface
    as an error status instead of resending them to another host.
    """
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    return urllib.request.build_opener(*handlers)
