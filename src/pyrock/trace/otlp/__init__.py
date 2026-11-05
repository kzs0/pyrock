"""OTLP trace export: HTTP/JSON (:mod:`.http`), gRPC (:mod:`.grpc`) and batching.

Submodules load lazily; importing this package is cheap. ``grpc`` and ``_h2``
are only imported when a gRPC endpoint is configured.
"""

from __future__ import annotations

__all__ = ["ExportError", "PermanentExportError"]


class ExportError(Exception):
    """An export attempt failed and may be retried."""


class PermanentExportError(ExportError):
    """An export failed in a way retrying cannot fix (e.g. HTTP 400, gRPC INVALID_ARGUMENT)."""
