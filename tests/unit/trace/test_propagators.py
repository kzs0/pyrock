"""Propagators (DESIGN §9.4): HTTP, WSGI-environ adapter and composite."""

from __future__ import annotations

import contextvars
import io
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from pyrock.trace import (
    CompositePropagator,
    Propagator,
    Span,
    SpanContext,
    TraceState,
    current_span,
    use_span,
)
from pyrock.trace.http import HTTPPropagator, WSGIEnvironHeaders

TID = "0af7651916cd43dd8448eb211c80319c"
SID = "b7ad6b7169203331"
TP = f"00-{TID}-{SID}-01"
TP_UNSAMPLED = f"00-{TID}-{SID}-00"


class CaseInsensitiveHeaders(Mapping[str, str]):
    """A case-insensitive header mapping, like those of popular HTTP libraries."""

    def __init__(self, items: dict[str, str]) -> None:
        self._items = {k.lower(): (k, v) for k, v in items.items()}

    def __getitem__(self, key: str) -> str:
        return self._items[key.lower()][1]

    def __iter__(self) -> Iterator[str]:
        return (k for k, _ in self._items.values())

    def __len__(self) -> int:
        return len(self._items)


def _assert_tp(sc: SpanContext | None, *, sampled: bool = True) -> SpanContext:
    assert sc is not None
    assert sc.trace_id_hex == TID
    assert sc.span_id_hex == SID
    assert sc.sampled is sampled
    assert sc.is_remote is True
    return sc


# ---------------------------------------------------------------- extract


def test_http_propagator_satisfies_protocol() -> None:
    assert isinstance(HTTPPropagator(), Propagator)


def test_extract_from_dict() -> None:
    sc = _assert_tp(HTTPPropagator().extract({"traceparent": TP, "tracestate": "a=1,b=2"}))
    assert sc.trace_state.entries() == (("a", "1"), ("b", "2"))


def test_extract_unsampled() -> None:
    _assert_tp(HTTPPropagator().extract({"traceparent": TP_UNSAMPLED}), sampled=False)


@pytest.mark.parametrize("name", ["Traceparent", "TRACEPARENT", "TraceParent"])
def test_extract_plain_dict_is_case_insensitive(name: str) -> None:
    sc = _assert_tp(HTTPPropagator().extract({name: TP, "TraceState": "a=1"}))
    assert dict(sc.trace_state) == {"a": "1"}


def test_extract_from_case_insensitive_mapping() -> None:
    headers = CaseInsensitiveHeaders({"TraceParent": TP, "TRACESTATE": "v=x"})
    sc = _assert_tp(HTTPPropagator().extract(headers))
    assert dict(sc.trace_state) == {"v": "x"}


def test_extract_from_str_pairs() -> None:
    sc = _assert_tp(HTTPPropagator().extract([("Content-Type", "x"), ("traceparent", TP)]))
    assert len(sc.trace_state) == 0


def test_extract_from_bytes_pairs_like_asgi() -> None:
    carrier = [(b"host", b"example"), (b"traceparent", TP.encode()), (b"tracestate", b"a=1")]
    sc = _assert_tp(HTTPPropagator().extract(carrier))
    assert dict(sc.trace_state) == {"a": "1"}


def test_extract_bytes_pairs_case_insensitive() -> None:
    _assert_tp(HTTPPropagator().extract([(b"TraceParent", TP.encode())]))


def test_extract_repeated_tracestate_pairs_are_joined_in_order() -> None:
    carrier = [("traceparent", TP), ("tracestate", "a=1"), ("TraceState", "b=2,c=3")]
    sc = _assert_tp(HTTPPropagator().extract(carrier))
    assert sc.trace_state.entries() == (("a", "1"), ("b", "2"), ("c", "3"))


def test_extract_comma_joined_tracestate() -> None:
    sc = _assert_tp(HTTPPropagator().extract({"traceparent": TP, "tracestate": "a=1, b=2"}))
    assert sc.trace_state.entries() == (("a", "1"), ("b", "2"))


def test_extract_missing_traceparent_is_none() -> None:
    assert HTTPPropagator().extract({}) is None
    assert HTTPPropagator().extract({"tracestate": "a=1"}) is None
    assert HTTPPropagator().extract([]) is None


@pytest.mark.parametrize(
    "value",
    ["", "garbage", f"00-{'0' * 32}-{SID}-01", f"ff-{TID}-{SID}-01", TP.upper()],
)
def test_extract_invalid_traceparent_is_none_and_ignores_tracestate(value: str) -> None:
    assert HTTPPropagator().extract({"traceparent": value, "tracestate": "a=1"}) is None


def test_extract_invalid_tracestate_keeps_traceparent() -> None:
    sc = _assert_tp(HTTPPropagator().extract({"traceparent": TP, "tracestate": "BAD=1"}))
    assert len(sc.trace_state) == 0


def test_extract_from_wsgi_environ_adapter() -> None:
    environ = {"HTTP_TRACEPARENT": TP, "HTTP_TRACESTATE": "a=1", "wsgi.input": io.BytesIO()}
    sc = _assert_tp(HTTPPropagator().extract(WSGIEnvironHeaders(environ)))
    assert dict(sc.trace_state) == {"a": "1"}


# ----------------------------------------------------------------- inject


def _ctx(*, sampled: bool = True, state: TraceState | None = None) -> SpanContext:
    return SpanContext.from_hex(TID, SID, sampled=sampled, trace_state=state)


def test_inject_into_dict() -> None:
    carrier: dict[str, str] = {}
    HTTPPropagator().inject(carrier, _ctx(state=TraceState([("a", "1"), ("t@s", "2")])))
    assert carrier == {"traceparent": TP, "tracestate": "a=1,t@s=2"}


def test_inject_without_trace_state_omits_tracestate() -> None:
    carrier: dict[str, str] = {}
    HTTPPropagator().inject(carrier, _ctx())
    assert carrier == {"traceparent": TP}


def test_inject_unsampled_flags() -> None:
    carrier: dict[str, str] = {}
    HTTPPropagator().inject(carrier, _ctx(sampled=False))
    assert carrier["traceparent"] == TP_UNSAMPLED


def test_inject_into_list_of_pairs_appends() -> None:
    carrier: list[tuple[str, str]] = [("accept", "*/*")]
    HTTPPropagator().inject(carrier, _ctx(state=TraceState([("a", "1")])))
    assert carrier == [("accept", "*/*"), ("traceparent", TP), ("tracestate", "a=1")]


def test_inject_overwrites_existing_lowercase_keys() -> None:
    carrier = {"traceparent": "stale", "tracestate": "stale=1"}
    HTTPPropagator().inject(carrier, _ctx())
    assert carrier["traceparent"] == TP


def test_inject_invalid_context_is_noop() -> None:
    carrier: dict[str, str] = {}
    HTTPPropagator().inject(carrier, SpanContext(bytes(16), bytes(8)))
    assert carrier == {}


def test_inject_without_context_or_current_span_is_noop() -> None:
    assert current_span() is None
    carrier: dict[str, str] = {}
    HTTPPropagator().inject(carrier)
    assert carrier == {}


def test_inject_uses_current_span() -> None:
    carrier: dict[str, str] = {}

    def run() -> None:
        use_span(Span("s", _ctx()))
        HTTPPropagator().inject(carrier)

    contextvars.copy_context().run(run)
    assert carrier == {"traceparent": TP}


def test_inject_extract_round_trip() -> None:
    carrier: dict[str, str] = {}
    original = _ctx(state=TraceState([("congo", "t61rcWkgMzE")]))
    HTTPPropagator().inject(carrier, original)
    extracted = HTTPPropagator().extract(carrier)
    assert extracted is not None
    assert (extracted.trace_id, extracted.span_id, extracted.trace_flags) == (
        original.trace_id,
        original.span_id,
        original.trace_flags,
    )
    assert extracted.trace_state == original.trace_state
    assert extracted.is_remote is True


# ---------------------------------------------------- WSGIEnvironHeaders


def _environ() -> dict[str, Any]:
    return {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/",
        "HTTP_TRACEPARENT": TP,
        "HTTP_X_CUSTOM_HEADER": "1",
        "HTTP_HOST": "example",
        "wsgi.input": io.BytesIO(),
        "wsgi.url_scheme": "http",
    }


def test_wsgi_headers_lookup_is_case_insensitive() -> None:
    view = WSGIEnvironHeaders(_environ())
    assert view["traceparent"] == TP
    assert view["Traceparent"] == TP
    assert view["TRACEPARENT"] == TP
    assert view["x-custom-header"] == "1"
    assert view["X-Custom-Header"] == "1"


def test_wsgi_headers_missing_key() -> None:
    view = WSGIEnvironHeaders(_environ())
    with pytest.raises(KeyError):
        view["tracestate"]
    assert view.get("tracestate") is None
    assert "tracestate" not in view


def test_wsgi_headers_iterates_only_http_headers() -> None:
    view = WSGIEnvironHeaders(_environ())
    names = {name.lower() for name in view}
    assert names == {"traceparent", "x-custom-header", "host"}
    assert len(view) == 3


def test_wsgi_headers_include_content_type_and_length() -> None:
    environ = {**_environ(), "CONTENT_TYPE": "text/plain", "CONTENT_LENGTH": "3"}
    view = WSGIEnvironHeaders(environ)
    assert view["content-type"] == "text/plain"
    assert view["Content-Length"] == "3"


def test_wsgi_headers_is_a_live_view_not_a_copy() -> None:
    environ = _environ()
    view = WSGIEnvironHeaders(environ)
    environ["HTTP_TRACESTATE"] = "a=1"
    assert view["tracestate"] == "a=1"


# ---------------------------------------------------------------- composite


class _Fake:
    def __init__(self, name: str, result: SpanContext | None, log: list[str]) -> None:
        self.name = name
        self.result = result
        self.log = log
        self.injected: list[tuple[Any, SpanContext | None]] = []

    def extract(self, carrier: Any) -> SpanContext | None:
        self.log.append(f"extract:{self.name}")
        return self.result

    def inject(self, carrier: Any, span_context: SpanContext | None = None) -> None:
        self.log.append(f"inject:{self.name}")
        self.injected.append((carrier, span_context))


def test_composite_propagators_property_preserves_order() -> None:
    log: list[str] = []
    a, b = _Fake("a", None, log), _Fake("b", None, log)
    composite = CompositePropagator(a, b)
    assert composite.propagators == (a, b)
    assert isinstance(composite, Propagator)


def test_composite_extract_returns_first_hit_in_order() -> None:
    log: list[str] = []
    first = _ctx()
    second = _ctx(sampled=False)
    composite = CompositePropagator(
        _Fake("miss", None, log), _Fake("hit", first, log), _Fake("later", second, log)
    )
    assert composite.extract({}) is first
    assert log == ["extract:miss", "extract:hit"]


def test_composite_extract_all_miss() -> None:
    log: list[str] = []
    assert CompositePropagator(_Fake("a", None, log), _Fake("b", None, log)).extract({}) is None
    assert log == ["extract:a", "extract:b"]


def test_composite_empty() -> None:
    composite = CompositePropagator()
    assert composite.propagators == ()
    assert composite.extract({"traceparent": TP}) is None
    carrier: dict[str, str] = {}
    composite.inject(carrier, _ctx())
    assert carrier == {}


def test_composite_inject_calls_all_in_order_with_same_context() -> None:
    log: list[str] = []
    a, b = _Fake("a", None, log), _Fake("b", None, log)
    carrier: dict[str, str] = {}
    ctx = _ctx()
    CompositePropagator(a, b).inject(carrier, ctx)
    assert log == ["inject:a", "inject:b"]
    assert a.injected == [(carrier, ctx)]
    assert b.injected == [(carrier, ctx)]


def test_composite_with_http_propagator() -> None:
    log: list[str] = []
    composite = CompositePropagator(_Fake("miss", None, log), HTTPPropagator())
    _assert_tp(composite.extract({"traceparent": TP}))
    carrier: dict[str, str] = {}
    composite.inject(carrier, _ctx())
    assert carrier["traceparent"] == TP
