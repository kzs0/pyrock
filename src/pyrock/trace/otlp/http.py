"""OTLP/HTTP JSON trace exporter (``urllib``).

Sends ``POST {endpoint}`` with ``Content-Type: application/json`` and the OTLP
JSON mapping of ``ExportTraceServiceRequest`` (IDs as lowercase hex, enum
values as integers, 64-bit integers as decimal strings).
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping, Sequence
from typing import Any

from pyrock.attr import AttrValue
from pyrock.trace import Span

__all__ = ["HTTPExporter", "encode_json", "to_otlp_json"]


def to_otlp_json(
    spans: Sequence[Span],
    /,
    *,
    service: str,
    resource: Mapping[str, AttrValue] | None = None,
) -> dict[str, Any]:
    """Return the OTLP JSON request object (``{"resourceSpans": [...]}``)."""
    raise NotImplementedError


def encode_json(
    spans: Sequence[Span],
    /,
    *,
    service: str,
    resource: Mapping[str, AttrValue] | None = None,
) -> bytes:
    """Return the UTF-8 JSON request body."""
    raise NotImplementedError


class HTTPExporter:
    """Exports spans with OTLP/HTTP JSON. Raises ``ExportError`` on failure."""

    def __init__(
        self,
        endpoint: str,
        *,
        service: str,
        resource: Mapping[str, AttrValue] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        raise NotImplementedError

    @property
    def endpoint(self) -> str:
        raise NotImplementedError

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        raise NotImplementedError
