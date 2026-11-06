"""OTLP/HTTP JSON trace exporter (``urllib``).

Sends ``POST {endpoint}`` with ``Content-Type: application/json`` and the OTLP
JSON mapping of ``ExportTraceServiceRequest`` (IDs as lowercase hex, enum
values as integers, 64-bit integers as decimal strings). ``urllib`` and
``ssl`` are imported on the first export, not with this module.
"""

from __future__ import annotations

import base64
import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pyrock._version import __version__
from pyrock.attr import AttrValue
from pyrock.trace import Span, SpanEvent, StatusCode
from pyrock.trace.otlp import ExportError, PermanentExportError
from pyrock.trace.otlp._pb import (
    end_time_ns,
    normalized_items,
    resource_attributes,
    tracestate_text,
)

if TYPE_CHECKING:
    import ssl
    import urllib.request

__all__ = ["HTTPExporter", "encode_json", "to_otlp_json"]

_SCOPE_NAME = "pyrock"
_NON_FINITE = {math.inf: "Infinity", -math.inf: "-Infinity"}


# -- JSON mapping ---------------------------------------------------------


def _any_value(value: AttrValue) -> dict[str, Any]:
    """The JSON ``AnyValue`` of an already-normalized value."""
    tp = type(value)
    if tp is str:
        return {"stringValue": value}
    if tp is bool:
        return {"boolValue": value}
    if tp is int:
        return {"intValue": str(value)}
    if tp is float:
        return {"doubleValue": _double(value)}  # type: ignore[arg-type]
    if tp is bytes:
        return {"bytesValue": base64.b64encode(value).decode("ascii")}  # type: ignore[arg-type]
    return {"arrayValue": {"values": [_any_value(v) for v in value]}}  # type: ignore[union-attr]


def _double(value: float) -> float | str:
    """Proto3 JSON spells non-finite doubles as strings."""
    if math.isfinite(value):
        return value
    return "NaN" if math.isnan(value) else _NON_FINITE[value]


def _attributes(out: dict[str, Any], attrs: Mapping[str, AttrValue]) -> None:
    items, dropped = normalized_items(attrs)
    if items:
        out["attributes"] = [{"key": k, "value": _any_value(v)} for k, v in items]
    if dropped:
        out["droppedAttributesCount"] = dropped


def _event(event: SpanEvent) -> dict[str, Any]:
    out: dict[str, Any] = {"timeUnixNano": str(event.timestamp_ns), "name": event.name}
    _attributes(out, event.attrs)
    return out


def _span(span: Span) -> dict[str, Any]:
    ctx = span.context
    out: dict[str, Any] = {"traceId": ctx.trace_id_hex, "spanId": ctx.span_id_hex}
    if ctx.trace_state:
        out["traceState"] = tracestate_text(ctx.trace_state)
    if span.parent is not None:
        out["parentSpanId"] = span.parent.span_id_hex
    out["name"] = span.name
    out["kind"] = int(span.kind)
    out["startTimeUnixNano"] = str(span.start_time_ns)
    out["endTimeUnixNano"] = str(end_time_ns(span))
    _attributes(out, span.attrs)
    events = span.events
    if events:
        out["events"] = [_event(e) for e in events]
    code, message = span.status
    if code is not StatusCode.UNSET:
        out["status"] = {"code": int(code), "message": message} if message else {"code": int(code)}
    return out


def to_otlp_json(
    spans: Sequence[Span],
    /,
    *,
    service: str,
    resource: Mapping[str, AttrValue] | None = None,
) -> dict[str, Any]:
    """Return the OTLP JSON request object (``{"resourceSpans": [...]}``)."""
    resource_obj: dict[str, Any] = {}
    _attributes(resource_obj, resource_attributes(service, resource))
    scope_spans = {
        "scope": {"name": _SCOPE_NAME, "version": __version__},
        "spans": [_span(span) for span in spans],
    }
    return {"resourceSpans": [{"resource": resource_obj, "scopeSpans": [scope_spans]}]}


def encode_json(
    spans: Sequence[Span],
    /,
    *,
    service: str,
    resource: Mapping[str, AttrValue] | None = None,
) -> bytes:
    """Return the UTF-8 JSON request body."""
    request = to_otlp_json(spans, service=service, resource=resource)
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()


# -- exporter -------------------------------------------------------------

_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_BAD_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _validated_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Copy ``headers``; ``ValueError`` for a non-token name or a control character in a value."""
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if not _TOKEN.fullmatch(name) or _BAD_VALUE.search(value):
            raise ValueError(f"invalid HTTP header {name!r}")
        out[name] = value
    return out


def _status_error(status: int, reason: str) -> ExportError:
    """429 and 5xx may succeed later; any other non-2xx status will not."""
    message = f"OTLP/HTTP export rejected: HTTP {status} {reason}".rstrip()
    if status == 429 or status >= 500:
        return ExportError(message)
    return PermanentExportError(message)


class HTTPExporter:
    """Exports spans with OTLP/HTTP JSON. Raises ``ExportError`` on failure.

    HTTP 429, 5xx, timeouts and connection errors raise a retryable
    :class:`~pyrock.trace.otlp.ExportError`; other non-2xx statuses (including
    redirects, which are never followed) raise
    :class:`~pyrock.trace.otlp.PermanentExportError`. ``headers`` are sent on
    every request; ``ValueError`` if a name or value is invalid.
    """

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
        self._endpoint = endpoint
        self._service = service
        self._resource = dict(resource or {})
        self._headers = {**_validated_headers(headers), "Content-Type": "application/json"}
        self._timeout = timeout
        self._ssl_context = ssl_context
        self._opener: urllib.request.OpenerDirector | None = None
        self._lock = threading.Lock()
        self._stopped = False

    @property
    def endpoint(self) -> str:
        """The collector URL."""
        return self._endpoint

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        """POST ``spans``; nothing is sent for an empty batch or after :meth:`shutdown`."""
        if not spans or self._stopped:
            return
        import http.client
        import urllib.error
        import urllib.request

        body = encode_json(spans, service=self._service, resource=self._resource)
        request = urllib.request.Request(
            self._endpoint, data=body, headers=self._headers, method="POST"
        )
        try:
            with self._get_opener().open(
                request, timeout=self._timeout if timeout is None else timeout
            ) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _status_error(exc.code, exc.reason) from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # URLError, timeouts, resets, malformed responses, bad URLs
            raise ExportError(f"OTLP/HTTP export to {self._endpoint} failed: {exc}") from exc

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Stop exporting (idempotent); later exports send nothing."""
        self._stopped = True

    def _get_opener(self) -> urllib.request.OpenerDirector:
        with self._lock:
            if self._opener is None:
                self._opener = _build_opener(self._ssl_context)
            return self._opener


def _build_opener(ssl_context: ssl.SSLContext | None) -> urllib.request.OpenerDirector:
    """An opener that never follows redirects (headers may carry credentials)."""
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    handlers: list[urllib.request.BaseHandler] = [NoRedirect()]
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    return urllib.request.build_opener(*handlers)
