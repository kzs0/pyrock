"""Recipe: W3C trace propagation for requests (copy into your project).

Requires ``requests`` (``pip install requests``) for this recipe only; pyrock
itself does not depend on it, and nothing in pyrock imports this file.

The transport adapter writes ``traceparent``/``tracestate`` for the innermost
pyrock span into every request the session sends, redirects included::

    session = requests.Session()
    session.mount("https://", PyrockAdapter())
    session.mount("http://", PyrockAdapter())

    with pyrock.step("HTTP GET users"):  # the span the downstream service continues
        session.get("https://users.internal/users/42", timeout=10)
"""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter

import pyrock


class PyrockAdapter(HTTPAdapter):
    """An ``HTTPAdapter`` that injects W3C trace context headers."""

    def send(
        self, request: requests.PreparedRequest, *args: Any, **kwargs: Any
    ) -> requests.Response:
        # PreparedRequest.headers is a case-insensitive MutableMapping[str, str].
        pyrock.inject_headers(request.headers)
        return super().send(request, *args, **kwargs)


def traced_session() -> requests.Session:
    """A ``requests.Session`` with :class:`PyrockAdapter` mounted for http and https."""
    session = requests.Session()
    adapter = PyrockAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
