"""A stdlib-only WSGI service instrumented with pyrock.

Run it, then make a few requests::

    python examples/http_server.py            # or: uv run python examples/http_server.py
    curl http://127.0.0.1:8080/users/42
    curl http://127.0.0.1:8080/fail
    curl http://127.0.0.1:9090/metrics         # the observability server

Every request becomes a ``SERVER`` operation named ``http.request`` with the
default attributes and labels (``http_method``, ``http_route``,
``http_status_code``). The app sets ``environ["pyrock.route"]`` to the route
*template* so that ``http_route`` stays low-cardinality. Set ``PORT`` and the
usual ``BEDROCK_*`` variables (``BEDROCK_TRACE_URL``, ``BEDROCK_LOG_FORMAT``,
...) to change the setup. Stop it with Ctrl-C.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from wsgiref.types import StartResponse, WSGIEnvironment

import pyrock
from pyrock.wsgi import ROUTE_KEY, Middleware

USERS = {"42": {"id": "42", "name": "Ada", "tier": "gold"}}


@pyrock.step("load_user")
def load_user(user_id: str) -> dict[str, str] | None:
    pyrock.register({"user.id": user_id})  # goes to the step span and the parent operation
    return USERS.get(user_id)


def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
    """A tiny router: ``/users/{id}``, ``/fail`` and 404 for everything else."""
    path = str(environ.get("PATH_INFO", "/"))

    if path.startswith("/users/"):
        environ[ROUTE_KEY] = "/users/{id}"
        with pyrock.operation("get_user", metric_labels=["tier"]) as op:
            user = load_user(path.removeprefix("/users/"))
            if user is None:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"not found\n"]
            op.register(tier=user["tier"])
            pyrock.info("user loaded", {"user.id": user["id"]})
            body = json.dumps(user).encode()
            start_response("200 OK", [("Content-Type", "application/json")])
            return [body]

    if path == "/fail":
        environ[ROUTE_KEY] = "/fail"
        # The exception escapes the app: the middleware records a failure with
        # http.status_code=500 and re-raises; wsgiref then answers 500.
        raise RuntimeError("simulated failure")

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found\n"]


class QuietHandler(WSGIRequestHandler):
    """wsgiref prints an access line per request to stderr; pyrock already logs."""

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(port: int) -> None:
    with pyrock.init(service="example-http"):
        # Build the middleware after init(): it captures the active instance.
        wrapped = Middleware(app)
        with make_server("127.0.0.1", port, wrapped, WSGIServer, QuietHandler) as httpd:
            pyrock.info("listening", url=f"http://127.0.0.1:{httpd.server_port}")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pyrock.info("shutting down")


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "8080")))
