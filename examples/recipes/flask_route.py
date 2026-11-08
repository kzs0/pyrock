"""Recipe: a Flask app with pyrock's WSGI middleware and route templates (copy into your project).

Requires ``flask`` (``pip install flask``) for this recipe only; pyrock itself
does not depend on it, and nothing in pyrock imports this file.

``http.route`` must be a template, never a raw path, or the request metrics
would get one series per URL. The ``before_request`` hook stores Flask's
matched rule (``/users/<user_id>``) under ``environ["pyrock.route"]``, which
the middleware reads when the request ends; unmatched requests (404s) share
the ``"_"`` route. Run with ``flask --app flask_route run`` or any WSGI server.

The request operation ends when the WSGI response is closed (that is how
streaming responses are timed). Real servers always close it; in tests, close
Flask test-client responses (or use them in a ``with`` block) before reading
request metrics.
"""

from __future__ import annotations

from flask import Flask, request

import pyrock
from pyrock.wsgi import ROUTE_KEY, Middleware

bedrock = pyrock.init(service="billing")

app = Flask(__name__)
# Construct the middleware after init(): it captures the active instance and
# activates it for every request, whichever thread the server uses.
app.wsgi_app = Middleware(app.wsgi_app)  # type: ignore[method-assign]


@app.before_request
def record_route_template() -> None:
    if request.url_rule is not None:
        request.environ[ROUTE_KEY] = request.url_rule.rule


@app.get("/users/<user_id>")
@pyrock.operation("get_user", metric_labels=["tier"])
def get_user(user_id: str) -> dict[str, str]:
    pyrock.register({"user.id": user_id}, tier="gold")
    pyrock.info("user loaded", {"user.id": user_id})
    return {"id": user_id}
