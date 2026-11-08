"""Recipe: W3C trace propagation for httpx (copy into your project).

Requires ``httpx`` (``pip install httpx``) for this recipe only; pyrock itself
does not depend on it, and nothing in pyrock imports this file.

The event hooks write ``traceparent``/``tracestate`` for the innermost pyrock
span into every outgoing request. httpx runs request hooks in the caller's
context, so wrapping a call in :func:`pyrock.step` (or an operation) makes that
span the parent the downstream service continues::

    client = httpx.Client(event_hooks={"request": [inject_trace_headers]})
    with pyrock.step("HTTP GET users"):
        client.get("https://users.internal/users/42")

    async with httpx.AsyncClient(event_hooks={"request": [ainject_trace_headers]}) as client:
        with pyrock.step("HTTP GET users"):
            await client.get("https://users.internal/users/42")
"""

from __future__ import annotations

from typing import Any

import httpx

import pyrock


def inject_trace_headers(request: httpx.Request) -> None:
    """``httpx.Client`` request hook: add the current trace context to ``request``."""
    pyrock.inject_headers(request.headers)  # httpx.Headers is a MutableMapping[str, str]


async def ainject_trace_headers(request: httpx.Request) -> None:
    """``httpx.AsyncClient`` request hook (async clients need coroutine hooks)."""
    pyrock.inject_headers(request.headers)


def traced_client(**kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` with the propagation hook added (other hooks are kept)."""
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [*hooks.get("request", []), inject_trace_headers]
    return httpx.Client(event_hooks=hooks, **kwargs)
