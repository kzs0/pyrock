"""OTLP Profiles encoder (``v1development``, proto v0.2.0, same dictionaries as Go).

Produces ``ExportProfilesServiceRequest`` bytes. Post-policy label sets above
64 items, 4,096 nodes or depth 8 are rejected (``ValueError``), never silently
truncated.
"""

from __future__ import annotations

from collections.abc import Mapping

from pyrock.attr import AttrValue
from pyrock.profile._pprof import Profile

__all__ = ["CONTENT_TYPE", "encode_export_request"]

CONTENT_TYPE = "application/x-protobuf"


def encode_export_request(
    profile: Profile,
    /,
    *,
    service: str,
    profile_type: str,
    resource: Mapping[str, AttrValue] | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Encode one profile; ``ValueError`` if the result would exceed ``max_bytes``."""
    raise NotImplementedError
