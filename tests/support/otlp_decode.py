"""Decode OTLP payloads into plain Python structures for assertions.

* JSON (OTLP/HTTP): :func:`decode_attributes`, :func:`flatten_spans`.
* Protobuf: :func:`decode_trace_request_pb` / :func:`decode_profiles_request_pb`
  use the official ``opentelemetry-proto`` schema (compat group) and skip the
  calling test when it is not installed.

Flattened span dicts have these keys::

    name, trace_id, span_id, parent_span_id (None for roots), trace_state,
    flags, kind (int), start_time_unix_nano (int), end_time_unix_nano (int),
    attributes (dict), events [{name, time_unix_nano, attributes}],
    links [{trace_id, span_id, attributes}], status_code (int, 0 = unset),
    status_message, dropped_attributes_count, resource (dict),
    scope_name, scope_version
"""

from __future__ import annotations

import base64
import importlib
from typing import Any

__all__ = [
    "decode_any_value",
    "decode_attributes",
    "decode_profiles_request_pb",
    "decode_trace_request_pb",
    "flatten_spans",
    "proto_available",
    "require_proto",
]


def decode_any_value(value: dict[str, Any] | None) -> Any:
    """Convert an OTLP JSON ``AnyValue`` into a Python value.

    ``intValue`` (a decimal string in JSON) becomes ``int``; ``bytesValue``
    (base64) becomes ``bytes``; arrays become lists and kvlists dicts. An empty
    ``AnyValue`` decodes to ``None``.
    """
    if not value:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        raw = value["doubleValue"]
        return float(raw) if not isinstance(raw, str) else float(raw.replace("Infinity", "inf"))
    if "bytesValue" in value:
        return base64.b64decode(value["bytesValue"])
    if "arrayValue" in value:
        return [decode_any_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return decode_attributes(value["kvlistValue"].get("values", []))
    raise ValueError(f"unknown AnyValue shape: {value!r}")


def decode_attributes(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Convert an OTLP ``KeyValue`` list into a dict (later duplicates win)."""
    out: dict[str, Any] = {}
    for kv in attrs or []:
        out[kv["key"]] = decode_any_value(kv.get("value"))
    return out


def _id(value: str | None, encoding: str) -> str | None:
    if not value:
        return None
    if encoding == "base64":
        return base64.b64decode(value).hex()
    return value


def flatten_spans(request: dict[str, Any], *, id_encoding: str = "hex") -> list[dict[str, Any]]:
    """Flatten an ``ExportTraceServiceRequest`` JSON object into span dicts.

    ``id_encoding`` is ``"hex"`` for OTLP/JSON (the wire format) and
    ``"base64"`` for ``MessageToDict`` output of protobuf messages.
    """
    spans: list[dict[str, Any]] = []
    for rs in request.get("resourceSpans", []):
        resource = decode_attributes(rs.get("resource", {}).get("attributes"))
        for ss in rs.get("scopeSpans", []):
            scope = ss.get("scope", {}) or {}
            for sp in ss.get("spans", []):
                status = sp.get("status", {}) or {}
                spans.append(
                    {
                        "name": sp.get("name", ""),
                        "trace_id": _id(sp.get("traceId"), id_encoding),
                        "span_id": _id(sp.get("spanId"), id_encoding),
                        "parent_span_id": _id(sp.get("parentSpanId"), id_encoding),
                        "trace_state": sp.get("traceState", ""),
                        "flags": int(sp.get("flags", 0)),
                        "kind": int(sp.get("kind", 0)),
                        "start_time_unix_nano": int(sp.get("startTimeUnixNano", 0)),
                        "end_time_unix_nano": int(sp.get("endTimeUnixNano", 0)),
                        "attributes": decode_attributes(sp.get("attributes")),
                        "events": [
                            {
                                "name": ev.get("name", ""),
                                "time_unix_nano": int(ev.get("timeUnixNano", 0)),
                                "attributes": decode_attributes(ev.get("attributes")),
                            }
                            for ev in sp.get("events", [])
                        ],
                        "links": [
                            {
                                "trace_id": _id(ln.get("traceId"), id_encoding),
                                "span_id": _id(ln.get("spanId"), id_encoding),
                                "attributes": decode_attributes(ln.get("attributes")),
                            }
                            for ln in sp.get("links", [])
                        ],
                        "status_code": int(status.get("code", 0)),
                        "status_message": status.get("message", ""),
                        "dropped_attributes_count": int(sp.get("droppedAttributesCount", 0)),
                        "resource": resource,
                        "scope_name": scope.get("name", ""),
                        "scope_version": scope.get("version", ""),
                    }
                )
    return spans


# ---------------------------------------------------------------- protobuf

_TRACE_PB = "opentelemetry.proto.collector.trace.v1.trace_service_pb2"
_PROFILES_PB = "opentelemetry.proto.collector.profiles.v1development.profiles_service_pb2"


def proto_available() -> bool:
    """Whether ``protobuf`` and ``opentelemetry-proto`` can be imported."""
    try:
        importlib.import_module("google.protobuf.json_format")
        importlib.import_module(_TRACE_PB)
    except ImportError:
        return False
    return True


def require_proto(module: str = _TRACE_PB) -> Any:
    """Import an ``opentelemetry-proto`` module or skip the calling test."""
    import pytest

    try:
        importlib.import_module("google.protobuf.json_format")
        return importlib.import_module(module)
    except ImportError as exc:
        pytest.skip(f"compat dependency unavailable: {exc}")


def message_to_dict(message: Any) -> dict[str, Any]:
    """``MessageToDict`` with integer enums (bytes fields stay base64)."""
    json_format = importlib.import_module("google.protobuf.json_format")
    result: dict[str, Any] = json_format.MessageToDict(message, use_integers_for_enums=True)
    return result


def decode_trace_request_pb(data: bytes) -> list[dict[str, Any]]:
    """Decode ``ExportTraceServiceRequest`` bytes into flattened span dicts."""
    pb = require_proto(_TRACE_PB)
    request = pb.ExportTraceServiceRequest()
    request.ParseFromString(data)
    return flatten_spans(message_to_dict(request), id_encoding="base64")


def decode_profiles_request_pb(data: bytes) -> Any:
    """Decode ``ExportProfilesServiceRequest`` bytes into the protobuf message.

    Skips the calling test when the installed ``opentelemetry-proto`` lacks
    the ``v1development`` profiles schema.
    """
    pb = require_proto(_PROFILES_PB)
    request = pb.ExportProfilesServiceRequest()
    request.ParseFromString(data)
    return request
