"""OTLP Profiles encoder (``v1development``, proto v0.2.0, same dictionaries as Go).

Produces ``ExportProfilesServiceRequest`` bytes. Post-policy label sets above
64 items, 4,096 nodes or depth 8 are rejected (``ValueError``), never silently
truncated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pyrock.attr import AttrValue
from pyrock.profile._pprof import Profile

__all__ = ["CONTENT_TYPE", "LabelFilter", "encode_export_request"]

CONTENT_TYPE = "application/x-protobuf"

LabelFilter = Callable[[Mapping[str, str | int]], Mapping[str, Any]]
"""Cleans one sample's labels (``Policy.profile_labels`` when ``allow_profile_labels``)."""


def encode_export_request(
    profile: Profile,
    /,
    *,
    service: str,
    profile_type: str,
    resource: Mapping[str, AttrValue] | None = None,
    max_bytes: int | None = None,
    label_filter: LabelFilter | None = None,
) -> bytes:
    """Encode one profile; ``ValueError`` if the result would exceed ``max_bytes``.

    Layout follows Go's encoder: one ``Profile`` per sample type (each with a
    single ``sample_type``), a shared request-level dictionary whose tables
    start with an empty/zero entry at index 0, and a ``profile.type`` profile
    attribute. Sample labels are emitted only through ``label_filter``
    (``None`` drops every label); its output is checked against the 64 item /
    4,096 node / depth 8 limits and rejected with ``ValueError`` above them.
    """
    raise NotImplementedError
