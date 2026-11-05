"""End-to-end: trace continuity across services and transports (DESIGN §9.4).

* Two in-process services talking real HTTP (WSGI -> WSGI, ASGI -> WSGI,
  stdlib ``http.server`` via ``instrument_http_server``) share one trace with
  correct parent/child links.
* A custom ``Propagator`` (Kafka-style list of ``(str, bytes)`` headers) round
  trips through ``remote_parent``.
* ``pyrock.trace.http`` and ``pyrock.trace.w3c`` behave per W3C Trace Context.
"""

from __future__ import annotations

import asyncio
import http.client
import http.server
from typing import Any, ClassVar

import pytest

import pyrock
import pyrock.http
from pyrock.testing import capture
from pyrock.trace import SpanContext, SpanKind, TraceState, w3c
from pyrock.trace.http import HTTPPropagator, WSGIEnvironHeaders
from pyrock.trace.propagator import CompositePropagator, Propagator
from pyrock.wsgi import Middleware as WSGIMiddleware
from tests.e2e._http_helpers import (
    PARENT_SPAN_ID,
    TRACE_ID,
    TRACEPARENT,
    serve_handler,
    serve_wsgi,
)
from tests.support.asgi_driver import run_asgi
from tests.support.wsgi_driver import make_environ


@pytest.fixture(autouse=True)
def _env(clean_env: pytest.MonkeyPatch) -> None:
    return None


def _backend() -> Any:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"from-b"]

    return WSGIMiddleware(app, operation_name="service.b")


def _frontend(b_url: str) -> Any:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        upstream = pyrock.http.get(b_url + "/data", timeout=5).read()
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"a+" + upstream]

    return WSGIMiddleware(app, operation_name="service.a")


# ---------------------------------------------------------------- two services


def test_two_wsgi_services_share_one_trace() -> None:
    with (
        capture() as t,
        serve_wsgi(_backend()) as b_url,
        serve_wsgi(_frontend(b_url)) as a_url,
        pyrock.operation("client"),
    ):
        body = pyrock.http.get(a_url + "/", timeout=5).read()
    assert body == b"a+from-b"
    trace_ids = {s.trace_id for s in t.spans}
    assert len(trace_ids) == 1
    assert trace_ids == {t.spans.one(name="client").trace_id}


def test_two_wsgi_services_link_parent_and_child() -> None:
    with (
        capture() as t,
        serve_wsgi(_backend()) as b_url,
        serve_wsgi(_frontend(b_url)) as a_url,
        pyrock.operation("client"),
    ):
        pyrock.http.get(a_url + "/", timeout=5).read()
    client = t.spans.one(name="client")
    service_a = t.spans.one(name="service.a")
    service_b = t.spans.one(name="service.b")
    calls = {s.span_id: s for s in t.spans.find(kind=SpanKind.CLIENT)}
    assert len(calls) == 2
    call_to_a = calls[service_a.parent_span_id]
    call_to_b = calls[service_b.parent_span_id]
    assert call_to_a.parent_span_id == client.span_id
    assert call_to_b.parent_span_id == service_a.span_id
    assert service_a.kind is SpanKind.SERVER
    assert service_b.kind is SpanKind.SERVER


def test_asgi_service_calling_wsgi_service_shares_trace() -> None:
    async def service_a(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        resp = await asyncio.to_thread(pyrock.http.get, scope["state"]["b_url"] + "/x")
        body = resp.read()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a+" + body})

    from pyrock.asgi import Middleware as ASGIMiddleware

    with capture() as t, serve_wsgi(_backend()) as b_url:
        resp = run_asgi(
            ASGIMiddleware(service_a, operation_name="service.a"),
            "GET",
            "/",
            headers={"host": "a.test", "traceparent": TRACEPARENT},
            scope={"state": {"b_url": b_url}},
        )
    assert resp.body == b"a+from-b"
    service_a_span = t.spans.one(name="service.a")
    service_b_span = t.spans.one(name="service.b")
    assert service_a_span.trace_id == TRACE_ID
    assert service_a_span.parent_span_id == PARENT_SPAN_ID
    assert service_b_span.trace_id == TRACE_ID
    call = t.spans.one(name="HTTP GET")
    assert call.parent_span_id == service_a_span.span_id
    assert service_b_span.parent_span_id == call.span_id


# ---------------------------------------------------------------- stdlib http.server


class _Hello(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        type(self).seen.append(pyrock.current_operation())
        status = 404 if self.path.startswith("/missing") else 200
        body = b"hi"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _raw_get(base: str, path: str, headers: dict[str, str] | None = None) -> int:
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def test_instrument_http_server_returns_a_subclass() -> None:
    instrumented = pyrock.http.instrument_http_server(_Hello)
    assert issubclass(instrumented, _Hello)


def test_instrument_http_server_opens_server_span() -> None:
    handler = pyrock.http.instrument_http_server(_Hello)
    with capture() as t, serve_handler(handler) as base:
        assert _raw_get(base, "/hello/bob") == 200
    span = t.spans.one(name="http.request")
    assert span.kind is SpanKind.SERVER
    assert span.attrs["http.method"] == "GET"
    assert span.attrs["http.status_code"] == 200
    assert span.attrs["http.route"] == "_"


def test_instrument_http_server_extracts_uppercase_traceparent() -> None:
    handler = pyrock.http.instrument_http_server(_Hello)
    with capture() as t, serve_handler(handler) as base:
        _raw_get(base, "/", {"TRACEPARENT": TRACEPARENT, "TraceState": "vendor=abc"})
    span = t.spans.one(name="http.request")
    assert span.trace_id == TRACE_ID
    assert span.parent_span_id == PARENT_SPAN_ID


def test_instrument_http_server_route_and_operation_name() -> None:
    handler = pyrock.http.instrument_http_server(
        _Hello, operation_name="stdlib.request", route=lambda h: "/hello/{name}"
    )
    with capture() as t, serve_handler(handler) as base:
        _raw_get(base, "/hello/alice")
        prom_text = t.metrics.text()
    assert t.spans.one(name="stdlib.request").attrs["http.route"] == "/hello/{name}"
    assert "/hello/alice" not in prom_text


def test_instrument_http_server_error_status_is_failure() -> None:
    handler = pyrock.http.instrument_http_server(_Hello)
    with capture() as t, serve_handler(handler) as base:
        assert _raw_get(base, "/missing") == 404
    span = t.spans.one(name="http.request")
    assert span.failed
    assert span.attrs["http.status_code"] == 404


def test_instrument_http_server_handler_sees_current_operation() -> None:
    _Hello.seen = []
    handler = pyrock.http.instrument_http_server(_Hello)
    with capture() as t, serve_handler(handler) as base:
        _raw_get(base, "/")
    (op,) = _Hello.seen
    assert op is not None
    assert op.span_id == t.spans.one(name="http.request").span_id


# ---------------------------------------------------------------- custom propagator


class KafkaPropagator:
    """The DESIGN §9.4 example: W3C context in a list of ``(str, bytes)`` headers."""

    def extract(self, carrier: list[tuple[str, bytes]]) -> SpanContext | None:
        traceparent = tracestate = None
        for key, value in carrier:
            if key == "traceparent":
                traceparent = value.decode()
            elif key == "tracestate":
                tracestate = value.decode()
        if traceparent is None:
            return None
        try:
            return w3c.span_context_from_traceparent(traceparent, tracestate)
        except w3c.InvalidTraceparent:
            return None

    def inject(
        self, carrier: list[tuple[str, bytes]], span_context: SpanContext | None = None
    ) -> None:
        sc = span_context or pyrock.current_span_context()
        if sc is None:
            return
        carrier.append(("traceparent", w3c.format_traceparent(sc).encode()))
        if len(sc.trace_state):
            carrier.append(("tracestate", w3c.format_tracestate(sc.trace_state).encode()))


def test_custom_propagator_satisfies_the_protocol() -> None:
    assert isinstance(KafkaPropagator(), Propagator)
    assert isinstance(HTTPPropagator(), Propagator)


def test_custom_propagator_round_trip_via_remote_parent() -> None:
    prop = KafkaPropagator()
    headers: list[tuple[str, bytes]] = []
    with capture() as t:
        with pyrock.operation("produce", kind=SpanKind.PRODUCER):
            prop.inject(headers)
        remote = prop.extract(headers)
        assert remote is not None
        assert remote.is_remote
        with pyrock.operation("consume", remote_parent=remote, kind=SpanKind.CONSUMER):
            pass
    produce = t.spans.one(name="produce")
    consume = t.spans.one(name="consume")
    assert consume.kind is SpanKind.CONSUMER
    assert consume.trace_id == produce.trace_id
    assert consume.parent_span_id == produce.span_id


def test_custom_propagator_carries_tracestate() -> None:
    prop = KafkaPropagator()
    parent = SpanContext.from_hex(
        TRACE_ID, PARENT_SPAN_ID, trace_state=TraceState([("vendor", "v1")]), is_remote=True
    )
    headers: list[tuple[str, bytes]] = []
    with capture(), pyrock.operation("relay", remote_parent=parent):
        prop.inject(headers)
    extracted = prop.extract(headers)
    assert extracted is not None
    assert extracted.trace_id_hex == TRACE_ID
    assert dict(extracted.trace_state) == {"vendor": "v1"}


def test_composite_propagator_injects_all_and_extracts_first_hit() -> None:
    sc = SpanContext.from_hex(TRACE_ID, PARENT_SPAN_ID)
    composite = CompositePropagator(HTTPPropagator(), KafkaPropagator())
    assert len(composite.propagators) == 2
    carrier: dict[str, str] = {}
    HTTPPropagator().inject(carrier, sc)
    extracted = composite.extract(carrier)
    assert extracted is not None
    assert extracted.span_id_hex == PARENT_SPAN_ID


# ---------------------------------------------------------------- HTTPPropagator


def test_http_propagator_extract_is_case_insensitive() -> None:
    sc = HTTPPropagator().extract({"TraceParent": TRACEPARENT, "TRACESTATE": "a=1"})
    assert sc is not None
    assert (sc.trace_id_hex, sc.span_id_hex) == (TRACE_ID, PARENT_SPAN_ID)
    assert sc.is_remote
    assert sc.sampled
    assert dict(sc.trace_state) == {"a": "1"}


def test_http_propagator_extract_from_asgi_byte_pairs_joins_tracestate() -> None:
    carrier = [
        (b"traceparent", TRACEPARENT.encode()),
        (b"tracestate", b"a=1"),
        (b"tracestate", b"b=2"),
    ]
    sc = HTTPPropagator().extract(carrier)
    assert sc is not None
    assert sc.trace_state.entries() == (("a", "1"), ("b", "2"))


def test_http_propagator_extract_invalid_returns_none() -> None:
    assert HTTPPropagator().extract({"traceparent": "00-bad", "tracestate": "a=1"}) is None
    assert HTTPPropagator().extract({}) is None


def test_http_propagator_inject_into_dict_and_pairs() -> None:
    sc = SpanContext.from_hex(
        TRACE_ID, PARENT_SPAN_ID, trace_state=TraceState([("a", "1")]), sampled=False
    )
    mapping: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    HTTPPropagator().inject(mapping, sc)
    HTTPPropagator().inject(pairs, sc)
    expected = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-00"
    assert mapping == {"traceparent": expected, "tracestate": "a=1"}
    assert ("traceparent", expected) in pairs
    assert ("tracestate", "a=1") in pairs


def test_http_propagator_inject_without_context_is_noop() -> None:
    carrier: dict[str, str] = {}
    HTTPPropagator().inject(carrier)
    assert carrier == {}


def test_wsgi_environ_headers_view() -> None:
    environ = make_environ(
        "GET", "/", headers={"traceparent": TRACEPARENT, "Content-Type": "text/plain"}
    )
    view = WSGIEnvironHeaders(environ)
    assert view["Traceparent"] == TRACEPARENT
    assert view["content-type"] == "text/plain"
    sc = HTTPPropagator().extract(view)
    assert sc is not None
    assert sc.trace_id_hex == TRACE_ID


# ---------------------------------------------------------------- w3c


def test_w3c_parse_traceparent() -> None:
    trace_id, span_id, flags = w3c.parse_traceparent(TRACEPARENT)
    assert trace_id.hex() == TRACE_ID
    assert span_id.hex() == PARENT_SPAN_ID
    assert flags == w3c.SAMPLED_FLAG


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01", id="upper-trace"),
        pytest.param("00-0af7651916cd43dd8448eb211c80319c-B7AD6B7169203331-01", id="upper-span"),
        pytest.param("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-0A", id="upper-flags"),
        pytest.param("00-00000000000000000000000000000000-b7ad6b7169203331-01", id="zero-trace"),
        pytest.param("00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01", id="zero-span"),
        pytest.param("ff-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", id="version-ff"),
        pytest.param("00-0af7651916cd43dd8448eb211c80319z-b7ad6b7169203331-01", id="non-hex"),
        pytest.param("00-abc-def-01", id="too-short"),
        pytest.param("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331", id="field-count"),
        pytest.param(
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-extra", id="v00-extra"
        ),
    ],
)
def test_w3c_invalid_traceparent(value: str) -> None:
    with pytest.raises(w3c.InvalidTraceparent):
        w3c.parse_traceparent(value)


def test_w3c_future_version_is_accepted() -> None:
    trace_id, _, _ = w3c.parse_traceparent(
        "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-extra-data"
    )
    assert trace_id.hex() == TRACE_ID


def test_w3c_format_traceparent_round_trip() -> None:
    for sampled, flags in ((True, "01"), (False, "00")):
        sc = SpanContext.from_hex(TRACE_ID, PARENT_SPAN_ID, sampled=sampled)
        text = w3c.format_traceparent(sc)
        assert text == f"00-{TRACE_ID}-{PARENT_SPAN_ID}-{flags}"
        trace_id, span_id, parsed_flags = w3c.parse_traceparent(text)
        assert (trace_id, span_id, parsed_flags) == (sc.trace_id, sc.span_id, sc.trace_flags)


def test_w3c_span_context_from_traceparent_is_remote() -> None:
    sc = w3c.span_context_from_traceparent(TRACEPARENT, "vendor=x")
    assert sc.is_remote
    assert sc.trace_id_hex == TRACE_ID
    assert dict(sc.trace_state) == {"vendor": "x"}


def test_w3c_invalid_tracestate_is_ignored_by_span_context() -> None:
    sc = w3c.span_context_from_traceparent(TRACEPARENT, "no-equals-sign")
    assert len(sc.trace_state) == 0


def test_w3c_parse_tracestate_entries_and_whitespace() -> None:
    state = w3c.parse_tracestate("vendor1=value1 , tenant@vendor=v2,,")
    assert state.entries() == (("vendor1", "value1"), ("tenant@vendor", "v2"))


def test_w3c_tracestate_32_entries_allowed_33_rejected() -> None:
    ok = ",".join(f"k{i}=v{i}" for i in range(32))
    assert len(w3c.parse_tracestate(ok)) == w3c.MAX_TRACESTATE_ENTRIES == 32
    too_many = ",".join(f"k{i}=v{i}" for i in range(33))
    with pytest.raises(w3c.InvalidTracestate):
        w3c.parse_tracestate(too_many)


@pytest.mark.parametrize("value", ["novalue", "k=a,b", "k=a=b"])
def test_w3c_invalid_tracestate(value: str) -> None:
    with pytest.raises(w3c.InvalidTracestate):
        w3c.parse_tracestate(value)


def test_w3c_format_tracestate_skips_invalid_entries() -> None:
    text = w3c.format_tracestate([("good", "1"), ("Bad Key", "2"), ("also", "x,y"), ("ok", "3")])
    assert text == "good=1,ok=3"


@pytest.mark.parametrize(
    ("key", "valid"),
    [("vendor", True), ("tenant@system", True), ("a1_-*/", True), ("Upper", False), ("", False)],
)
def test_w3c_tracestate_key_validation(key: str, valid: bool) -> None:
    assert w3c.is_valid_tracestate_key(key) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [("abc", True), ("a b", True), ("a,b", False), ("a=b", False), ("v ", False)],
)
def test_w3c_tracestate_value_validation(value: str, valid: bool) -> None:
    assert w3c.is_valid_tracestate_value(value) is valid
