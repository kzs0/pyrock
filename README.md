# pyrock

pyrock is the Python port of [Bedrock](https://github.com/kzs0/bedrock): one
opinionated observability library for CPython that gives you traces (OTLP over
HTTP/JSON or gRPC), Prometheus metrics, continuous profiles (OTLP Profiles) and
structured, trace-correlated logs, all built around *operations* (named units
of work). It uses only the standard library. A Go service and a Python service
behind the same collector produce the same metric names, label sets, span
attributes, log shape and `BEDROCK_*` configuration, so dashboards and alerts
carry over.

```python
import pyrock

with pyrock.init(service="users-api"):
    with pyrock.operation("process_user", attrs={"user_id": "123"},
                          metric_labels=["status"]) as op:
        op.register(status="active")
        do_work()          # an exception escaping here marks the operation as failed
```

> **Status:** alpha (`0.1.0`). The API follows [DESIGN.md](DESIGN.md);
> minor versions may change behaviour until 1.0, and every change is recorded
> in [CHANGELOG.md](CHANGELOG.md).

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Core concepts](#core-concepts)
- [API tour](#api-tour)
- [Configuration](#configuration)
- [Privacy](#privacy)
- [Observability server](#observability-server)
- [Profiles](#profiles)
- [Cloud backend](#cloud-backend)
- [Environment detection](#environment-detection)
- [Differences from Go Bedrock](#differences-from-go-bedrock)
- [Compatibility](#compatibility)
- [Development](#development)
- [License](#license)

## Install

```bash
pip install pyrock
```

- CPython **3.11 or newer**, including the free-threaded build (3.14t, with the
  GIL disabled). Every shared structure is guarded by a lock; nothing relies on
  the GIL.
- **Zero runtime dependencies.** The wheel is pure Python. The WSGI/ASGI
  middleware, the HTTP client and the gRPC exporter are built on stdlib and
  protocol interfaces, never on frameworks. Framework glue lives in
  [`examples/recipes/`](examples/recipes) for you to copy.
- `import pyrock` is cheap. Profiling, cloud wiring, gRPC, the server, the HTTP
  client and the middleware load on first use.

## Quick start

### A WSGI service (Flask, Django, Falcon, Pyramid, ...)

```python
import pyrock
from pyrock.wsgi import Middleware

pyrock.init(service="billing")        # reads BEDROCK_* first; keywords override
app = Middleware(app)                 # Flask: app.wsgi_app = Middleware(app.wsgi_app)
```

### An ASGI service (FastAPI, Starlette, Litestar, ...)

```python
import pyrock
from pyrock.asgi import Middleware

bedrock = pyrock.Bedrock(service="users-api")    # live, not activated
app = Middleware(app, bedrock=bedrock)           # FastAPI: app.add_middleware(Middleware, bedrock=bedrock)
# On shutdown: await bedrock.ashutdown(), or use the Bedrock as the lifespan context manager.
```

Each request becomes a `SERVER` operation named `http.request`, with the metrics
`http_request_count`, `_successes`, `_failures` and `_duration_ms` labelled by
`http_method`, `http_route` and `http_status_code`. Incoming `traceparent` and
`tracestate` headers continue the caller's trace.

### Operations in your code

```python
import pyrock

@pyrock.operation("get_user", metric_labels=["tier"])
def get_user(user_id: str) -> User:
    user = db.load(user_id)
    pyrock.register(tier=user.tier)            # on the innermost operation or step
    pyrock.info("user loaded", user_id=user_id)  # carries trace_id/span_id
    return user
```

Runnable versions are in [`examples/`](examples): `http_server.py` (wsgiref),
`asgi_app.py` (raw ASGI) and `worker.py` (a background source with a thread
pool). They need nothing but pyrock.

## Core concepts

### Operations

An operation is the main unit of instrumentation. Each one creates a span,
records four metrics (`<name>_count`, `<name>_successes`, `<name>_failures` and
the `<name>_duration_ms` histogram), accepts attributes, and succeeds unless
something marks it failed. Use it as a context manager or as a decorator on
functions, coroutines, generators and async generators:

```python
with pyrock.operation("process_user", attrs={"user_id": uid}, metric_labels=["status"]) as op:
    op.register(status="active")             # keyword attributes
    op.register({"user.tier": "gold"})       # a mapping for dotted keys
    op.event("cache_miss", key="u:123")      # a span event, recorded immediately
    if not valid:
        op.fail(ValueError("bad user"))      # a handled failure; no raise needed
```

`Operation` also has `succeed()` (clear an earlier failure, for retry loops),
`end(error=..., success=...)` and `done()`, plus read-only `name`, `trace_id`,
`span_id`, `span_context` and `failed`. For callbacks and hand-offs that can't
use `with`, `pyrock.start_operation(...)` starts one without activating it:
call `op.activate()`, then `op.deactivate(activation)` and `op.end(...)`.

Metric families use the base name with dots turned into underscores:
`http.request` gives `http_request_count`.

### Success by default, and exceptions

- **No exception:** success, unless `fail()` was called.
- **An `Exception` escapes:** failure. The error attributes (`error`,
  `error.type`, `error.fingerprint`, `error.chain` for chained exceptions) are
  registered and the exception is re-raised. pyrock never swallows exceptions.
- **`expected=(KeyError, ...)`:** listed exceptions are recorded as a span
  event and leave the outcome a success, even when they escape.
- **`BaseException`:** `asyncio.CancelledError` and `KeyboardInterrupt` are
  failures. `GeneratorExit` and `SystemExit(0)` are not.
- **`ExceptionGroup`:** a failure, summarised in `error.group.count` and
  `error.types`.

### Steps

Steps are lightweight spans for helper functions. Their attributes go to the
parent operation, and they record no metrics of their own.

```python
@pyrock.step("parse_payload")
def parse(raw: bytes) -> Payload:
    pyrock.register(bytes=len(raw))    # on the step span and on the parent operation
    ...
```

An exception escaping a step marks the step span as an error. The operation
fails only if the exception also escapes the operation. A step with no active
operation still creates a span if a trace is active, and is a noop otherwise.

### Sources

Sources represent long-running processes. They prefix the names of operations
opened beneath them, share their attributes and labels with them, and record
aggregate metrics:

```python
with pyrock.source("background.worker", attrs={"worker.type": "async"},
                   metric_labels=["worker.type"]) as src:
    src.sum("jobs_processed", 1)            # counter   background_worker_jobs_processed
    src.gauge("queue_depth", q.qsize())     # gauge     background_worker_queue_depth
    src.histogram("latency_ms", 52.3)       # histogram background_worker_latency_ms
    with pyrock.operation("process"):       # named background.worker.process
        ...
```

### Metric labels and cardinality

Only keys declared in `metric_labels=` become metric labels; a declared label
with no value is `"_"`. Everything else is logged and traced but never turns
into a series, so request IDs and user IDs are safe as attributes. Label keys
have dots replaced (`http.method` becomes `http_method`). Static attributes
are added as labels to every metric, except infrastructure keys (`host.*`,
`process.*`, `container.*`, `k8s.*`, `cloud.*`). Past `metric_series_limit`
new series are dropped and counted; past `metric_family_limit` registration
raises.

### `no_trace`

`no_trace=True` on an operation or step turns off span creation for that scope
and everything beneath it. Metrics are still recorded, and logs still carry the
enclosing trace IDs. Use it on hot paths.

### Numbering

A second `operation("fetch")` or `step("fetch")` under the same parent
operation or step gets the span name `fetch[1]`, then `fetch[2]`, and so on.
Root operations and direct children of a source are never numbered, so span
names stay bounded in loops. Numbering applies to span names only; metric
names never change.

### Context, asyncio and threads

There is no process-wide default instance. The active instance, operation,
step, source and the `no_trace` flag live in `contextvars`:

- **asyncio** needs nothing: tasks copy the context when created, and
  `asyncio.to_thread` copies it too.
- **Threads** start with an empty context on most interpreters, so work
  submitted to a plain `ThreadPoolExecutor` sees the noop. Carry the context
  explicitly:

  ```python
  pool.submit(pyrock.bind(process, item))           # wrap one callable

  with pyrock.ContextThreadPoolExecutor(max_workers=8) as pool:
      pool.map(process, items)                      # copies context on every submit

  pyrock.Thread(target=consume).start()             # a threading.Thread that copies context
  pyrock.Thread(target=consume, context=ctx)        # explicit context, as on 3.14
  ```

- **Python 3.14t:** when `sys.flags.thread_inherit_context` is set (the
  default on free-threaded 3.14), new threads inherit the creator's context,
  and with it the active instance. That is the interpreter's behaviour and
  pyrock does not fight it. Run a target through `contextvars.Context().run`
  when you need an empty context.
- **Server threads:** `wsgi.Middleware`, `asgi.Middleware` and
  `http.instrument_http_server` capture the instance when they are built
  (`bedrock=`, or `pyrock.current()` at that point) and activate it for every
  request, so handler code on server worker threads is instrumented. If they
  are built before any instance exists, they resolve `pyrock.current()` per
  request instead.
- **`fork()`:** a fork hook resets queues and metric series in the child and
  restarts the export, push and profile threads; it never starts the
  observability server there (the parent owns the port). With pre-fork servers
  (gunicorn, uWSGI) call `pyrock.init()` in the worker's post-fork hook.

### Lifecycle

`pyrock.init(...)` builds, starts and activates a `Bedrock`. It is a sync and
async context manager: leaving the block shuts it down (flush spans, final
metrics push, stop profiles and the server, restore logging) and restores the
previously active instance. Without `with`, call `bedrock.shutdown(timeout=30)`
or `await bedrock.ashutdown()`. Shutdown is idempotent and thread-safe. pyrock
registers no `atexit` handler unless you pass `shutdown_atexit=True`.

`pyrock.Bedrock(...)` builds a fully live instance (exporters, server and log
installation start) without activating it, for libraries, tests and explicit
wiring. Every module-level helper also exists as a method
(`bedrock.operation(...)`, `bedrock.counter(...)`, ...), and `bedrock=` on
`operation`, `step` and `source` pins an instance (which is then active for
that scope).

### Noop

When no instance is active, every helper returns a preallocated noop
(operation, step, source, counter, gauge, histogram) and does nothing.
Decorators always wrap and look up the active instance at call time, so
functions decorated at import time, before `init()`, are instrumented once an
instance is active. The noop path costs one `ContextVar.get()` plus a call.
Library code can instrument freely.

## API tour

### Logging

pyrock builds on stdlib `logging`. By default `init()` installs a JSON handler
on the root logger, so your existing `logging.getLogger(__name__)` calls, and
those of your libraries, become structured and trace-correlated:

```python
log = logging.getLogger(__name__)
log.info("user loaded", extra={"user.id": uid})   # extra keys become fields
log.exception("boom")                             # exc_info becomes error.* fields

pyrock.debug("cache warm", entries=1200)
pyrock.info("processing request", user_id="123")
pyrock.warning("slow upstream", timeout=timedelta(seconds=5))   # pyrock.warn is an alias
pyrock.error("job failed", exc)                                  # a positional exception: error.* fields
pyrock.log(logging.INFO, "custom", {"http.route": "/users"})
```

```json
{"time":"2026-11-05T12:00:00.123Z","level":"INFO","msg":"processing request",
 "service":"users-api","env":"production","user_id":"123",
 "trace_id":"0af7651916cd43dd8448eb211c80319c","span_id":"b7ad6b7169203331",
 "logger":"myapp.users"}
```

- `log_install="root"` (default) owns the root logger and restores it on
  shutdown; `"pyrock"` configures only the `pyrock` logger (use it when Django
  `LOGGING` or `dictConfig` owns logging, and add `pyrock.logs.CorrelationFilter`
  to your handlers); `"none"` installs nothing.
- `log_format="text"` gives a human-readable line instead of JSON.
- `log_canonical=True` emits one `operation.complete` record per operation with
  `operation`, `duration_ms`, `success`, `attributes`, `trace_id` and `span_id`.
  A failed operation's record also has `error`, `error.type` and
  `error.fingerprint`.
- `log_add_source=True` adds `source` as `module:function:line`.

### Metrics

```python
requests = pyrock.counter("api_requests_total", "Total API requests", labels=["endpoint", "method"])
requests.labels(endpoint="/users", method="GET").inc()
requests.inc()                                        # declared labels render as "_"

depth = pyrock.gauge("queue_depth", "Current depth", labels=["queue_name"])
depth.labels(queue_name="jobs").set(42)               # also inc(), dec(), add(), sub()

latency = pyrock.histogram("query_latency_ms", "Query latency", labels=["db_type"])
latency.labels({"db_type": "postgres"}).observe(123.45)   # a mapping works for dotted keys
with latency.labels(db_type="pg").time():             # milliseconds; also usable as a decorator
    run_query()
```

Static labels are added automatically. An undeclared label key raises
`ValueError`. Re-registering a name with a different type or label set raises
`MetricConflictError`; the same schema returns the existing metric. Counters
reject negative and NaN increments with `ValueError`. Bound children are
cached, so `.labels(...)` in a loop is cheap.

With `runtime_metrics=True` (default), each scrape or push also reports
`python_gc_collections_total`, `python_gc_collected_total`,
`python_gc_uncollectable_total`, `python_gc_pause_seconds`, `python_threads`,
`python_asyncio_tasks` (loops tracked by the ASGI middleware or
`pyrock.track_loop()`), `python_info`, `python_tracemalloc_traced_bytes` (when
tracemalloc is tracing) and the standard `process_*` metrics
(`process_cpu_seconds_total`, `process_resident_memory_bytes`, ...).

### HTTP server instrumentation

Both middlewares take the same options:

```python
Middleware(app,
    operation_name="http.request",
    additional_labels=["user_type"],
    additional_attrs=lambda environ: {"user_type": environ.get("HTTP_X_USER_TYPE", "")},
    success_codes=range(200, 400),         # default: every status below 400
    trace_propagation=True,
    route=lambda environ: environ.get("pyrock.route"),
    bedrock=None,
)
```

- Default attributes: `http.method`, `http.route`, `http.scheme`, `http.host`,
  `http.status_code`. `http.user_agent` is sensitive and emitted only when the
  privacy policy's `attribute_keys` allowlists it.
- `http.route` is always a *template* or `"_"`, never a raw path. It comes from
  `route=` (a `None` result falls through), then from the framework: ASGI
  `scope["route"].path_format` (FastAPI) or `scope["pyrock.route"]` /
  `environ["pyrock.route"]` set by a framework hook. See the Flask and Django
  recipes.
- The final status counts: the last `start_response` in WSGI, the first
  non-1xx `http.response.start` in ASGI. A status outside `success_codes`
  marks failure. An exception escaping the app marks failure with
  `http.status_code=500` (or the status already sent) and is re-raised.
- Duration runs until the body finishes: the WSGI iterable's `close()`, or the
  last ASGI body message, so streaming responses are timed correctly. (In
  tests, close test-client responses before reading request metrics.) The
  WSGI middleware wraps the response iterable, so a server's
  `wsgi.file_wrapper` fast path is not used for instrumented responses.
- ASGI passes `lifespan` through. `websocket=True` covers each websocket
  connection as one `SERVER` operation named `http.websocket`.
- `pyrock.http.instrument_http_server(MyHandler)` returns a traced subclass of
  a stdlib `http.server.BaseHTTPRequestHandler`, with the same options.

### HTTP client

```python
import pyrock.http

resp = pyrock.http.get("https://api.example.com/users")   # a CLIENT span "HTTP GET"
resp = pyrock.http.post(url, json={"name": "a"}, timeout=10)
resp = pyrock.http.request("PUT", url, data=body, headers={"Authorization": f"Bearer {tok}"})
resp.status, resp.headers, resp.json(), resp.text

opener = pyrock.http.build_opener(urllib.request.HTTPCookieProcessor())   # like NewClient
```

Client spans record `http.method`, `http.host`, `http.scheme` and
`http.status_code`; 4xx/5xx set error status. URLs never keep credentials,
query or fragment, and raw paths (`http.target`, `http.url`) are emitted only
when allowlisted. HTTP error responses are returned as a `Response`; pass
`raise_for_status=True` to get `urllib.error.HTTPError` instead.
`pyrock.http.TracingHandler` adds tracing to an existing urllib setup; note
that urllib has no hook for requests that fail before a response, so only
openers from `build_opener` (and `request`/`get`/`post`) export the span of
such a failed request.

For third-party clients, `pyrock.inject_headers(headers)` writes
`traceparent`/`tracestate` into any `MutableMapping`. Copy-in recipes:
[`httpx_hooks.py`](examples/recipes/httpx_hooks.py) and
[`requests_adapter.py`](examples/recipes/requests_adapter.py).

### Propagators and W3C Trace Context

`pyrock.trace.http.HTTPPropagator` reads and writes header mappings and lists
of `(name, value)` pairs (str or ASGI bytes). Header names are
case-insensitive, repeated `tracestate` headers are joined, and an invalid
`traceparent` starts a new trace. `pyrock.trace.w3c` has the format helpers:
`parse_traceparent`, `format_traceparent`, `span_context_from_traceparent`,
`parse_tracestate`, `format_tracestate` and the key/value validators.

Any object with `extract(carrier)` and `inject(carrier, span_context=None)`
satisfies the `pyrock.trace.propagator.Propagator` protocol, so other
transports are a few lines:

```python
from pyrock.trace import SpanKind, w3c

class KafkaPropagator:
    def extract(self, headers: list[tuple[str, bytes]]) -> pyrock.SpanContext | None:
        for key, value in headers:
            if key == "traceparent":
                try:
                    return w3c.span_context_from_traceparent(value.decode())
                except w3c.InvalidTraceparent:
                    return None
        return None

    def inject(self, headers, span_context=None) -> None:
        sc = span_context or pyrock.current_span_context()
        if sc is not None:
            headers.append(("traceparent", w3c.format_traceparent(sc).encode()))

with pyrock.operation("consume", kind=SpanKind.CONSUMER,
                      remote_parent=KafkaPropagator().extract(msg.headers)):
    ...
```

The gRPC version (client and server interceptors for `grpcio`) is
[`examples/recipes/grpc_interceptors.py`](examples/recipes/grpc_interceptors.py).

### Sampling

```python
from pyrock.trace import sampler

pyrock.init(trace_sampler=sampler.ParentBased(sampler.Ratio(0.1)))
```

`AlwaysOn`, `AlwaysOff`, `Ratio(r)` and `ParentBased(root)`. The default is
`ParentBased` with a root that samples `trace_sample_rate` of new traces.
Sampling controls export, not span creation. `trace_sample_rate=0` disables
recording. See [Differences from Go](#differences-from-go-bedrock).

### Testing with `pyrock.testing`

```python
from pyrock.testing import capture

def test_process_user():
    with capture(service="test") as t:         # in memory: no server, no detection, no network
        process_user("123")

    op = t.spans.one(name="process_user")
    assert op.attrs["user_id"] == "123"
    assert not op.failed
    assert t.metrics.value("process_user_count", status="active") == 1
    assert t.logs.find(msg="processing request")["trace_id"] == op.trace_id
```

Spans are complete once the block exits; `t.metrics.text()` and
`t.metrics.value()` also work inside it. The filters (`spans.find`,
`spans.one`, `metrics.value`, `logs.find`, `logs.find_all`) take keywords
and an optional positional mapping for dotted keys
(`t.metrics.value("worker_jobs", {"worker.type": "async"})`).
`spans.find(kind=...)` filters by kind. Nested captures restore the outer
instance. Under pytest, the `pyrock_capture` fixture is also available; it is
registered through an entry point, and pyrock never imports pytest.

## Configuration

`pyrock.init()` builds its `Config` from `BEDROCK_*` environment variables,
then applies keyword overrides (any `Config` field) and validates. A malformed
variable or invalid value raises `pyrock.ConfigError` (a `ValueError`) whose
`problems` list names every issue; values are never echoed. Unknown keyword
names raise `TypeError`.

```python
pyrock.init(service="users-api", log_level="debug",
            static_attrs={"env": "production", "version": "1.2.3"})

cfg = pyrock.Config.from_env().replace(metric_prefix="myapp", log_canonical=True)
pyrock.init(cfg, static_attrs={"region": "us-west-2"})
```

Durations are `float` seconds (a `timedelta` is accepted). In the environment
they keep Go's syntax (`10s`, `500ms`, `1m30s`); a bare number is seconds.
Booleans accept `1/0`, `true/false`, `yes/no` and `on/off`. Empty variables
count as unset.

| Variable | `Config` field | Type | Default |
|---|---|---|---|
| `BEDROCK_SERVICE` | `service` | str | `"unknown"` |
| `BEDROCK_TRACE_URL` | `trace_url` | str \| None | `None` (no export) |
| `BEDROCK_TRACE_PROTOCOL` | `trace_protocol` | `"grpc"` \| `"http"` \| None | `None` (auto: URL with scheme or path is HTTP, bare `host:port` is gRPC) |
| `BEDROCK_TRACE_INSECURE` | `trace_insecure` | bool | `False` |
| `BEDROCK_TRACE_SAMPLE_RATE` | `trace_sample_rate` | float in [0, 1] | `1.0` |
| `BEDROCK_LOG_LEVEL` | `log_level` | `debug`/`info`/`warn`/`warning`/`error`/`critical` (any case) | `"INFO"` |
| `BEDROCK_LOG_FORMAT` | `log_format` | `"json"` \| `"text"` | `"json"` |
| `BEDROCK_LOG_ADD_SOURCE` | `log_add_source` | bool | `False` |
| `BEDROCK_LOG_CANONICAL` | `log_canonical` | bool | `False` |
| `BEDROCK_LOG_INSTALL` | `log_install` | `"root"` \| `"pyrock"` \| `"none"` | `"root"` (Python only) |
| `BEDROCK_METRIC_PREFIX` | `metric_prefix` | str | `""` |
| `BEDROCK_METRIC_BUCKETS` | `metric_buckets` | comma-separated floats, strictly increasing | `None` (defaults: 0.5 … 1000) |
| `BEDROCK_METRIC_SERIES_LIMIT` | `metric_series_limit` | positive int | `1000` |
| `BEDROCK_METRIC_FAMILY_LIMIT` | `metric_family_limit` | positive int | `1000` |
| `BEDROCK_RUNTIME_METRICS` | `runtime_metrics` | bool | `True` |
| `BEDROCK_SERVER_ENABLED` | `server_enabled` | bool | `True` |
| `BEDROCK_SERVER_ADDR` | `server_addr` | `host:port` | `"127.0.0.1:9090"` |
| `BEDROCK_SERVER_METRICS` | `server_metrics` | bool | `True` |
| `BEDROCK_SERVER_PPROF` | `server_pprof` | bool | `False` |
| `BEDROCK_SERVER_READ_TIMEOUT` | `server_read_timeout` | duration | `10s` |
| `BEDROCK_SERVER_READ_HEADER_TIMEOUT` | `server_read_header_timeout` | duration | `5s` |
| `BEDROCK_SERVER_WRITE_TIMEOUT` | `server_write_timeout` | duration | `30s` |
| `BEDROCK_SERVER_IDLE_TIMEOUT` | `server_idle_timeout` | duration | `120s` |
| `BEDROCK_SERVER_MAX_HEADER_BYTES` | `server_max_header_bytes` | positive int | `1048576` (1 MiB) |
| `BEDROCK_SERVER_MAX_CONNECTIONS` | `server_max_connections` | positive int | `64` (Python only) |
| `BEDROCK_DISABLE_ENVIRONMENT_DETECTION` | `disable_environment_detection` | bool | `False` |
| `BEDROCK_SHUTDOWN_TIMEOUT` | `shutdown_timeout` | duration | `30s` |

Fields without a variable, set in code only:

| Field | Type | Default |
|---|---|---|
| `trace_sampler` | `pyrock.trace.sampler.Sampler` \| None | `None` (`ParentBased`, sampling roots at `trace_sample_rate`) |
| `log_output` | text stream \| None | `None` (`sys.stderr`) |
| `server_profile_handler` | WSGI app \| None | `None` (see [Profiles](#profiles)) |
| `data_policy` | `pyrock.privacy.Policy` \| None | `None` (the default policy) |
| `profiles` | `pyrock.profile.ProfileExportConfig` \| None | `None` (no profile export) |

`pyrock.env.parse(MyConfig)` reads your own dataclasses the same way:

```python
from dataclasses import dataclass, field
from typing import Annotated
from pyrock.env import Env, parse

@dataclass
class AppConfig:
    bedrock: pyrock.Config = field(default_factory=pyrock.Config.from_env)
    port: Annotated[int, Env("PORT")] = 8080
    database_url: Annotated[str, Env("DATABASE_URL", required=True)] = ""
    allowed_hosts: Annotated[list[str], Env("ALLOWED_HOSTS", sep=",")] = field(default_factory=list)

cfg = parse(AppConfig)          # env.ParseError lists every failure; environ= for tests
pyrock.init(cfg.bedrock)
```

Supported field types: `str`, `int`, `float`, `bool`, `timedelta`,
`list[T]`, `tuple[T, ...]`, `Literal[...]`, `enum.Enum`, `Optional[T]` and
nested dataclasses.

## Privacy

Telemetry defaults are conservative, as in Go. `privacy.Policy` is enforced by
one walker on span attributes, events, status messages, log records (including
`extra` and nested values), metric label values and profile labels.

```python
from pyrock import privacy

pyrock.init(data_policy=privacy.Policy(
    attribute_keys=None,             # None keeps non-sensitive fields; a list is a strict allowlist ("http.*" wildcards)
    resource_keys=["host.name"],     # extra resource keys to export
    allow_error_details=False,       # error.message, exception.*
    allow_source_paths=False,        # full file paths, error.stack
    allow_profile_labels=False,
    max_string_length=1024,
    redact=lambda path, value: (value, True),   # (new_value, keep); an exception drops the value
))
```

By default:

- sensitive keys (`password`, `authorization`, `cookie`, `api_key`, `secret`,
  `*token`, ...) become `"[redacted]"`;
- error messages, stacks and `exception.*` stay private; `error`,
  `error.type`, `error.fingerprint`, `error.chain`, `error.types` and
  `error.group.count` are kept;
- `http.user_agent`, `http.target` and `http.url` are emitted only when
  `attribute_keys` allowlists them; URLs always lose credentials, query and
  fragment;
- resource keys that identify a host, process, container or Kubernetes object
  are dropped unless `resource_keys` allowlists them;
- values of unknown types are dropped (pyrock never calls `str()` or `repr()`
  on arbitrary objects), long strings are truncated, traversal is bounded
  (4,096 nodes), and cycles become `"[cycle]"`.

## Observability server

With `server_enabled` (default), `init()` starts a small HTTP/1.1 server on
daemon threads at `127.0.0.1:9090`:

| Endpoint | Behaviour |
|---|---|
| `/metrics` | Prometheus text format 0.0.4 (if `server_metrics`). The whole snapshot is validated first; a failure returns 500 with no partial body. |
| `/health`, `/ready` | `200 ok` |
| `/debug/pprof/*` | Only with `server_pprof=True` **and** a `server_profile_handler` |

Security defaults match Go: loopback-only address, pprof off, a 5 s deadline
for the request line and headers (Slowloris), 10 s read, 30 s write and 120 s
idle timeouts, a 1 MiB header limit (431 and close), at most 64 concurrent
connections (an extra connection gets 503 and is closed), and graceful
shutdown. If the port is busy, a warning is logged and the application keeps
running. Binding to `0.0.0.0` is an explicit opt-in
(`BEDROCK_SERVER_ADDR=0.0.0.0:9090`).

The server can be run or mounted by hand:

```python
from pyrock import server

srv = server.Server(bedrock.metrics, server.Config(addr="127.0.0.1:0"))
srv.start()                     # srv.address is (host, port) once started
srv.shutdown(timeout=15)

app = server.metrics_app(bedrock.metrics)   # /metrics as a plain WSGI app
```

## Profiles

Python has no runtime pprof, so pyrock builds each profile type from stdlib
pieces and encodes it natively as OTLP Profiles (`v1development`, v0.2.0
schema, as in Go) and as pprof `profile.proto`.

| `ProfileType` | Implementation | Sample types |
|---|---|---|
| `CPU` | A sampler thread at 100 Hz (`sample_hz`) over `sys._current_frames()`, keeping threads whose CPU clock advanced. Without per-thread CPU clocks (non-Linux), it falls back to wall time and says so in a `profile.note` attribute. | `samples/count`, `cpu/nanoseconds` |
| `WALL` | The same sampler without the CPU filter (Python extra, useful for I/O-bound code) | `samples/count`, `wall/nanoseconds` |
| `HEAP` | `tracemalloc` snapshot diffs. tracemalloc is started only if it is not already tracing, and stopped on export stop only if pyrock started it. Expect roughly 10–30 % allocation overhead. | `alloc_space/bytes`, `inuse_space/bytes`, `alloc_objects/count` |
| `THREADS` (alias `GOROUTINE`) | Stacks of every thread plus pending asyncio tasks on tracked loops | `threads/count`, `tasks/count` |
| `MUTEX` | Waits recorded by the instrumented `pyrock.profile.Lock` and `RLock`, sampled by `mutex_sample_fraction` | `contentions/count`, `delay/nanoseconds` |
| `BLOCK` | Waits recorded by the instrumented `pyrock.profile.Condition` and `Semaphore`, sampled by `block_sample_rate` | `contentions/count`, `delay/nanoseconds` |

`MUTEX` and `BLOCK` only see the instrumented primitives (drop-in replacements
for the `threading` ones); without them the profiles are empty and carry a
`profile.note`. Only one pyrock sampler runs per process: another one, or a
`PYROCK_PROFILER_OWNER` variable, makes CPU collection skip (and count the
skip), never stop the other profiler.

### OTLP export

```python
from pyrock.profile import ProfileExportConfig, ProfileType

pyrock.init(profiles=ProfileExportConfig(
    endpoint="https://collector.example/v1/profiles",
    headers={"Authorization": f"Bearer {token}"},     # never shown in repr
    types=(ProfileType.CPU, ProfileType.HEAP),        # default: CPU, HEAP, THREADS; () disables
    interval=60.0,
    cpu_sample_duration=10.0,                         # > 0 and < interval
    timeout=10.0, max_bytes=8 << 20, retries=3,       # max 32 MiB, 0 to 3 retries
))
```

The first collection runs immediately, then once per `interval`. Each upload
carries an `x-profile-type` header; redirects are never followed, plain
`http://` needs `allow_http=True`, and 429/5xx/connection errors are retried.
`bedrock.profile_stats()` returns `exported`, `failed` and `skipped` counts.
`bedrock.start_profile_export(cfg)` starts an additional exporter that
`shutdown()` also stops.

### Debug handler and `go tool pprof`

```python
from pyrock.profile import debug

pyrock.init(server_pprof=True, server_profile_handler=debug.app())
```

`/debug/pprof/profile?seconds=N` (CPU; `N` above 30 is rejected with 400),
`/heap`, `/goroutine` (`?debug=1` for text stacks), `/mutex` and `/block`
serve gzipped pprof that works with `go tool pprof` and Pyroscope. GET only.
`/heap` needs tracemalloc to be tracing already (HEAP export or
`PYTHONTRACEMALLOC`). Importing `pyrock` registers nothing; the handler must be
imported and passed explicitly. It serves full source paths, so keep it on a
protected interface.

```bash
go tool pprof http://127.0.0.1:9090/debug/pprof/profile?seconds=10
go tool pprof -http=:8081 http://127.0.0.1:9090/debug/pprof/heap
```

## Cloud backend

```python
pyrock.init(cloud=pyrock.cloud(
    "brk_live_abc123",
    endpoint="https://ingest.bedrock.dev",           # default
    push_interval=15.0,
    profile_interval=60.0,
    profile_cpu_sample_duration=10.0,
    profiles=[ProfileType.CPU, ProfileType.HEAP, ProfileType.THREADS],   # [] disables
    retry_backoff=(1.0, 2.0, 4.0),                   # () disables retries
    traces_endpoint=None,                            # optional gRPC target override
))
```

This is the same wiring as Go's `WithCloud`:

- **Traces** go over OTLP/gRPC with `x-bedrock-key` metadata, through a larger
  batch processor (queue of 10,000). The target is `traces_endpoint` (bare
  `host:port` or a URL) when set, otherwise derived from `endpoint`.
- **Retry:** one attempt plus one retry per `retry_backoff` entry (4 calls with
  the default). Back-off waits are interrupted by shutdown.
- **Fan-out:** with a local `trace_url` as well, every batch goes to both
  exporters.
- **Metrics push** sends Prometheus text to `{endpoint}/v1/metrics` with
  `Authorization: Bearer` every `push_interval`, skips a push while one is in
  flight, and flushes once more on shutdown.
- **Profile push** sends OTLP profiles to `{endpoint}/v1/profiles` every
  `profile_interval`.

`api_key` is excluded from `repr` and equality. Because the key travels with
every request, an `http://` endpoint is rejected unless `insecure=True`, which
is for local development only.

## Environment detection

Unless `disable_environment_detection` is set, `init()` detects resource
attributes in parallel under a 500 ms total budget. Detection never fails or
blocks `init()`.

| Attributes | Source |
|---|---|
| `process.pid`, `process.executable.name`, `process.runtime.name`, `process.runtime.version` | the running interpreter |
| `host.name` | `socket.gethostname()` |
| `service.version` | `git rev-parse` of the source tree (first 12 characters), else the installed distribution's version |
| `k8s.pod.name`, `k8s.namespace`, `k8s.node.name`, `k8s.container.name` | the same variables and files as Go |
| `container.id` | `/proc/self/cgroup` and `/proc/self/mountinfo` |
| `cloud.provider`, `cloud.region`, `host.id` | AWS IMDSv2 or GCP metadata, probed only when local hints (DMI files, platform variables) suggest that cloud |

Detected attributes come before `static_attrs`, so your values win. Like
static attributes, they are resource attributes on spans, fields in logs and
labels on metrics, except the infrastructure keys (`host.*`, `process.*`,
`container.*`, `k8s.*`, `cloud.*`), which never become labels and are exported
only when the privacy policy's `resource_keys` allows them (`cloud.*` passes by
default). Note that `service.version` does become a metric label.

## Differences from Go Bedrock

pyrock keeps Bedrock's concepts, wire formats and names. These are the places
where it differs on purpose:

- **API shape.** Context managers and decorators instead of `op, ctx :=
  Operation(ctx, ...)`; plain values instead of `attr.String(...)`; keyword
  arguments instead of functional options; exceptions instead of
  `Register(attr.Error(err))`. No `ctx` argument: context travels in
  `contextvars`.
- **Samplers.** `Ratio` is deterministic on the low 8 bytes of the trace ID,
  so every service makes the same decision for a trace (Go draws a random
  number per span). `ParentBased` drops spans whose parent is unsampled (Go
  consults the root sampler).
- **Log levels.** JSON `level` uses Go's names `DEBUG`, `INFO`, `WARN`,
  `ERROR`; Python's `CRITICAL` stays `CRITICAL`. Records also carry `logger`.
- **Counters** raise `ValueError` on negative or NaN increments (Go ignores
  them).
- **Thread context.** Threads do not inherit context unless the interpreter
  does (3.14t): use `bind`, `ContextThreadPoolExecutor` or `pyrock.Thread`.
  pyrock never monkey-patches `threading`.
- **WebSockets** (ASGI, opt-in) are one `http.websocket` operation per
  connection, with the HTTP label schema and `"_"` for `http_method` and
  `http_status_code`, so a shared `operation_name` keeps one metric schema.
  Go has no equivalent.
- **Profiles.**
  - `THREADS` stands in for `GOROUTINE` (the name is kept as an alias and the
    wire value is `goroutine`).
  - `MUTEX` and `BLOCK` only cover pyrock's instrumented `Lock`/`RLock` and
    `Condition`/`Semaphore`; Python exposes no process-wide contention data.
  - `WALL` is an extra type.
  - CPU samples carry no asyncio task label.
  - The exporter collects the first profile immediately on start instead of
    after one interval.
- **Python-only settings:** `log_install` (`BEDROCK_LOG_INSTALL`) and
  `server_max_connections` (`BEDROCK_SERVER_MAX_CONNECTIONS`); Python threads
  cost more than goroutines, so the server caps connections.
- **Import cost.** `import pyrock` takes about 40–50 ms, mostly stdlib
  `logging` and `dataclasses`; heavy modules load on first use.
- **Overhead.** An active operation costs on the order of 10 µs to enter and
  exit, and the noop path well under 1 µs.

## Compatibility

| Python | Status |
|---|---|
| 3.11, 3.12, 3.13, 3.14 | Supported and tested in CI |
| 3.14t (free-threaded) | Supported and tested with `PYTHON_GIL=0`, including a repeated stress job |
| 3.15 (pre-release) | Tested in CI |

- **Linux** runs the full matrix. **macOS** and **Windows** run the suite on
  3.14; fork tests and Linux-only detectors are skipped there, and CPU
  profiles fall back to wall time where per-thread CPU clocks are missing.
- CPython only. Code stays valid on 3.11; newer stdlib APIs are used only
  behind feature detection.
- Pre-1.0: minor versions may change behaviour. Every intentional change is
  listed in [CHANGELOG.md](CHANGELOG.md) with a migration note.

## Development

```bash
uv sync                 # dev tools: pytest, coverage, ruff, mypy
make ci                 # ruff format --check, ruff check, mypy --strict src, tests with ≥80% branch coverage
make test               # pytest only; PYTESTFLAGS="-k name" to filter
make compat             # official OTLP protos and a real grpcio collector (compat group)
make matrix             # the suite on 3.11 … 3.15 and 3.14t (PYTHON_GIL=0)
make stress             # concurrency-sensitive tests, repeated (CI: 3.14t, 10 runs)
make build              # wheel and sdist
```

Tests live in `tests/`: `e2e/` (black-box, observes only collectors, `/metrics`,
logs and headers), `unit/<area>/` (module contracts), `structure/` (stdlib-only
imports, dependency boundaries, lazy imports, the public API) and `support/`
(shared fakes). The `compat` dependency group (`protobuf`,
`opentelemetry-proto`, `grpcio`) is test-only; those tests skip cleanly when it
is not installed. See [CONTRIBUTING.md](CONTRIBUTING.md) for the rules and
[SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

MIT. See [LICENSE](LICENSE).
