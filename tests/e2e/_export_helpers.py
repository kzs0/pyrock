"""Private helpers for the export/backend end-to-end tests (agent E3).

Only pyrock's public API and the shared ``tests/support`` fakes are used.
Nothing here calls pyrock at import time, so the test modules always collect
(even while the implementation is still ``NotImplementedError`` stubs).
"""

from __future__ import annotations

import gzip
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tests.support.collectors import OTLPHTTPCollector, RecordedRequest
from tests.support.prom import PromText, parse

QUIET: dict[str, Any] = {
    "server_enabled": False,
    "disable_environment_detection": True,
    "runtime_metrics": False,
    "log_install": "none",
}
"""Overrides that keep an ``init()`` free of servers, probes, runtime series and logging."""


def start(**overrides: Any) -> Any:
    """``pyrock.init(activate=False, **QUIET, **overrides)``.

    Instances are never activated so nothing leaks into later tests through
    the context; tests call the method API (``b.operation(...)``) instead.
    """
    import pyrock

    kwargs: dict[str, Any] = {**QUIET, **overrides}
    return pyrock.init(activate=False, **kwargs)


@contextmanager
def running(**overrides: Any) -> Iterator[Any]:
    """:func:`start` plus a guaranteed ``shutdown()`` on exit."""
    b = start(**overrides)
    try:
        yield b
    finally:
        b.shutdown(timeout=10)


def exposition(b: Any) -> str:
    """The Prometheus text for ``b``'s registry."""
    from pyrock.metric import prometheus

    return prometheus.encode(b.metrics).decode("utf-8")


def prom(b: Any) -> PromText:
    return parse(exposition(b))


def family_block(text: str, family: str) -> list[str]:
    """The HELP/TYPE comment lines and sample lines that belong to ``family``.

    Histogram sample names (``_bucket``, ``_sum``, ``_count``) are included.
    """
    names = {family, f"{family}_bucket", f"{family}_sum", f"{family}_count"}
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith(("# HELP ", "# TYPE ")):
            if line.split(" ", 3)[2] == family:
                out.append(line)
            continue
        sample = line.split("{", 1)[0].split(" ", 1)[0]
        if sample in names:
            out.append(line)
    return out


def scrape_http(b: Any, path: str = "/metrics") -> tuple[int, dict[str, str], bytes]:
    """GET ``path`` from ``b``'s observability server."""
    address = b.server_address
    assert address is not None, "observability server is not running"
    host, port = address
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, headers, resp.read()


def raw_spans(collector: OTLPHTTPCollector) -> list[dict[str, Any]]:
    """Raw OTLP/JSON span objects (wire shape) from every trace request."""
    out: list[dict[str, Any]] = []
    for req in collector.trace_requests():
        for rs in req.json().get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                out.extend(ss.get("spans", []))
    return out


def raw_attr(collector: OTLPHTTPCollector, span_name: str, key: str) -> dict[str, Any]:
    """The raw OTLP/JSON ``AnyValue`` of attribute ``key`` on the span ``span_name``."""
    spans = [s for s in raw_spans(collector) if s.get("name") == span_name]
    assert len(spans) == 1, f"expected one span {span_name!r}, got {len(spans)}"
    for kv in spans[0].get("attributes", []):
        if kv["key"] == key:
            value: dict[str, Any] = kv["value"]
            return value
    raise AssertionError(f"span {span_name!r} has no attribute {key!r}")


def body(req: RecordedRequest) -> bytes:
    """Request body, gunzipped when ``Content-Encoding: gzip``."""
    data = req.body
    if (req.header("content-encoding") or "").lower() == "gzip" or data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def pause(seconds: float) -> None:
    """Let background workers run for a while (only for *negative* assertions)."""
    threading.Event().wait(seconds)


def export_one(
    collector: OTLPHTTPCollector, name: str = "probe", **overrides: Any
) -> dict[str, Any]:
    """Init with ``trace_url`` at ``collector``, run one operation, shut down, return its span."""
    with running(trace_url=collector.traces_url, **overrides) as b, b.operation(name):
        pass
    return collector.span(name)
