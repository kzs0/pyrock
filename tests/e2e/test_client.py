"""End-to-end: the instrumented ``urllib`` client in ``pyrock.http`` (DESIGN §9.3).

A local recording server (``tests.support`` ``FakeHTTPServer``) sees the real
bytes and headers; spans come from ``pyrock.testing.capture()``.
"""

from __future__ import annotations

import contextlib
import http.cookiejar
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import pyrock
import pyrock.http
from pyrock.testing import capture
from pyrock.trace import SpanContext, SpanKind, TraceState
from tests.e2e._http_helpers import attr_strings, free_port
from tests.support.collectors import FakeHTTPServer, RecordedRequest


class Upstream(FakeHTTPServer):
    """A recording server with a few canned routes."""

    def handle(self, req: RecordedRequest) -> tuple[int, bytes, list[tuple[str, str]]]:
        if req.path == "/json":
            return 200, b'{"name": "a", "n": 1}', [("Content-Type", "application/json")]
        if req.path == "/redirect":
            return 302, b"", [("Location", "/target")]
        if req.path == "/cookie":
            return 200, b"set", [("Set-Cookie", "session=abc; Path=/")]
        if req.path == "/latin":
            return 200, "café".encode("latin-1"), [("Content-Type", "text/plain; charset=latin-1")]
        return 200, b"hello", [("Content-Type", "text/plain; charset=utf-8"), ("X-Upstream", "y")]


@pytest.fixture
def upstream(clean_env: pytest.MonkeyPatch) -> Iterator[Upstream]:
    with Upstream() as server:
        yield server


def _client_spans(t: Any) -> list[Any]:
    return [s for s in t.spans if s.kind is SpanKind.CLIENT]


# ---------------------------------------------------------------- spans


def test_get_creates_client_span_named_http_get(upstream: Upstream) -> None:
    with capture() as t:
        pyrock.http.get(upstream.url + "/plain")
    span = t.spans.one(name="HTTP GET")
    assert span.kind is SpanKind.CLIENT


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("POST", lambda url: pyrock.http.post(url, data=b"x")),
        ("PUT", lambda url: pyrock.http.request("PUT", url, data=b"x")),
        ("DELETE", lambda url: pyrock.http.request("DELETE", url)),
    ],
)
def test_span_name_follows_method(
    upstream: Upstream, method: str, call: Callable[[str], Any]
) -> None:
    with capture() as t:
        call(upstream.url + "/plain")
    span = t.spans.one(name=f"HTTP {method}")
    assert span.attrs["http.method"] == method
    assert upstream.requests[-1].method == method


def test_client_span_attributes(upstream: Upstream) -> None:
    with capture() as t:
        pyrock.http.get(upstream.url + "/plain")
    attrs = t.spans.one(name="HTTP GET").attrs
    assert attrs["http.method"] == "GET"
    assert attrs["http.host"] == f"127.0.0.1:{upstream.port}"
    assert attrs["http.scheme"] == "http"
    assert attrs["http.status_code"] == 200


def test_client_span_is_child_of_active_operation(upstream: Upstream) -> None:
    with capture() as t, pyrock.operation("caller"):
        pyrock.http.get(upstream.url + "/plain")
    caller = t.spans.one(name="caller")
    client = t.spans.one(name="HTTP GET")
    assert client.trace_id == caller.trace_id
    assert client.parent_span_id == caller.span_id


def test_traceparent_injected_matches_client_span(upstream: Upstream) -> None:
    with capture() as t, pyrock.operation("caller"):
        pyrock.http.get(upstream.url + "/plain")
    client = t.spans.one(name="HTTP GET")
    (req,) = upstream.requests
    assert req.header("traceparent") == f"00-{client.trace_id}-{client.span_id}-01"


def test_tracestate_injected_from_active_context(upstream: Upstream) -> None:
    parent = SpanContext.from_hex(
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "00f067aa0ba902b7",
        trace_state=TraceState([("vendor", "abc"), ("tenant@sys", "1")]),
        is_remote=True,
    )
    with capture(), pyrock.operation("consume", remote_parent=parent):
        pyrock.http.get(upstream.url + "/plain")
    (req,) = upstream.requests
    assert req.header("traceparent").startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert req.header("tracestate") == "vendor=abc,tenant@sys=1"


def test_caller_headers_are_sent_and_not_mutated(upstream: Upstream) -> None:
    headers = {"X-Original": "unchanged", "Authorization": "Bearer t0k"}
    before = dict(headers)
    with capture(), pyrock.operation("caller"):
        pyrock.http.get(upstream.url + "/plain", headers=headers)
    assert headers == before
    (req,) = upstream.requests
    assert req.header("x-original") == "unchanged"
    assert req.header("authorization") == "Bearer t0k"
    assert req.header("traceparent")


def test_without_bedrock_no_header_and_no_error(upstream: Upstream) -> None:
    resp = pyrock.http.get(upstream.url + "/plain")
    assert resp.status == 200
    (req,) = upstream.requests
    assert req.header("traceparent") is None
    assert req.header("tracestate") is None


# ---------------------------------------------------------------- status handling


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_error_status_sets_span_error(upstream: Upstream, status: int) -> None:
    upstream.fail_next(status=status)
    with capture() as t:
        pyrock.http.get(upstream.url + "/plain")
    span = t.spans.one(name="HTTP GET")
    assert span.failed
    assert span.attrs["http.status_code"] == status


def test_success_status_is_not_an_error(upstream: Upstream) -> None:
    with capture() as t:
        pyrock.http.get(upstream.url + "/plain")
    assert not t.spans.one(name="HTTP GET").failed


def test_http_error_is_returned_as_response_by_default(upstream: Upstream) -> None:
    upstream.fail_next(status=404, body=b"nope")
    with capture():
        resp = pyrock.http.get(upstream.url + "/plain")
    assert resp.status == 404
    assert not resp.ok
    assert resp.read() == b"nope"


def test_raise_for_status_raises_http_error(upstream: Upstream) -> None:
    upstream.fail_next(status=500)
    with capture() as t, pytest.raises(urllib.error.HTTPError) as info:
        pyrock.http.get(upstream.url + "/plain", raise_for_status=True)
    assert info.value.code == 500
    assert t.spans.one(name="HTTP GET").failed


def test_connection_refused_raises_and_records_error() -> None:
    with capture() as t, pytest.raises(urllib.error.URLError):
        pyrock.http.get(f"http://127.0.0.1:{free_port()}/", timeout=2)
    assert t.spans.one(name="HTTP GET").failed


def test_timeout_is_applied(upstream: Upstream) -> None:
    upstream.hang()
    try:
        with capture() as t, pytest.raises((TimeoutError, urllib.error.URLError)):
            pyrock.http.get(upstream.url + "/slow", timeout=0.2)
    finally:
        upstream.release()
    assert t.spans.one(name="HTTP GET").failed


# ---------------------------------------------------------------- Response API


def test_response_api(upstream: Upstream) -> None:
    with capture():
        resp = pyrock.http.get(upstream.url + "/plain")
    assert resp.status == 200
    assert resp.reason == "OK"
    assert resp.ok
    assert resp.url == upstream.url + "/plain"
    assert resp.headers["X-Upstream"] == "y"
    assert resp.read() == b"hello"
    resp.close()


def test_response_text_uses_charset(upstream: Upstream) -> None:
    with capture():
        resp = pyrock.http.get(upstream.url + "/latin")
    assert resp.text == "café"


def test_response_json(upstream: Upstream) -> None:
    with capture():
        resp = pyrock.http.get(upstream.url + "/json")
    assert resp.json() == {"name": "a", "n": 1}


def test_response_partial_read(upstream: Upstream) -> None:
    with capture():
        resp = pyrock.http.get(upstream.url + "/plain")
    assert resp.read(2) == b"he"
    assert resp.read() == b"llo"


def test_response_is_a_context_manager(upstream: Upstream) -> None:
    with capture():
        resp = pyrock.http.get(upstream.url + "/plain")
        with resp as entered:
            assert entered is resp
            assert entered.read() == b"hello"


# ---------------------------------------------------------------- request bodies


def test_post_json_body_and_content_type(upstream: Upstream) -> None:
    with capture():
        pyrock.http.post(upstream.url + "/plain", json={"name": "a", "n": [1, 2]})
    (req,) = upstream.requests
    assert req.method == "POST"
    assert json.loads(req.body) == {"name": "a", "n": [1, 2]}
    assert req.header("content-type").startswith("application/json")


def test_post_raw_data_and_custom_headers(upstream: Upstream) -> None:
    with capture():
        pyrock.http.post(
            upstream.url + "/plain",
            data=b"\x00raw-bytes",
            headers={"Content-Type": "application/octet-stream"},
        )
    (req,) = upstream.requests
    assert req.body == b"\x00raw-bytes"
    assert req.header("content-type") == "application/octet-stream"


def test_request_put_with_data(upstream: Upstream) -> None:
    with capture():
        resp = pyrock.http.request("PUT", upstream.url + "/plain", data=b"payload")
    assert resp.status == 200
    (req,) = upstream.requests
    assert (req.method, req.body) == ("PUT", b"payload")


# ---------------------------------------------------------------- URL sanitizing


def test_query_fragment_and_path_never_in_span(upstream: Upstream) -> None:
    url = upstream.url + "/pathsecret-42/items?token=querysecret&a=1#fragsecret"
    with capture(log_level="DEBUG") as t:
        pyrock.http.get(url)
    (req,) = upstream.requests
    assert req.path == "/pathsecret-42/items"
    assert req.query == "token=querysecret&a=1"
    span = t.spans.one(name="HTTP GET")
    for secret in ("querysecret", "fragsecret", "pathsecret"):
        assert not any(secret in v for v in attr_strings(span)), secret
        assert secret not in json.dumps(list(t.logs), default=str), secret


def test_credentials_never_in_span_or_logs(upstream: Upstream) -> None:
    url = f"http://creduser:credpass@127.0.0.1:{upstream.port}/plain"
    with capture(log_level="DEBUG") as t:
        # urllib may refuse userinfo in the netloc; the span must still be clean.
        with contextlib.suppress(OSError):
            pyrock.http.get(url, timeout=2)
    for span in t.spans:
        for secret in ("creduser", "credpass"):
            assert not any(secret in v for v in attr_strings(span)), secret
            assert secret not in span.status_message
    dumped = json.dumps(list(t.logs), default=str)
    assert "creduser" not in dumped
    assert "credpass" not in dumped


def test_http_url_attr_is_sanitized(upstream: Upstream) -> None:
    with capture() as t:
        pyrock.http.get(upstream.url + "/p?q=1#f")
    attrs = t.spans.one(name="HTTP GET").attrs
    if "http.url" in attrs:
        assert attrs["http.url"].startswith(f"http://127.0.0.1:{upstream.port}")
        assert "?" not in attrs["http.url"]
        assert "#" not in attrs["http.url"]


# ---------------------------------------------------------------- openers and handlers


def test_build_opener_keeps_other_handlers(upstream: Upstream) -> None:
    jar = http.cookiejar.CookieJar()
    cookies = urllib.request.HTTPCookieProcessor(jar)
    opener = pyrock.http.build_opener(cookies)
    assert cookies in opener.handlers
    assert any(isinstance(h, pyrock.http.TracingHandler) for h in opener.handlers)
    with capture(), pyrock.operation("caller"):
        opener.open(upstream.url + "/cookie", timeout=5).read()
        opener.open(upstream.url + "/plain", timeout=5).read()
    first, second = upstream.requests
    assert first.header("traceparent")
    assert second.header("cookie") == "session=abc"
    assert second.header("traceparent")


def test_request_accepts_a_custom_opener(upstream: Upstream) -> None:
    class Stamp(urllib.request.BaseHandler):
        def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
            req.add_header("X-Stamp", "1")
            return req

    opener = pyrock.http.build_opener(Stamp())
    with capture() as t:
        pyrock.http.get(upstream.url + "/plain", opener=opener)
    (req,) = upstream.requests
    assert req.header("x-stamp") == "1"
    assert req.header("traceparent")
    assert len(t.spans.find(name="HTTP GET")) == 1


def test_tracing_handler_on_plain_urllib_opener(upstream: Upstream) -> None:
    opener = urllib.request.build_opener(pyrock.http.TracingHandler())
    with (
        capture() as t,
        pyrock.operation("caller"),
        opener.open(upstream.url + "/plain", timeout=5) as resp,
    ):
        assert resp.read() == b"hello"
    client = t.spans.one(name="HTTP GET")
    assert client.kind is SpanKind.CLIENT
    assert client.parent_span_id == t.spans.one(name="caller").span_id
    (req,) = upstream.requests
    assert req.header("traceparent") == f"00-{client.trace_id}-{client.span_id}-01"


def test_tracing_handler_marks_http_error(upstream: Upstream) -> None:
    upstream.fail_next(status=502)
    opener = urllib.request.build_opener(pyrock.http.TracingHandler())
    with capture() as t, pytest.raises(urllib.error.HTTPError):
        opener.open(upstream.url + "/plain", timeout=5)
    span = t.spans.one(name="HTTP GET")
    assert span.failed
    assert span.attrs["http.status_code"] == 502


def test_redirects_keep_the_same_trace(upstream: Upstream) -> None:
    with capture() as t, pyrock.operation("caller"):
        resp = pyrock.http.get(upstream.url + "/redirect")
    assert resp.status == 200
    assert resp.read() == b"hello"
    caller = t.spans.one(name="caller")
    paths = [r.path for r in upstream.requests]
    assert paths == ["/redirect", "/target"]
    for req in upstream.requests:
        assert req.header("traceparent").split("-")[1] == caller.trace_id
    for span in _client_spans(t):
        assert span.trace_id == caller.trace_id


# ---------------------------------------------------------------- inject_headers


def test_inject_headers_writes_current_context_and_returns_same_dict() -> None:
    with capture(), pyrock.operation("caller") as op:
        headers: dict[str, str] = {"accept": "*/*"}
        out = pyrock.inject_headers(headers)
    assert out is headers
    assert headers["traceparent"] == f"00-{op.trace_id}-{op.span_id}-01"
    assert headers["accept"] == "*/*"


def test_pyrock_http_reexports_inject_headers() -> None:
    assert pyrock.http.inject_headers is pyrock.inject_headers


def test_inject_headers_with_explicit_span_context() -> None:
    sc = SpanContext.from_hex("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", sampled=False)
    out = pyrock.inject_headers({}, sc)
    assert out["traceparent"] == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"


def test_inject_headers_without_active_span_is_a_noop(clean_env: pytest.MonkeyPatch) -> None:
    with capture():
        headers: dict[str, str] = {"accept": "*/*"}
        out = pyrock.inject_headers(headers)
    assert out is headers
    assert headers == {"accept": "*/*"}


def test_inject_headers_without_bedrock_is_a_noop() -> None:
    headers: dict[str, str] = {}
    assert pyrock.inject_headers(headers) == {}
