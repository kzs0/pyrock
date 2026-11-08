"""Recipe: a FastAPI app with pyrock's ASGI middleware (copy into your project).

Requires ``fastapi`` and an ASGI server such as ``uvicorn``
(``pip install fastapi uvicorn``) for this recipe only; pyrock itself depends
on neither, and nothing in pyrock imports this file. Run with
``uvicorn fastapi_app:app``.

* The instance is built with :class:`pyrock.Bedrock` (live, not activated) and
  pinned on the middleware with ``bedrock=``, so every request runs with it
  active. :class:`pyrock.Bedrock` is an async context manager, so it doubles
  as the lifespan: shutting the app down flushes spans and stops the server.
* FastAPI records the matched route in ``scope["route"]``; the middleware uses
  its ``path_format`` (``/users/{user_id}``) as ``http.route``.
* ``@pyrock.operation`` keeps the endpoint's signature (``functools.wraps``),
  so FastAPI's parameter parsing is unaffected. Put it *below* the route
  decorator.
* ``asyncio.to_thread`` copies the context, so blocking work in a thread
  stays inside the operation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import pyrock
from pyrock.asgi import Middleware

bedrock = pyrock.Bedrock(service="users-api", static_attrs={"env": "prod"})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with bedrock:  # ashutdown() on exit
        yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(Middleware, bedrock=bedrock)


@pyrock.step("fetch_user")
def fetch_user(user_id: str) -> dict[str, str] | None:
    # Blocking I/O would go here; the step's attrs propagate to the operation.
    pyrock.register({"user.id": user_id})
    return {"id": user_id, "tier": "gold"} if user_id.isdigit() else None


@app.get("/users/{user_id}")
@pyrock.operation("get_user", metric_labels=["tier"], expected=(HTTPException,))
async def get_user(user_id: str) -> dict[str, str]:
    user = await asyncio.to_thread(fetch_user, user_id)
    if user is None:
        # Listed in expected=: a 404 is a normal outcome, not a failed operation.
        raise HTTPException(status_code=404, detail="user not found")
    pyrock.register(tier=user["tier"])
    pyrock.info("user loaded", {"user.id": user_id})
    return user
