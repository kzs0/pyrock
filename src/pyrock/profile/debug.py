"""Optional ``/debug/pprof/*`` WSGI handler serving gzipped pprof.

.. code-block:: python

    from pyrock.profile import debug
    pyrock.init(server_pprof=True, server_profile_handler=debug.app())

Endpoints: ``profile?seconds=N`` (``N`` above ``max_seconds`` is rejected with
``400``, as in Go), ``heap``,
``goroutine`` (threads and tasks; ``?debug=1`` returns text), ``mutex`` and
``block``. GET only (others: 405). Importing ``pyrock`` registers nothing; this
module must be imported explicitly (like Go's ``net/http/pprof``).
"""

from __future__ import annotations

from pyrock._types import WSGIApp

__all__ = ["app"]


def app(*, max_seconds: float = 30.0, prefix: str = "/debug/pprof") -> WSGIApp:
    """Return the WSGI app handling ``{prefix}/*``."""
    raise NotImplementedError
