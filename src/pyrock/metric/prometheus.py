"""Prometheus text exposition format 0.0.4.

:func:`encode` validates the **whole snapshot first** (bucket bounds finite and
increasing, cumulative counts monotonic and not above the count, names and
label names valid) and only then writes text. A validation failure raises
:class:`ExpositionError`, and ``/metrics`` answers 500 with no partial body.

Sample values and bucket bounds use Go's ``%g`` formatting (``strconv``
``FormatFloat(v, 'g', -1, 64)``): ``1``, ``0.5``, ``1e+06``, ``+Inf``, ``NaN``.
Metric family names turn dots into underscores (``http.request`` ->
``http_request_count``).
"""

from __future__ import annotations

from collections.abc import Iterable

from pyrock.metric import MetricFamily, Registry

__all__ = [
    "CONTENT_TYPE",
    "ExpositionError",
    "encode",
    "escape_help",
    "escape_label_value",
    "validate",
]

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class ExpositionError(ValueError):
    """The snapshot cannot be encoded as valid exposition text."""


def validate(families: Iterable[MetricFamily], /) -> None:
    """Raise :class:`ExpositionError` if any family is invalid."""
    raise NotImplementedError


def escape_help(text: str, /) -> str:
    """Escape ``\\`` and newlines in HELP text."""
    raise NotImplementedError


def escape_label_value(value: str, /) -> str:
    """Escape ``\\``, ``"`` and newlines in a label value."""
    raise NotImplementedError


def encode(source: Registry | Iterable[MetricFamily], /) -> bytes:
    """Return UTF-8 exposition text for a registry (collected now) or a snapshot."""
    raise NotImplementedError
