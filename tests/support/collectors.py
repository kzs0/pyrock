"""Fake network peers for black-box tests. Every server binds ``127.0.0.1:0``.

* :class:`OTLPHTTPCollector`: OTLP/HTTP collector. ``POST /v1/traces`` (JSON,
  or protobuf when the content type says so), ``POST /v1/profiles``
  (protobuf), and any other ``POST`` (e.g. ``/v1/metrics`` pushes) are
  recorded with their headers.
* :class:`MetricsPushSink`: the same server, with helpers for Prometheus
  pushes (``/v1/metrics``).
* :class:`GRPCCollector`: a real grpcio OTLP ``TraceService`` (h2c), skipping
  the test when grpcio/opentelemetry-proto are unavailable.
* :class:`FakeMetadataServer`: AWS IMDSv2 and GCP metadata endpoints.

Failure injection (HTTP fakes): :meth:`FakeHTTPServer.fail_next` queues
responses (status, body, headers, optional delay) consumed in order by the
next matching requests; :meth:`FakeHTTPServer.set_default_status` changes the
steady-state status; :meth:`FakeHTTPServer.hang` makes requests block until
:meth:`FakeHTTPServer.release` (or close).

All servers are context managers and also have :meth:`close`; the pytest
fixtures in ``tests/conftest.py`` close them for you.
"""

from __future__ import annotations

import collections
import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from tests.support.otlp_decode import decode_trace_request_pb, flatten_spans

__all__ = [
    "FakeHTTPServer",
    "FakeMetadataServer",
    "GRPCCollector",
    "MetricsPushSink",
    "OTLPHTTPCollector",
    "RecordedRequest",
    "ScriptedResponse",
    "wait_until",
]

DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True)
class RecordedRequest:
    """One request received by a fake server."""

    method: str
    path: str
    """Path without the query string."""
    query: str
    headers: dict[str, str]
    """Header names lower-cased; repeated headers joined with ``", "``."""
    raw_headers: list[tuple[str, str]]
    body: bytes
    received_at: float = field(default_factory=time.monotonic)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass(frozen=True)
class ScriptedResponse:
    """A queued response for failure injection."""

    status: int = 503
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    delay: float = 0.0
    path: str | None = None
    """Only requests to this path consume it (``None``: any path)."""


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # silence stderr
        pass

    def _handle(self) -> None:
        owner = self.server.owner
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        raw = [(k, v) for k, v in self.headers.items()]
        joined: dict[str, str] = {}
        for k, v in raw:
            key = k.lower()
            joined[key] = f"{joined[key]}, {v}" if key in joined else v
        req = RecordedRequest(self.command, parts.path, parts.query, joined, raw, body)
        status, resp_body, headers = owner._respond(req)
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _handle


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    owner: FakeHTTPServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Clients that time out or hang up (timeout tests) are expected; stay quiet.
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError | TimeoutError):
            return
        super().handle_error(request, client_address)


class FakeHTTPServer:
    """A recording HTTP server on a daemon thread. Subclasses customize :meth:`handle`."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._requests: list[RecordedRequest] = []
        self._script: collections.deque[ScriptedResponse] = collections.deque()
        self._default_status = 200
        self._release = threading.Event()
        self._release.set()
        self._closing = threading.Event()
        self._closed = False
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"{type(self).__name__}-serve",
            daemon=True,
        )
        self._thread.start()

    # -- addressing -------------------------------------------------------

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        """Base URL, e.g. ``http://127.0.0.1:54321`` (no trailing slash)."""
        return f"http://127.0.0.1:{self.port}"

    # -- failure injection ----------------------------------------------

    def fail_next(
        self,
        n: int = 1,
        *,
        status: int = 503,
        body: bytes = b"",
        headers: Iterable[tuple[str, str]] = (),
        delay: float = 0.0,
        path: str | None = None,
    ) -> None:
        """Answer the next ``n`` matching requests with ``status`` (recorded as usual)."""
        with self._cond:
            for _ in range(n):
                self._script.append(ScriptedResponse(status, body, tuple(headers), delay, path))

    def set_default_status(self, status: int) -> None:
        """Status for requests with no scripted response (default 200)."""
        with self._cond:
            self._default_status = status

    def hang(self) -> None:
        """Make every subsequent request block (after being recorded) until :meth:`release`."""
        self._release.clear()

    def release(self) -> None:
        self._release.set()

    # -- recording -------------------------------------------------------

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._cond:
            return list(self._requests)

    def requests_to(self, path: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.path == path]

    def wait_for_requests(
        self, n: int, *, path: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> list[RecordedRequest]:
        """Block until at least ``n`` (matching) requests arrived; ``AssertionError`` on timeout."""

        def matching() -> list[RecordedRequest]:
            return [r for r in self._requests if path is None or r.path == path]

        with self._cond:
            if not self._cond.wait_for(lambda: len(matching()) >= n, timeout=timeout):
                got = len(matching())
                raise AssertionError(
                    f"timed out after {timeout}s waiting for {n} request(s)"
                    f"{' to ' + path if path else ''}; got {got}"
                )
            return matching()

    def clear(self) -> None:
        with self._cond:
            self._requests.clear()

    # -- request handling --------------------------------------------------

    def handle(self, req: RecordedRequest) -> tuple[int, bytes, list[tuple[str, str]]]:
        """Default steady-state response. Subclasses override."""
        return self._default_status, b"", []

    def _respond(self, req: RecordedRequest) -> tuple[int, bytes, list[tuple[str, str]]]:
        scripted: ScriptedResponse | None = None
        with self._cond:
            for i, item in enumerate(self._script):
                if item.path is None or item.path == req.path:
                    scripted = item
                    del self._script[i]
                    break
            self._requests.append(req)
            self._cond.notify_all()
        if not self._release.is_set():
            self._release.wait(timeout=60)
        if scripted is not None:
            if scripted.delay:
                self._closing.wait(scripted.delay)  # cut short by close()
            return scripted.status, scripted.body, list(scripted.headers)
        with self._cond:
            default = self._default_status
        if default != 200:
            return default, b"", []
        return self.handle(req)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing.set()
        self._release.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> FakeHTTPServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class OTLPHTTPCollector(FakeHTTPServer):
    """An OTLP/HTTP collector for traces (JSON or protobuf) and profiles (protobuf)."""

    TRACES_PATH = "/v1/traces"
    PROFILES_PATH = "/v1/profiles"
    METRICS_PATH = "/v1/metrics"

    @property
    def traces_url(self) -> str:
        return self.url + self.TRACES_PATH

    @property
    def profiles_url(self) -> str:
        return self.url + self.PROFILES_PATH

    @property
    def metrics_url(self) -> str:
        return self.url + self.METRICS_PATH

    def handle(self, req: RecordedRequest) -> tuple[int, bytes, list[tuple[str, str]]]:
        if req.method != "POST":
            return 405, b"", []
        if req.path == self.TRACES_PATH:
            if (req.header("content-type") or "").startswith("application/json"):
                return 200, b"{}", [("Content-Type", "application/json")]
            return 200, b"", [("Content-Type", "application/x-protobuf")]
        return 200, b"", []

    # -- traces ----------------------------------------------------------

    def trace_requests(self) -> list[RecordedRequest]:
        return self.requests_to(self.TRACES_PATH)

    @staticmethod
    def _decode_traces(req: RecordedRequest) -> list[dict[str, Any]]:
        ctype = req.header("content-type") or ""
        if ctype.startswith("application/json"):
            return flatten_spans(req.json())
        return decode_trace_request_pb(req.body)

    def spans(self) -> list[dict[str, Any]]:
        """Every span received so far, flattened (see :mod:`tests.support.otlp_decode`)."""
        out: list[dict[str, Any]] = []
        for req in self.trace_requests():
            out.extend(self._decode_traces(req))
        return out

    def span_names(self) -> list[str]:
        return [s["name"] for s in self.spans()]

    def span(self, name: str) -> dict[str, Any]:
        """The single span called ``name`` (``AssertionError`` if 0 or >1)."""
        found = [s for s in self.spans() if s["name"] == name]
        if len(found) != 1:
            raise AssertionError(
                f"expected one span {name!r}, got {len(found)}: {self.span_names()}"
            )
        return found[0]

    def wait_for_spans(self, n: int, *, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
        """Block until at least ``n`` spans arrived and return all spans.

        Raises ``AssertionError`` on timeout.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                spans: list[dict[str, Any]] = []
                for req in self._requests:
                    if req.path == self.TRACES_PATH:
                        spans.extend(self._decode_traces(req))
                if len(spans) >= n:
                    return spans
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"timed out after {timeout}s waiting for {n} span(s); "
                        f"got {len(spans)}: {[s['name'] for s in spans]}"
                    )
                self._cond.wait(remaining)

    # -- profiles --------------------------------------------------------

    def profile_requests(self) -> list[RecordedRequest]:
        return self.requests_to(self.PROFILES_PATH)

    def wait_for_profiles(
        self, n: int, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[RecordedRequest]:
        return self.wait_for_requests(n, path=self.PROFILES_PATH, timeout=timeout)

    # -- metrics pushes (when used as a cloud endpoint) -------------------

    def metric_pushes(self) -> list[RecordedRequest]:
        return self.requests_to(self.METRICS_PATH)


class MetricsPushSink(OTLPHTTPCollector):
    """Receives Prometheus text pushes at ``/v1/metrics`` (any path is recorded)."""

    @property
    def endpoint(self) -> str:
        """Base URL to configure as a cloud ``endpoint`` (pushes go to ``/v1/metrics``)."""
        return self.url

    def pushes(self) -> list[RecordedRequest]:
        return self.metric_pushes()

    def texts(self) -> list[str]:
        return [r.text for r in self.pushes()]

    def wait_for_pushes(self, n: int, *, timeout: float = DEFAULT_TIMEOUT) -> list[RecordedRequest]:
        return self.wait_for_requests(n, path=self.METRICS_PATH, timeout=timeout)


class FakeMetadataServer(FakeHTTPServer):
    """AWS IMDSv2 and GCP metadata endpoints.

    AWS: ``PUT /latest/api/token`` (needs ``X-aws-ec2-metadata-token-ttl-seconds``)
    returns :attr:`token`; ``GET /latest/meta-data/instance-id`` and
    ``/latest/meta-data/placement/region`` need ``X-aws-ec2-metadata-token``
    (401 otherwise; with ``require_token=False`` IMDSv1 GETs work too).

    GCP: ``GET /computeMetadata/v1/instance/id`` and ``/instance/zone`` need
    ``Metadata-Flavor: Google`` (403 otherwise) and answer with the same header.

    ``provider`` selects which side answers (``"aws"``, ``"gcp"``, ``"both"``,
    or ``"none"``); the other answers 404. Use :meth:`hang` to test timeouts.
    """

    def __init__(
        self,
        *,
        provider: str = "aws",
        instance_id: str = "i-0123456789abcdef0",
        region: str = "us-west-2",
        gcp_instance_id: str = "1234567890123456789",
        gcp_zone: str = "projects/123456/zones/us-central1-a",
        token: str = "fake-imds-token",
        require_token: bool = True,
    ) -> None:
        if provider not in ("aws", "gcp", "both", "none"):
            raise ValueError(f"unknown provider {provider!r}")
        self.provider = provider
        self.instance_id = instance_id
        self.region = region
        self.gcp_instance_id = gcp_instance_id
        self.gcp_zone = gcp_zone
        self.token = token
        self.require_token = require_token
        super().__init__()

    @property
    def aws_base(self) -> str:
        return self.url

    @property
    def gcp_base(self) -> str:
        return self.url

    def environ(self) -> dict[str, str]:
        """Env vars pointing pyrock's detectors at this server (unused side disabled)."""
        aws = self.url if self.provider in ("aws", "both") else ""
        gcp = self.url if self.provider in ("gcp", "both") else ""
        return {"PYROCK_AWS_METADATA_ENDPOINT": aws, "PYROCK_GCP_METADATA_ENDPOINT": gcp}

    def handle(self, req: RecordedRequest) -> tuple[int, bytes, list[tuple[str, str]]]:
        text = [("Content-Type", "text/plain")]
        aws = self.provider in ("aws", "both")
        gcp = self.provider in ("gcp", "both")
        if aws and req.path == "/latest/api/token":
            if req.method != "PUT":
                return 405, b"", text
            if not req.header("x-aws-ec2-metadata-token-ttl-seconds"):
                return 400, b"missing ttl", text
            return 200, self.token.encode(), text
        if aws and req.path.startswith("/latest/meta-data/"):
            if req.method != "GET":
                return 405, b"", text
            if self.require_token and req.header("x-aws-ec2-metadata-token") != self.token:
                return 401, b"", text
            values = {
                "/latest/meta-data/instance-id": self.instance_id,
                "/latest/meta-data/placement/region": self.region,
            }
            if req.path in values:
                return 200, values[req.path].encode(), text
            return 404, b"", text
        if gcp and req.path.startswith("/computeMetadata/v1/"):
            if req.header("metadata-flavor") != "Google":
                return 403, b"", text
            flavor = [*text, ("Metadata-Flavor", "Google")]
            values = {
                "/computeMetadata/v1/instance/id": self.gcp_instance_id,
                "/computeMetadata/v1/instance/zone": self.gcp_zone,
            }
            if req.path in values:
                return 200, values[req.path].encode(), flavor
            return 404, b"", flavor
        return 404, b"", text


class GRPCCollector:
    """A real grpcio OTLP ``TraceService`` on ``127.0.0.1:0`` (plaintext h2c).

    Skips the calling test when ``grpcio`` or ``opentelemetry-proto`` is not
    installed (they are in the ``compat`` dependency group).
    """

    def __init__(self, *, max_workers: int = 4) -> None:
        import importlib

        import pytest

        try:
            self._grpc: Any = importlib.import_module("grpc")
            self._pb: Any = importlib.import_module(
                "opentelemetry.proto.collector.trace.v1.trace_service_pb2"
            )
            self._pb_grpc: Any = importlib.import_module(
                "opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc"
            )
        except ImportError as exc:
            pytest.skip(f"compat dependency unavailable: {exc}")

        from concurrent.futures import ThreadPoolExecutor

        self._cond = threading.Condition()
        self._calls: list[tuple[Any, dict[str, str]]] = []
        self._failures: collections.deque[tuple[Any, str]] = collections.deque()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="grpc-fake")
        self._server = self._grpc.server(self._executor)
        collector = self

        class _Servicer(self._pb_grpc.TraceServiceServicer):  # type: ignore[misc,name-defined]
            def Export(self, request: Any, context: Any) -> Any:
                metadata = dict(context.invocation_metadata())
                with collector._cond:
                    collector._calls.append((request, metadata))
                    failure = collector._failures.popleft() if collector._failures else None
                    collector._cond.notify_all()
                if failure is not None:
                    context.abort(failure[0], failure[1])
                return collector._pb.ExportTraceServiceResponse()

        self._pb_grpc.add_TraceServiceServicer_to_server(_Servicer(), self._server)
        self.port: int = self._server.add_insecure_port("127.0.0.1:0")
        self._server.start()
        self._closed = False

    @property
    def endpoint(self) -> str:
        """``host:port`` (what pyrock treats as an OTLP/gRPC target)."""
        return f"127.0.0.1:{self.port}"

    def fail_next(
        self, n: int = 1, *, code: str = "UNAVAILABLE", details: str = "injected"
    ) -> None:
        """Abort the next ``n`` calls with ``grpc.StatusCode[code]``."""
        status = self._grpc.StatusCode[code]
        with self._cond:
            for _ in range(n):
                self._failures.append((status, details))

    @property
    def metadata(self) -> list[dict[str, str]]:
        """Invocation metadata of each call (lower-case keys)."""
        with self._cond:
            return [md for _, md in self._calls]

    @property
    def requests(self) -> list[Any]:
        """The ``ExportTraceServiceRequest`` messages received."""
        with self._cond:
            return [req for req, _ in self._calls]

    def spans(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for req in self.requests:
            out.extend(decode_trace_request_pb(req.SerializeToString()))
        return out

    def wait_for_calls(self, n: int, *, timeout: float = DEFAULT_TIMEOUT) -> int:
        with self._cond:
            if not self._cond.wait_for(lambda: len(self._calls) >= n, timeout=timeout):
                got = len(self._calls)
                raise AssertionError(
                    f"timed out after {timeout}s waiting for {n} gRPC call(s); got {got}"
                )
            return len(self._calls)

    def wait_for_spans(self, n: int, *, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                spans: list[dict[str, Any]] = []
                for req, _ in self._calls:
                    spans.extend(decode_trace_request_pb(req.SerializeToString()))
                if len(spans) >= n:
                    return spans
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"timed out after {timeout}s waiting for {n} span(s); got {len(spans)}"
                    )
                self._cond.wait(remaining)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.stop(grace=None).wait(5)
        self._executor.shutdown(wait=True)

    def __enter__(self) -> GRPCCollector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def wait_until(
    predicate: Callable[[], bool], *, timeout: float = DEFAULT_TIMEOUT, interval: float = 0.01
) -> None:
    """Poll ``predicate`` until true; ``AssertionError`` on timeout (for state with no event)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        threading.Event().wait(interval)
