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


# --- scratch reference
import math as _math
from urllib.parse import parse_qs as _pq

def app(*, max_seconds=30.0, prefix="/debug/pprof"):
    from pyrock.profile import _heap, _locks, _pprof, _sampler, _stacks
    def reply(sr, status, body, ctype="text/plain; charset=utf-8", extra=()):
        sr(status, [("Content-Type", ctype), ("Content-Length", str(len(body))), *extra])
        return [body]
    def handler(environ, sr):
        path = environ.get("PATH_INFO", "")
        if not path.startswith(prefix + "/"):
            return reply(sr, "404 Not Found", b"not found\n")
        name = path[len(prefix) + 1:]
        if name not in ("profile", "heap", "goroutine", "mutex", "block"):
            return reply(sr, "404 Not Found", b"not found\n")
        if environ.get("REQUEST_METHOD") != "GET":
            return reply(sr, "405 Method Not Allowed", b"", extra=[("Allow", "GET")])
        q = _pq(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        octet = "application/octet-stream"
        if name == "profile":
            raw = q.get("seconds", ["30"])[0]
            try:
                secs = float(raw)
            except ValueError:
                return reply(sr, "400 Bad Request", b"bad seconds\n")
            if not _math.isfinite(secs) or secs <= 0 or secs > max_seconds:
                return reply(sr, "400 Bad Request", b"bad seconds\n")
            prof = _sampler.SamplingProfiler(hz=100, cpu=True).collect(secs)
            if prof is None:
                return reply(sr, "503 Service Unavailable", b"busy\n")
            return reply(sr, "200 OK", _pprof.encode_gzip(prof), octet)
        if name == "heap":
            h = _heap.HeapProfiler(); h.start()
            try: prof = h.snapshot()
            finally: h.stop()
            return reply(sr, "200 OK", _pprof.encode_gzip(prof), octet)
        if name == "goroutine":
            if q.get("debug", ["0"])[0] == "1":
                return reply(sr, "200 OK", _stacks.format_text().encode())
            return reply(sr, "200 OK", _pprof.encode_gzip(_stacks.thread_profile()), octet)
        prof = _locks.mutex_profile() if name == "mutex" else _locks.block_profile()
        return reply(sr, "200 OK", _pprof.encode_gzip(prof), octet)
    return handler
