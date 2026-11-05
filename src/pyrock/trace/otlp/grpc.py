"""OTLP/gRPC trace exporter over the in-house HTTP/2 client (:mod:`._h2`).

Calls ``/opentelemetry.proto.collector.trace.v1.TraceService/Export`` with a
length-prefixed protobuf body. Metadata header names and values are validated
(no CR/LF, lowercase names). A non-zero ``grpc-status`` raises ``ExportError``.
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping, Sequence

from pyrock.attr import AttrValue
from pyrock.trace import Span

__all__ = ["EXPORT_PATH", "GRPCExporter", "frame_message", "parse_endpoint"]

EXPORT_PATH = "/opentelemetry.proto.collector.trace.v1.TraceService/Export"


def frame_message(payload: bytes, /) -> bytes:
    """Prefix ``payload`` with the gRPC 5-byte header (uncompressed)."""
    raise NotImplementedError


def parse_endpoint(endpoint: str, /) -> tuple[str, int]:
    """Parse ``host:port`` (or a URL) into ``(host, port)``; ``ValueError`` if malformed."""
    raise NotImplementedError


class GRPCExporter:
    """Exports spans with OTLP/gRPC. Raises ``ExportError`` on failure."""

    def __init__(
        self,
        endpoint: str,
        *,
        service: str,
        resource: Mapping[str, AttrValue] | None = None,
        headers: Mapping[str, str] | None = None,
        insecure: bool = False,
        timeout: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """``ValueError`` for invalid metadata names/values or a malformed endpoint."""
        raise NotImplementedError

    @property
    def endpoint(self) -> str:
        raise NotImplementedError

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        raise NotImplementedError
