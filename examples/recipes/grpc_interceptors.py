"""Recipe: gRPC trace propagation and server operations for grpcio (copy into your project).

Requires ``grpcio`` (``pip install grpcio``) for this recipe only; pyrock itself
does not depend on it, and nothing in pyrock imports this file. It is the
Python counterpart of Go Bedrock's ``example/grpc``.

* :class:`ClientInterceptor` adds ``traceparent``/``tracestate`` metadata for
  the innermost pyrock span to every unary call.
* :class:`ServerInterceptor` continues the caller's trace: each unary call
  becomes a ``SERVER`` operation named ``grpc.server`` with an ``rpc.method``
  attribute and metric label (method names are a bounded set). An exception
  escaping the handler, ``context.abort()`` included, marks it failed.

grpcio runs server handlers on its own thread pool, where no pyrock instance
is active, so the server interceptor pins one with ``bedrock=``::

    bedrock = pyrock.init(service="users-grpc")
    server = grpc.server(
        concurrent.futures.ThreadPoolExecutor(max_workers=8),
        interceptors=[ServerInterceptor(bedrock)],
    )

    channel = grpc.intercept_channel(grpc.insecure_channel("users:50051"), ClientInterceptor())

Streaming RPCs pass through unchanged; extend ``intercept_service`` the same
way for ``unary_stream`` / ``stream_unary`` / ``stream_stream`` handlers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

import grpc

import pyrock
from pyrock.trace.http import HTTPPropagator

# gRPC metadata is a sequence of (key, value) pairs with lowercase keys, which
# is exactly the "list of pairs" carrier the HTTP propagator understands.
_PROPAGATOR = HTTPPropagator()


class _CallDetails(NamedTuple):
    method: str
    timeout: float | None
    metadata: Any
    credentials: Any
    wait_for_ready: bool | None
    compression: Any


class _ClientCallDetails(_CallDetails, grpc.ClientCallDetails):
    """``grpc.ClientCallDetails`` is abstract; this is the usual concrete form."""


class ClientInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Injects W3C trace context into the metadata of every unary call."""

    def intercept_unary_unary(
        self,
        continuation: Callable[[grpc.ClientCallDetails, Any], Any],
        client_call_details: grpc.ClientCallDetails,
        request: Any,
    ) -> Any:
        metadata: list[tuple[str, str | bytes]] = list(client_call_details.metadata or ())
        _PROPAGATOR.inject(metadata)
        details = _ClientCallDetails(
            client_call_details.method,
            client_call_details.timeout,
            metadata,
            client_call_details.credentials,
            getattr(client_call_details, "wait_for_ready", None),
            getattr(client_call_details, "compression", None),
        )
        return continuation(details, request)


class ServerInterceptor(grpc.ServerInterceptor):
    """Runs every unary handler inside a ``SERVER`` operation that continues the caller's trace."""

    def __init__(self, bedrock: pyrock.Bedrock | None = None, *, name: str = "grpc.server") -> None:
        self._bedrock = bedrock
        self._name = name

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler | None],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        method: str = handler_call_details.method  # e.g. "/users.Users/Get"
        metadata = list(handler_call_details.invocation_metadata or ())
        parent = _PROPAGATOR.extract(metadata)
        inner = handler.unary_unary

        def behavior(request: Any, context: grpc.ServicerContext) -> Any:
            with pyrock.operation(
                self._name,
                attrs={"rpc.system": "grpc", "rpc.method": method},
                metric_labels=["rpc.method"],
                kind=pyrock.SpanKind.SERVER,
                remote_parent=parent,
                bedrock=self._bedrock,
            ):
                return inner(request, context)

        return grpc.unary_unary_rpc_method_handler(
            behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
