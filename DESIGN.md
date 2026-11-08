# pyrock — Bedrock for Python

**Status:** implemented (0.1.0.dev0); where the implementation deviated, this document has been updated and `PLAN.md` records the decision · **Target:** feature parity with `github.com/kzs0/bedrock` · **Dependencies:** none (CPython standard library only)

pyrock is the Python version of Bedrock. It is one opinionated library that
covers traces, metrics, profiles and structured logs, built around *operations*
(named units of work). It keeps all of Bedrock's concepts and its wire formats
(OTLP traces/profiles, Prometheus exposition, W3C Trace Context, `BEDROCK_*`
environment variables, metric names). The API is written the way a Python
developer would expect: context managers, decorators, `contextvars`, the
`logging` module, WSGI/ASGI and dataclasses.

```python
import pyrock

with pyrock.init(service="users-api"):
    with pyrock.operation("process_user", attrs={"user_id": "123"},
                          metric_labels=["status"]) as op:
        op.register(status="active")
        do_work()          # an exception escaping here marks the op as failed
```

---

## 1. Goals and non-goals

**Goals**

1. **Same concepts:** Init, Operation, Source, Step, NoTrace, static attributes,
   success by default, metric labels declared up front with a `"_"` default,
   numbered duplicate names, a noop when not initialized.
2. **Same features:** OTLP trace export (HTTP/JSON and gRPC), samplers, W3C
   propagation, a pluggable propagator interface, automatic operation metrics,
   direct counters/gauges/histograms, runtime metrics, the Prometheus `/metrics`
   server with security defaults, canonical logs, privacy policy, environment
   auto-detection, OTLP profile export, an optional pprof-compatible debug
   handler, the cloud backend (gRPC traces, metrics push, profile push,
   retry and fan-out), and an `env.parse` config helper.
3. **Interoperable output:** a Go service and a Python service behind the same
   collector produce the same metric names, label sets, span attributes, log
   shape and env-var configuration. Dashboards and alerts carry over.
4. **Zero runtime dependencies:** `pip install pyrock` installs one pure-Python
   wheel. Optional framework integrations depend only on protocols (WSGI, ASGI,
   `urllib` handlers), never on the frameworks themselves.
5. **Pythonic:** it should feel like stdlib `logging` plus `contextlib`, not like
   ported Go.

**Non-goals**

- Implementing the OpenTelemetry Python API/SDK. We emit OTLP but do not
  register as an OTel `TracerProvider`. A bridge could be a separate package
  later.
- Prometheus "multiprocess mode" shared-memory files. See §15 for the
  pre-fork story.

**Baseline:** CPython **3.11+**. 3.10 reaches end of life in October 2026.
3.11 gives us `ExceptionGroup`, `asyncio.TaskGroup`, `tomllib`, and fast
`contextvars`. The library is tested on 3.11 through 3.14, including the
free-threaded build (3.14t, run with `PYTHON_GIL=0`) and the 3.15 pre-release.
It never relies on the GIL for correctness.

---

## 2. Translating Bedrock's ideas to Python

| Go idea | Python form | Why |
|---|---|---|
| `context.Context` carries everything, no globals | `contextvars.ContextVar`s hold the active `Bedrock`, operation, source, step and no-trace flag | `contextvars` is Python's equivalent of request-scoped context. It flows through `await` and `asyncio` tasks automatically and is isolated per thread. Users don't have to pass a `ctx` argument everywhere, but there is still no process-global singleton: if no var is set, you get the noop. |
| `op, ctx := Operation(ctx, …); defer op.Done()` | `with pyrock.operation(…) as op:` or `@pyrock.operation(…)` | Context managers are Python's `defer`. Decorators remove boilerplate entirely. |
| `op.Register(ctx, attr.Error(err))` marks failure | An exception escaping the `with` block or decorated function marks failure automatically. `op.fail(exc)` covers handled errors. | Python reports errors by raising. Ops still succeed by default. |
| `attr.String("k", v)`, `attr.Int(…)` | Plain values: `{"k": "v", "count": 3}` or `register(k="v")`. Types are inferred (`str`, `int`, `float`, `bool`, `timedelta`, `datetime`, `bytes`, `BaseException`, and sequences of those). | Dynamic typing. Annotations (`AttrValue`) keep type checkers happy. |
| Functional options (`Attrs()`, `MetricLabels()`, `NoTrace()`) | Keyword-only arguments: `attrs=`, `metric_labels=`, `no_trace=` | Keyword arguments are Python's options pattern, and IDEs can complete them. |
| `slog` | stdlib `logging`: a `Handler`, a JSON `Formatter`, and a correlation `Filter` | Existing `logging.getLogger(__name__)` calls get trace correlation without any changes. |
| `http.Handler` middleware | `pyrock.wsgi.Middleware` / `pyrock.asgi.Middleware` | These two protocols cover Django, Flask, FastAPI, Starlette, Litestar, Falcon and Pyramid with no framework imports. |
| `NewClient(*http.Client)` / `RoundTripper` | `urllib.request` `BaseHandler` plus `pyrock.http.request()` / `get()` / `post()`, and an `inject_headers()` helper | A `urllib` handler is the stdlib's way to extend clients. Recipes cover `httpx`/`requests` (see §9.3). |
| `Config` struct + `env` tags | A frozen `@dataclass Config` with `Config.from_env()`. `pyrock.env.parse(MyConfig)` reads `Annotated[int, Env("PORT")]`. | Dataclasses plus `Annotated` metadata are the modern typed-config style. |
| `close()` returned by `Init` | `init()` returns a `Bedrock` that is also a context manager. Call `.shutdown(timeout=)`, or `await .ashutdown()` from async code. | Supports `with`, explicit lifecycle, and async apps. |
| Goroutines for export and push | Daemon `threading.Thread` workers with `threading.Event` for stop signals | They work the same in sync, threaded and asyncio apps, and are never tied to one event loop. |
| `runtime/pprof` | A sampling profiler thread using `sys._current_frames()`, `tracemalloc`, thread/task stack dumps, and opt-in instrumented locks | This is the closest stdlib match for each Go profile type (§12). |
| `trace.Propagator` interface | `typing.Protocol` with `extract()` / `inject()` | Structural typing, so no inheritance is needed. |

### 2.1 Why not pass `ctx` explicitly like Go?

We could expose a `ctx` object, but Python has no culture of explicit context
arguments. `contextvars` is already the per-request scope used by asyncio,
`decimal` and the Python OTel SDK. It has the right semantics:

- Each asyncio `Task` gets a **copy** of the context when it is created, so an
  operation opened in a handler is the parent of operations in tasks spawned
  from it. Siblings never leak into each other.
- Threads start with an **empty** context, which is unfortunately true on 3.11
  and 3.13 (3.14 inherits only on free-threaded builds). Without help,
  `ThreadPoolExecutor` work would land in the noop. pyrock provides helpers so
  that losing context is a visible choice and never a surprise (§4.6).

You can still use an explicit instance when you need it: `b = pyrock.Bedrock(config)`
works without being activated, and `b.operation(...)`, `b.counter(...)` and the
rest are available as methods. This is what libraries and tests use.

---

## 3. Package layout

```
pyrock/
├── __init__.py          # Public API re-exports (init, operation, step, source, info, counter, ...)
├── py.typed
├── _version.py          # __version__, SCOPE_NAME (utility: imports nothing from pyrock)
├── _compat.py           # Feature detection, ConfigError, LOG_LEVELS, validation helpers (utility)
├── _types.py            # WSGI/ASGI type aliases (utility)
├── _transport.py        # Header validation, deadlines, no-redirect opener, fork forwarding (utility)
├── _bedrock.py          # Bedrock class + noop instance: lifecycle, accessors, fork hook, module helpers
├── _context.py          # ContextVars + bind, ContextThreadPoolExecutor (lazy), Thread
├── _noop.py             # Preallocated noop counter/gauge/histogram
├── config.py            # Config dataclass, ENV_VARS, from_env(), default() (ConfigError re-exported from _compat)
├── env.py               # Env marker, parse(dataclass_type) -> instance
├── attr.py              # AttrValue typing, Event, error normalization, Attrs (ordered, deduped)
├── _operation.py        # Operation, Step, Source + their noop singletons (context managers + decorators)
├── trace/
│   ├── __init__.py      # Tracer, Span, SpanContext, SpanKind, StatusCode, TraceState, current_span()
│   ├── sampler.py       # Sampler Protocol, AlwaysOn, AlwaysOff, Ratio, ParentBased
│   ├── propagator.py    # Propagator Protocol, CompositePropagator
│   ├── w3c.py           # parse/format traceparent & tracestate, validation
│   ├── http.py          # HTTPPropagator for header Mappings / MutableMappings
│   └── otlp/
│       ├── __init__.py  # ExportError, PermanentExportError
│       ├── _pb.py       # Minimal hand-written protobuf varint/len-delimited encoder
│       ├── _h2.py       # Minimal HTTP/2 client (TLS+ALPN "h2" or h2c) + HPACK literals
│       ├── http.py      # OTLP/HTTP JSON exporter (urllib)
│       ├── grpc.py      # OTLP/gRPC exporter over _h2
│       └── batch.py     # BatchProcessor (bounded queue, worker thread, stats)
├── metric/
│   ├── __init__.py      # Registry, Counter, Gauge, Histogram, label-bound children
│   ├── runtime.py       # RuntimeCollector (gc, threads, rusage, RSS, fds, asyncio)
│   └── prometheus.py    # Text exposition encode() + validation
├── logs.py              # BedrockHandler, JSONFormatter, CorrelationFilter, canonical log
├── privacy.py           # Policy dataclass + enforcement walker
├── server.py            # Observability server (ThreadingHTTPServer, hardened)
├── _httpcore.py         # Request instrumentation shared by wsgi, asgi and http.server
├── wsgi.py              # WSGI middleware
├── asgi.py              # ASGI 3 middleware (http, opt-in websocket, lifespan passthrough)
├── http.py              # urllib TracingHandler, request/get/post, build_opener, instrument_http_server
├── _cloud.py            # cloud() option, retry + fan-out exporters, wiring
├── _envdetect.py        # process/git/k8s/container/AWS/GCP detection
├── _push.py             # metrics push loop
├── profile/
│   ├── __init__.py      # Re-exports: ProfileType, ProfileExportConfig, start_export(), locks
│   ├── _config.py       # ProfileType, ProfileExportConfig, ProfileExportStats
│   ├── _export.py       # ProfileExporter thread (collect, encode, POST, retry)
│   ├── _frames.py       # Stack interning shared by the collectors
│   ├── _sampler.py      # Wall/CPU sampling profiler thread
│   ├── _heap.py         # tracemalloc snapshots
│   ├── _stacks.py       # Thread + asyncio task stack dumps
│   ├── _locks.py        # Opt-in instrumented Lock/RLock/Condition for contention
│   ├── _pprof.py        # profile.proto (pprof) encoder
│   ├── _otlp.py         # OTLP profiles v1development v0.2.0 encoder
│   └── debug.py         # Optional /debug/pprof/* handler (pprof-format, works with `go tool pprof`)
└── testing.py           # In-memory exporters, capture() helper, pytest-friendly assertions
```

Import rules mirror Go. The utility modules (`_types`, `_compat`, `_version`,
`_transport`) import nothing from pyrock, and any module may import them.
`attr`, `env` and `privacy` import only utilities. `trace` and `metric` import
`attr` and `privacy`. `logs` imports `attr`, `trace` and `privacy`. `server`
imports `metric`; `profile` imports `attr`, `privacy` and `trace`; `config`
imports `env`, `privacy` and (lazily, for type checks) `trace` and `profile`.
Only the top-level modules (`_bedrock`, `_context`, `_noop`, `_operation`,
`_httpcore`, `wsgi`, `asgi`, `http`, `_cloud`, `testing`) see everything. A test enforces this, as
`dependency_boundary_test.go` does in Go, by walking the AST of each module.
The same test asserts that every `import` resolves to the stdlib
(`sys.stdlib_module_names`).

Heavy modules (`profile`, `_cloud`, `trace.otlp.grpc`, `_h2`, `server`,
`wsgi`, `asgi`, `http`, `_httpcore`, `testing`) load lazily so `import pyrock`
stays cheap. The original target was 15 ms; the implementation takes about
40 ms, of which about 18 ms is stdlib `logging` and `dataclasses` and cannot be
avoided (PLAN decision 41). `tests/structure/test_import_cost.py` guards
against eager imports with a loose budget.

---

## 4. Core API

### 4.1 Initialization

```python
import pyrock

# From BEDROCK_* environment variables (malformed values raise ConfigError, like Go's panic)
bedrock = pyrock.init()

# Explicit overrides. Keyword overrides apply on top of the env-derived config.
bedrock = pyrock.init(
    service="users-api",
    log_level="debug",
    static_attrs={"env": "production", "version": "1.2.3"},
)

# A full Config object
cfg = pyrock.Config.from_env().replace(metric_prefix="myapp", log_canonical=True)
bedrock = pyrock.init(cfg, static_attrs={"region": "us-west-2"})
```

Signature:

```python
def init(
    config: Config | None = None,
    /, *,
    static_attrs: Mapping[str, AttrValue] | None = None,
    cloud: CloudConfig | None = None,
    activate: bool = True,
    **overrides: Any,            # any Config field, e.g. service=, trace_url=
) -> Bedrock: ...
```

`init()` does the following:

1. Builds the config: `config or Config.from_env()`, then applies `**overrides`
   and validates. Unknown override names raise `TypeError`, as Python does for
   bad keyword arguments.
2. Runs environment detection (§13) unless disabled, and puts the detected
   resource attrs before `static_attrs`, so user values win.
3. Builds the tracer, registry, logging handler, server, profile exporter and
   cloud wiring.
4. When `activate=True` (the default), sets the `Bedrock` in the root context
   var. Everything after `init()` in that thread or task, and every task spawned
   later, sees it.
5. Registers an `os.register_at_fork` hook (§15).

The `Bedrock` returned is a context manager, both sync and async:

```python
with pyrock.init(service="worker") as bedrock:      # shutdown on exit
    run()

async def main():
    async with pyrock.init(service="api"):           # ashutdown() on exit, off-loop
        await serve()
```

Exiting `with pyrock.init()` shuts the instance down and restores the context
value that was active before, which deactivates it. Nested `with pyrock.init()`
and `testing.capture()` blocks therefore restore the outer active instance.

`pyrock.Bedrock(...)` (Go: `bedrock.New`) builds a **fully live** instance: the
constructor starts exporters, the server and log installation according to
config. It is not activated, and the caller must shut it down.

Without `with`, call `bedrock.shutdown(timeout=30.0)`. Shutdown is idempotent and
thread-safe. It flushes spans, does a final metrics push, stops profile export
and the server, and restores logging handlers. pyrock **does not** register
`atexit` on its own because hidden behaviour is avoided. `pyrock.init(..., shutdown_atexit=True)`
is an explicit opt-in for scripts.

### 4.2 Operations

Use a context manager:

```python
with pyrock.operation(
    "process_user",
    attrs={"user_id": user_id},           # high-cardinality is fine: logged and traced, not a label
    metric_labels=["status"],             # only declared keys become labels; missing → "_"
) as op:
    op.register(status="active")          # kwargs for identifier-safe keys
    op.register({"user.tier": "gold"})    # mapping for dotted keys
    op.event("cache_miss", key="u:123")   # span event, recorded immediately
    if not ok:
        op.fail(ValueError("bad user"))   # handled failure, no raise needed
```

Use a decorator on sync functions, coroutines, generators and async generators:

```python
@pyrock.operation("db.get_user", metric_labels=["table"], attrs={"table": "users"})
async def get_user(user_id: str) -> User:
    pyrock.register(user_id=user_id)      # registers on the *current* op/step
    return await db.fetch(user_id)
```

Signature (the same object works as a context manager and a decorator):

```python
def operation(
    name: str, /, *,
    attrs: Mapping[str, AttrValue] | None = None,
    metric_labels: Iterable[str] = (),
    no_trace: bool = False,
    remote_parent: SpanContext | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    expected: tuple[type[BaseException], ...] = (),   # exceptions that do NOT mark failure
) -> OperationScope: ...
```

`Operation` methods:

| Method | Go equivalent | Notes |
|---|---|---|
| `register(*items, **attrs)` | `Register(ctx, …)` | Items can be a `Mapping`, an `Event`, or a `BaseException`. An exception marks failure, the same as `attr.Error`. |
| `event(name, /, attrs=None, **kw)` | `Event(ctx, attr.NewEvent(…))` | Recorded on the span immediately. Not added to operation attrs. |
| `fail(exc=None)` | `Register(attr.Error(err))` / `Failure(err)` | Records the error attrs and marks failure. |
| `succeed()` | `Success()` / `EndSuccess()` | Clears an earlier failure, for retry loops that recover. |
| `end(*, error=None, success=None)` | `End(EndFailure/EndSuccess)` | Explicit end for scopes that can't use `with`. |
| `done()` | `Done()` | Alias for `end()`. Idempotent. |
| `.name`, `.span_context`, `.trace_id`, `.span_id`, `.failed` | accessors | Read-only. |

How the scope ends:

- **No exception:** success, unless `fail()` was called.
- **`Exception` escapes:** failure, error attrs registered, then re-raised.
  pyrock never swallows exceptions. Exceptions in `expected` are recorded as a
  span event (`exception`) and leave the outcome as success. This fits
  control-flow exceptions such as `KeyError` in a cache lookup.
- **`BaseException` escapes:** `asyncio.CancelledError` and `KeyboardInterrupt`
  are failures with `error.type` set. `GeneratorExit` (a generator closed early)
  and `SystemExit(0)` are never failures.
- **`ExceptionGroup`:** a failure. Each leaf is summarised in
  `error.group.count` / `error.types`.

On exit, attrs are written to the span in bulk, the span ends, the four metrics
are recorded, and a canonical log is emitted if enabled. This is the lifecycle
from `operation.go`.

**Metrics produced** are the same as Go, so dashboards work unchanged:
`process_user_count`, `_successes`, `_failures`, `_duration_ms` (a histogram),
each with the declared labels plus static labels. Dots in label keys become
underscores (`http.method` → `http_method`), as in Go.

**Nesting and numbering:** a second `operation("fetch")` (or step) under the
same parent operation or step becomes `fetch[1]`, then `fetch[2]`, and so on.
This uses the same per-parent counter as Go, and keeps sibling spans distinct
while metric names stay stable. Root operations and direct children of a
source are never numbered, so span names stay bounded in loops. The numbering
applies to span names only. Metric families use the base name, with dots turned
into underscores (`http.request` → `http_request_count`).

**Manual lifecycle**, for callbacks, framework hooks and producer/consumer
handoffs:

```python
op = pyrock.start_operation("upload", attrs={"size": n})   # not activated in context
token = op.activate()           # optional: make it current for this context
...
op.end(error=exc)
op.deactivate(token)
```

### 4.3 Steps

Steps are lightweight spans that add their attrs to the parent operation and
create no metrics of their own.

```python
@pyrock.step("parse_payload")
def parse(raw: bytes) -> Payload:
    pyrock.register(bytes=len(raw))   # goes to the step's span and propagates to the parent op
    ...

with pyrock.step("validate", attrs={"rules": 12}) as step:
    step.event("rule_failed", rule="email")
```

`step()` accepts `attrs=`, `no_trace=` and `expected=`. An exception escaping a
step sets the step span status to error and records it, then propagates to the
operation as usual. The operation fails only if the exception also escapes the
operation. This matches Go: a step's errors propagate to the parent.

`pyrock.step()` with no active operation still creates a span if a trace is
active, and otherwise is a noop. This matches `StepFromContext`.

### 4.4 Sources

Sources represent long-lived processes. They prefix child operation names and
share attrs and labels with them.

```python
with pyrock.source("background.worker",
                   attrs={"worker.type": "async"},
                   metric_labels=["worker.type"]) as src:
    for job in jobs:
        src.sum("jobs_processed", 1)         # counter  → background_worker_jobs_processed
        src.gauge("queue_depth", q.qsize())  # gauge
        src.histogram("latency_ms", 52.3)    # histogram
        with pyrock.operation("process"):    # full name: background.worker.process
            handle(job)
```

`source()` also works as a decorator on long-running functions such as
`@pyrock.source("consumer")` on `async def run()`. Aggregation names are
prefixed with the source name and carry source labels plus static labels. As in
Go, `Source.done()` exists to keep the API symmetrical. Leaving the scope just
restores the previous context.

### 4.5 NoTrace

`no_trace=True` on an operation or step turns off span creation for that scope
and all of its children, through a context var. Metrics are still recorded and
logs still carry the enclosing trace IDs. This is meant for hot paths.

### 4.6 Context across threads, executors and callbacks

asyncio needs nothing extra. For threads:

```python
# Wrap a callable to run inside the current context (op, source, bedrock):
pool.submit(pyrock.bind(process, item))

# Or use an executor that copies context on every submit:
with pyrock.ContextThreadPoolExecutor(max_workers=8) as pool:
    pool.map(process, items)

# Or start a thread with context:
pyrock.Thread(target=consume).start()          # threading.Thread subclass that copies context
pyrock.Thread(target=consume, context=ctx)     # explicit context, as on 3.14's threading.Thread

# asyncio.to_thread already copies context, so it works unchanged.
```

`pyrock.bind(fn)` is a thin wrapper around `contextvars.copy_context().run`.
Executor and `Thread` are small subclasses. These are the only places pyrock
asks you to think about context, and they are stdlib types, so they can be
swapped in anywhere.

### 4.7 Module-level helpers vs. the instance

Every top-level function (`pyrock.operation`, `pyrock.info`, `pyrock.counter`, …)
resolves the active `Bedrock` from context. A method of the same name exists on
`Bedrock` for explicit use (`bedrock.operation(...)`). `pyrock.current()`
returns the active `Bedrock`, or the noop singleton. `pyrock.current_operation()`,
`pyrock.current_span()` and `pyrock.register()` act on whatever is innermost.

### 4.8 Noop behaviour

When `init()` hasn't run or isn't active in this context, every helper returns
a **preallocated singleton** noop (operation, step, source, counter, gauge,
histogram). Its `__enter__` returns itself and its methods are empty.

Decorators always wrap. `@pyrock.operation`, `@pyrock.step` and `@pyrock.source`
return a `functools.wraps` wrapper that looks up the active instance **at call
time**, so functions decorated at import time, before `init()`, are
instrumented once an instance is active. The noop path costs one
`ContextVar.get()` plus a direct call. The function is returned unchanged only
when an explicit noop `bedrock=` is given. Library authors can instrument
freely: apps that never call `init()` pay about one `ContextVar.get()` per
call. An e2e test (`tests/e2e/test_noop.py`) asserts with `tracemalloc` that
the noop path allocates nothing in steady state, the counterpart of Go's
`BenchmarkOperation_Noop`. There is no separate timing benchmark (§20). The
noop singletons live next to what they replace: operation, step and source in
`_operation`, the noop instance in `_bedrock`, and metrics in `_noop`.

---

## 5. Attributes

pyrock uses plain Python values rather than typed constructors:

```python
AttrValue = (
    str | bool | int | float | bytes
    | datetime.timedelta | datetime.datetime | BaseException
    | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
)
```

The `pyrock.attr` module covers the cases plain values can't:

```python
from pyrock import attr

attr.Event("retry", attempt=2)          # usable with register(), like attr.NewEvent
attr.error(exc, stack=True)             # explicit error attrs, like attr.ErrorWithStack
attr.Attrs({"a": 1}, b=2)               # ordered, last-write-wins set, like attr.Set
```

Type mapping to OTLP and Prometheus:

| Python | OTLP | Prometheus label / log |
|---|---|---|
| `str` | `stringValue` | as-is |
| `bool` | `boolValue` | `"true"` / `"false"` (checked before `int`, because `bool` subclasses `int`) |
| `int` | `intValue` (out-of-range i64 → string) | decimal |
| `float` | `doubleValue` | `repr`-style shortest form |
| `timedelta` | `doubleValue` in ms, key unchanged | e.g. `"5000ms"` in logs |
| `datetime` | RFC 3339 string | RFC 3339 |
| `bytes` | `bytesValue` | base64 |
| sequences | `arrayValue` | JSON array in logs. Not allowed as metric labels (rendered as `"_"`). |
| `BaseException` | error attrs (below) | |
| anything else | dropped and counted. Not `str()`-ed, to avoid leaking `repr`s. Matches Go's "unknown structured values are suppressed". | |

**Error normalization** (`attr.error`, used by `register(exc)`, `fail()` and
exception exit) produces the same keys as Go:

- `error` = `True` (marks failure), `error.type` = qualified class name
  (`builtins.ValueError`, `myapp.errors.NotFound`).
- `error.message`, only if `privacy.allow_error_details`.
- `error.fingerprint`: a stable hash of the type plus the module/qualname of the
  top N frames, with no line numbers, so it survives unrelated edits. It is 16
  lowercase hex characters.
- `error.chain`: types along `__cause__` / `__context__` as a `list[str]`, so
  wrapped errors are visible like Go's `%w` chains.
- `error.stack`: a formatted traceback, only with `stack=True` and
  `allow_source_paths`. Otherwise paths are trimmed to `module:function`.
- `ExceptionGroup`: `error.group.count` (an `int`) and `error.types` (a
  `list[str]`).

**Static attrs** are *resource* attributes on exported spans (as in Go),
top-level fields in log records, and labels on metrics.

---

## 6. Configuration

### 6.1 `Config`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    service: str = "unknown"

    # tracing
    trace_url: str | None = None
    trace_protocol: Literal["grpc", "http"] | None = None   # None = auto-detect, like Go
    trace_insecure: bool = False
    trace_sample_rate: float = 1.0
    trace_sampler: Sampler | None = None

    # logging
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_output: TextIO | None = None          # default sys.stderr
    log_add_source: bool = False
    log_canonical: bool = False
    log_install: Literal["root", "pyrock", "none"] = "root"   # Python-specific, see §8

    # metrics
    metric_prefix: str = ""
    metric_buckets: tuple[float, ...] | None = None
    metric_series_limit: int = 1000
    metric_family_limit: int = 1000
    runtime_metrics: bool = True

    # server
    server_enabled: bool = True
    server_addr: str = "127.0.0.1:9090"
    server_metrics: bool = True
    server_pprof: bool = False
    server_profile_handler: WSGIApp | None = None   # explicit debug handler, like Go
    server_read_timeout: float = 10.0
    server_read_header_timeout: float = 5.0
    server_write_timeout: float = 30.0
    server_idle_timeout: float = 120.0
    server_max_header_bytes: int = 1 << 20
    server_max_connections: int = 64              # Python-specific, see §11

    # privacy / environment / profiles
    data_policy: privacy.Policy | None = None
    disable_environment_detection: bool = False
    profiles: ProfileExportConfig | None = None

    shutdown_timeout: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Config: ...
    @classmethod
    def default(cls) -> Config: ...
    def replace(self, **changes) -> Config: ...      # dataclasses.replace + validation
```

- **Durations are `float` seconds**, which is the Python convention (`timeout=`
  in `socket`, `urllib`, `asyncio`). They also accept `timedelta`. Environment
  variables keep Go's syntax (`10s`, `500ms`, `1m30s`) so one `.env` file
  configures both languages. A bare number is read as seconds.
- **Env vars are identical:** `BEDROCK_SERVICE`, `BEDROCK_TRACE_URL`,
  `BEDROCK_SERVER_ADDR`, `BEDROCK_DISABLE_ENVIRONMENT_DETECTION` and every other
  variable in the Go table, plus the Python-only `BEDROCK_LOG_INSTALL` and
  `BEDROCK_SERVER_MAX_CONNECTIONS` (a positive integer).
- `log_level` takes level names only (`debug`, `info`, `warn`/`warning`,
  `error`, `critical`, any case); the log helpers also accept ints (PLAN
  decision 37).
- Validation matches Go: the sample rate must be in [0, 1], and
  `trace_sample_rate=0` disables recording. Buckets must be finite and strictly
  increasing. Timeouts must be positive. `server_pprof=True` without a
  `server_profile_handler` enables nothing, with a warning. Failures raise
  `pyrock.ConfigError(ValueError)` and list every problem, not just the first.

### 6.2 `pyrock.env.parse`

```python
from typing import Annotated
from dataclasses import dataclass, field
from pyrock.env import Env, parse

@dataclass
class AppConfig:
    bedrock: pyrock.Config = field(default_factory=pyrock.Config.from_env)  # nested: read from BEDROCK_*
    port: Annotated[int, Env("PORT")] = 8080
    database_url: Annotated[str, Env("DATABASE_URL", required=True)] = ""
    allowed_hosts: Annotated[list[str], Env("ALLOWED_HOSTS", sep=",")] = field(default_factory=list)
    debug: Annotated[bool, Env("DEBUG")] = False

cfg = parse(AppConfig)                     # raises env.ParseError listing all failures
bedrock = pyrock.init(cfg.bedrock)
```

Supported types: `str`, `int`, `float`, `bool` (`1/0/true/false/yes/no/on/off`),
`timedelta` (Go duration syntax), `list[T]`/`tuple[T, ...]`, `Literal[...]`,
`enum.Enum`, `Optional[T]`, and nested dataclasses. `parse` also accepts an
`environ=` mapping for tests.

---

## 7. Tracing

### 7.1 Model

`pyrock.trace` contains `Tracer`, `Span`, `SpanContext` (a frozen dataclass:
`trace_id: bytes`, `span_id: bytes`, `trace_flags: int`, `trace_state: TraceState`,
`is_remote: bool`), and `SpanKind`. Users rarely touch them directly, because
operations and steps are the interface. IDs come from `os.urandom` (fast, and
safe across forks). Hex formatting is cached.

`pyrock.current_span()` returns the innermost live span, or `None`.

### 7.2 Samplers

```python
from pyrock.trace import sampler

pyrock.init(trace_sampler=sampler.ParentBased(sampler.Ratio(0.1)))
```

`AlwaysOn`, `AlwaysOff`, `Ratio(r)` (deterministic on the trace-ID's low 8 bytes,
so every service makes the same decision for a trace), and `ParentBased(root)`
(the default). Sampler is a `Protocol` with
`should_sample(trace_id, name, parent_sampled) -> SamplingResult`. As in Go,
sampling controls export, not span creation.

**Differences from Go**, which pyrock makes on purpose (PLAN decision 26; the
README documents them): Go's `RatioSampler` draws a random number per span, whereas
`Ratio` depends only on the trace ID. Go's `ParentBasedSampler` consults the
root sampler when the parent is unsampled, whereas `ParentBased` drops spans
whose parent is unsampled.

### 7.3 Export

`trace_url` selects the protocol in the same way as Go:
`http(s)://…` or a URL with a path means OTLP/HTTP JSON (via `urllib`). A bare
`host:port` means OTLP/gRPC.

**gRPC with no dependencies** is the hardest part of the zero-dependency rule.
The stdlib has no HTTP/2, so pyrock ships `trace/otlp/_h2.py`, a deliberately
small HTTP/2 client:

- It handles only unary client calls on a single long-lived connection: TLS
  with ALPN `h2` (`ssl.SSLContext.set_alpn_protocols`), or h2c prior-knowledge
  when `trace_insecure`. It sends SETTINGS, HEADERS, DATA,
  WINDOW_UPDATE, PING and GOAWAY. It reads trailers for `grpc-status`.
- For HPACK it sends only literal-without-indexing headers with no Huffman
  coding. Decoding supports the static table, dynamic table and Huffman,
  because servers use them. Huffman decoding is a 257-entry table.
- Protobuf is a 150-line hand-written encoder (`_pb.py`) for the fixed OTLP
  `ExportTraceServiceRequest` schema, covering varints, fixed64 and
  length-delimited fields. There is no reflection and no `.proto` compilation.

The Go repo does the same (`internal/h2c`). Its `compat/otlp` suite becomes
pyrock's `compat` dependency group and `compat`-marked tests (`make compat`),
which install `protobuf`, `opentelemetry-proto` and `grpcio` **as test-only
dependencies**. It decodes our bytes with the official
schema and runs against a real collector.

`BatchProcessor` behaves as in Go: a bounded `queue` (default 2048 spans),
batches of 512, a 5-second schedule, a background thread, and drop-on-full with
counters. `bedrock.trace_export_stats()` returns
`BatchStats(exported, dropped, failed, queue_len)`.

Every export carries the instrumentation scope name `pyrock` and the package
version. Static attrs are sent as resource attributes.

---

## 8. Logging

pyrock builds on stdlib `logging` rather than adding a parallel logger.

**What `init()` installs** depends on `log_install`:

- `"root"` (default): installs a `pyrock.logs.BedrockHandler` on the root logger
  with `JSONFormatter` (or `TextFormatter`) and sets the level. All of your
  existing `logging.getLogger(__name__).info(...)` calls, and those of your
  libraries, become structured and trace-correlated. The previous root handlers
  and level are saved and restored on `shutdown()`.
- `"pyrock"`: configures only the `pyrock` logger. Use this when the app owns
  logging config (Django `LOGGING`, `dictConfig`). Add `pyrock.logs.CorrelationFilter`
  to your own handlers to get `trace_id`, `span_id` and static attrs.
- `"none"`: installs nothing.

**Direct helpers**, the same as Go's `bedrock.Info(ctx, …)`:

```python
pyrock.debug("cache warm", entries=1200)
pyrock.info("processing request", user_id="123")
pyrock.warning("slow upstream", timeout=timedelta(seconds=5))   # `warn` alias too
pyrock.error("job failed", exc)                                  # positional exception → error attrs
pyrock.log(logging.INFO, "custom", {"http.route": "/users"})
```

These go through the `pyrock` logger with the attrs placed in `extra`, so they
pass through the same handlers, filters and privacy policy. Standard logging
works too:

```python
log = logging.getLogger(__name__)
log.info("user loaded", extra={"user.id": uid})   # extra keys become structured fields
log.exception("boom")                             # exc_info → error.* fields (privacy-filtered)
```

JSON shape (identical keys to Go):

```json
{"time":"2026-11-05T12:00:00.123Z","level":"INFO","msg":"processing request",
 "service":"users-api","env":"production","user_id":"123",
 "trace_id":"0af7651916cd43dd8448eb211c80319c","span_id":"b7ad6b7169203331",
 "logger":"myapp.users"}
```

`level` uses Go's names: `DEBUG`, `INFO`, `WARN` and `ERROR`. Python's
`CRITICAL` stays `CRITICAL`. Static attrs are top-level fields.

`logger` is extra on the Python side. `log_add_source` adds `source` as
`module:function:line`, or the full path when `allow_source_paths`.

**Canonical logs** (`log_canonical=True`): when an operation ends it emits
`operation.complete` at INFO with `operation`, `duration_ms`, `success`,
`attributes`, `trace_id` and `span_id`. It is the same record Go produces. A
failed operation's record also has top-level `error`, `error.type` and
`error.fingerprint`. `error.message` is added only with `allow_error_details`.

---

## 9. HTTP

### 9.1 WSGI middleware

```python
from pyrock.wsgi import Middleware

app = Middleware(app,                         # Flask: app.wsgi_app = Middleware(app.wsgi_app)
    operation_name="http.request",
    additional_labels=["user_type"],
    additional_attrs=lambda environ: {"user_type": environ.get("HTTP_X_USER_TYPE", "")},
    success_codes=range(200, 400),
    trace_propagation=True,
    route=lambda environ: environ.get("pyrock.route"),   # see "routes" below
    bedrock=None,                             # explicit instance; see "instances and threads"
)
```

### 9.2 ASGI middleware

```python
from pyrock.asgi import Middleware

app = Middleware(app, operation_name="http.request")   # FastAPI/Starlette: app.add_middleware(Middleware)
```

Both middlewares:

- Extract `traceparent`/`tracestate`. Header names are case-insensitive, and
  repeated `tracestate` headers are joined with commas. An invalid traceparent
  starts a new trace and ignores tracestate.
- Open a `SERVER`-kind operation with the default attrs `http.method`,
  `http.route`, `http.scheme`, `http.host` and `http.status_code`.
  `http.user_agent` is sensitive and off by default, as in Go: it is emitted
  only when the policy's `attribute_keys` allowlist matches it
  (`"http.user_agent"` or `"http.*"`). The default
  labels are `http_method`, `http_route` and `http_status_code`.
- Capture the **final** status. In WSGI that is the last `start_response` call.
  In ASGI it is `http.response.start`, skipping informational 1xx responses.
- When an exception escapes the app, record a failure with `http.status_code=500`
  and re-raise (Go's panic behaviour). If the exception comes after headers were
  sent, it still marks failure and is re-raised, but `http.status_code` stays
  the status already sent. A response with a status outside
  `success_codes` marks failure without raising.
- Measure duration until the response body finishes: when the WSGI iterable's
  `close()` runs, or at the last ASGI `more_body=False`. This way streaming
  responses are timed correctly. Because the WSGI middleware wraps the
  response iterable, a server's `wsgi.file_wrapper` fast path is not used for
  instrumented responses.
- ASGI passes `lifespan` and `websocket` scopes through. WebSocket
  instrumentation is opt-in (`websocket=True`), and covers the whole connection
  as one `SERVER` operation named `http.websocket` (an explicit
  `operation_name=` overrides it), with `http.route` and `http.scheme`
  (`ws`/`wss`). It ends when the connection closes. In ASGI,
  `operation_name=None` (the default) means `http.request` for HTTP and
  `http.websocket` for websockets.

**Instances and threads.** There is **no** process-wide default instance, so a
plain `threading.Thread` still sees the noop. `wsgi.Middleware`,
`asgi.Middleware` and `http.instrument_http_server` capture the instance when
they are constructed: either `bedrock=` or `pyrock.current()` at that point. If
that is the noop, they resolve `current()` per request instead. Each request is
handled inside a context with that instance active, so handler code on server
worker threads is instrumented.

**Routes and cardinality.** Raw paths would break the cardinality guarantee, so
`http.route` must be a *template* or `"_"`. Templates come from, in order:

1. the `route=` callable, if given (a `None` result falls through to the next
   source);
2. the ASGI `scope["route"]` path format (Starlette/FastAPI set this), or
   `scope["pyrock.route"]` / `environ["pyrock.route"]` if a framework hook sets it;
3. `"_"` otherwise. Unmatched and custom routers share one series, as in Go.

Small framework recipes, documented and not imported, set the route key:
a Flask `before_request` that sets `request.url_rule.rule`, a Django middleware
that sets `resolver_match.route`, a Starlette router hook. Each is about five
lines, in the same spirit as Go's copy-in `example/grpc`.

`pyrock.http.instrument_http_server(MyHandler, bedrock=None)` wraps
`http.server.BaseHTTPRequestHandler` subclasses for stdlib-only services, with
the same instance rules as the middlewares.

### 9.3 Client

```python
import pyrock.http

resp = pyrock.http.get("https://api.example.com/users")             # one-off; a CLIENT span named "HTTP GET"
resp = pyrock.http.post(url, json={"name": "a"})                     # json= / data= / headers= / timeout=
resp = pyrock.http.request("PUT", url, data=body, headers={"Authorization": f"Bearer {tok}"})

# Reusable opener (the analogue of NewClient). It keeps your other urllib handlers.
opener = pyrock.http.build_opener(urllib.request.HTTPCookieProcessor())
opener.open(req, timeout=30)

# Add tracing to an existing opener or urllib setup
handler = pyrock.http.TracingHandler()     # a urllib.request.BaseHandler
```

Client spans record `http.method`, `http.host`, `http.scheme` and
`http.status_code`. Raw outgoing paths and URLs (`http.target`, `http.url`) are
emitted only when allowlisted in the policy's `attribute_keys`. Credentials,
query and fragment are always stripped, and URL userinfo is never turned into
auth. 4xx and 5xx responses set error status. Redirects
keep the same trace.

`pyrock.http.request` returns a small `Response` object (`status`, `headers`,
`read()`, `json()`, `text`, and context manager support) wrapping the
`http.client.HTTPResponse`. `HTTPError` is caught and returned as a response by
default (`raise_for_status=False`). This is friendlier than raw `urllib`, which
raises. With `raise_for_status=True`, `urllib.error.HTTPError` is raised.

For third-party clients, one primitive and two recipes cover most code:

```python
pyrock.inject_headers(headers)       # writes traceparent/tracestate into any MutableMapping

# httpx: event hook          → client = httpx.Client(event_hooks={"request": [pyrock_httpx_hook]})
# requests: Session adapter  → session.mount("https://", PyrockAdapter())
```

The recipes live in `examples/recipes/`, not in the package, because the
package must not import `httpx` or `requests`.

**Limitation:** urllib has no hook for a request that fails before a
response. Openers from `pyrock.http.build_opener` (and so `request`/`get`/
`post`) end the span with the error; a `TracingHandler` added to a plain
urllib opener never exports the span of such a request.

### 9.4 Propagators

```python
class Propagator(Protocol):
    def extract(self, carrier: Any) -> SpanContext | None: ...
    def inject(self, carrier: Any, span_context: SpanContext | None = None) -> None: ...
```

The shipped `pyrock.trace.http.HTTPPropagator` works on any header `Mapping`,
including WSGI-environ adapters. `pyrock.trace.w3c` offers
`parse_traceparent()`, `format_traceparent()`, `parse_tracestate()`,
`format_tracestate()`, `is_valid_tracestate_key()` and
`is_valid_tracestate_value()`, with the same validation rules and the 32-entry
limit.

To continue a trace from a non-HTTP transport:

```python
class KafkaPropagator:
    def extract(self, headers: list[tuple[str, bytes]]) -> SpanContext | None:
        for k, v in headers:
            if k == "traceparent":
                try:
                    return w3c.span_context_from_traceparent(v.decode())
                except w3c.InvalidTraceparent:
                    return None
        return None

    def inject(self, headers: list[tuple[str, bytes]], span_context=None) -> None:
        sc = span_context or pyrock.current_span_context()
        if sc:
            headers.append(("traceparent", w3c.format_traceparent(sc).encode()))

with pyrock.operation("consume", remote_parent=KafkaPropagator().extract(msg.headers),
                      kind=SpanKind.CONSUMER):
    ...
```

The gRPC interceptor example (Go's `example/grpc`) becomes
`examples/recipes/grpc_interceptors.py`, a client and server interceptor for `grpcio`
to copy into a project.

---

## 10. Metrics

### 10.1 Direct metrics

```python
requests = pyrock.counter("api_requests_total", "Total API requests", labels=["endpoint", "method"])
requests.labels(endpoint="/users", method="GET").inc()
requests.inc()                               # static labels only; declared labels → "_"

depth = pyrock.gauge("queue_depth", "Current depth", labels=["queue_name"])
depth.labels(queue_name="jobs").set(42)
depth.inc(); depth.dec(); depth.add(5); depth.sub(2)

latency = pyrock.histogram("query_latency_ms", "Query latency", buckets=None, labels=["db_type"])
latency.labels(db_type="postgres").observe(123.45)

with latency.labels(db_type="pg").time():        # Pythonic extra: context-manager timer (ms)
    run_query()

@latency.labels(db_type="pg").time()             # ...or as a decorator
def q(): ...
```

- `.labels(**kw)` follows the `prometheus_client` naming that Python users
  know, and also accepts a mapping for dotted keys: `.labels({"http.method": "GET"})`.
  Go's `.With(attrs…)` maps to this.
- Bound children are cached, so calling `.labels(...)` in a loop is cheap.
- Same rules as Go: static labels are added automatically. Undeclared label
  keys raise `ValueError` at the call site, so mistakes are loud. A schema
  conflict when re-registering a name with different labels or types raises
  `MetricConflictError` at registration. Re-registering the same schema returns
  the existing metric. Past `metric_series_limit`, new series are dropped and
  counted in `registry.dropped_series()`. Past `metric_family_limit`,
  registration raises. A different `help` text on re-registration is not a
  conflict: the first registration wins, as in Go. Unlike Go, counters raise
  `ValueError` on negative or NaN increments (PLAN decision 33).
  `MetricConflictError` and `MetricLimitError` (like `Activation`) are
  importable from `pyrock` but are not in `__all__` (decision 39).
- Counters and gauges use one `threading.Lock` per series (correct on
  free-threaded builds). Histograms use bucket arrays and `bisect`.

### 10.2 Runtime metrics

`runtime_metrics=True` registers a collector that runs on each scrape or push
and reports the Python equivalents of Go's runtime metrics:

| Metric | Source |
|---|---|
| `python_gc_collections_total{generation}` / `python_gc_collected_total` / `python_gc_uncollectable_total` | `gc.get_stats()` |
| `python_gc_pause_seconds` (histogram) | `gc.callbacks` timing |
| `python_threads` | `threading.active_count()` |
| `python_asyncio_tasks` | `len(asyncio.all_tasks(loop))` for loops registered via `pyrock.asgi` / `pyrock.track_loop()` |
| `python_info{implementation,version,gil}` | `sys.implementation`, `sys._is_gil_enabled()` (3.13+) |
| `process_cpu_seconds_total`, `process_resident_memory_bytes`, `process_virtual_memory_bytes`, `process_open_fds`, `process_max_fds`, `process_start_time_seconds` | `resource.getrusage`, `/proc/self/{stat,statm,fd,limits}` (Linux), with `resource` fallbacks elsewhere |
| `python_tracemalloc_traced_bytes` | only when tracemalloc is already tracing |

`process_*` names follow the Prometheus client conventions, so the standard
dashboards work.

### 10.3 Prometheus exposition

`pyrock.metric.prometheus.encode(registry) -> bytes` validates the **whole
snapshot first**: bucket bounds finite and increasing, cumulative counts
monotonic and no larger than the count, names and labels valid. Then it writes
text format 0.0.4. A validation failure raises `ExpositionError`, and `/metrics`
returns 500 with no partial body, as in Go. Numbers use Go's `%g` formatting.

---

## 11. Observability server

When `server_enabled`, `init()` starts a `ThreadingHTTPServer` on a daemon
thread at `127.0.0.1:9090` (loopback only by default).

| Endpoint | Behaviour |
|---|---|
| `/metrics` | Prometheus text (if `server_metrics`) |
| `/health`, `/ready` | `200 ok` |
| `/debug/pprof/*` | Only if `server_pprof` **and** `server_profile_handler` are set (§12.3). The handler receives the request with `PATH_INFO` unchanged. |

Security defaults match Go:

| Setting | Implementation in Python |
|---|---|
| `read_header_timeout=5s` (Slowloris) | The handler reads the request line and headers itself on a socket with a **deadline** (the remaining time is recomputed before each `recv`), not a per-`recv` idle timeout, so a slow drip still trips it. |
| `read_timeout=10s`, `write_timeout=30s` | Deadline-based socket timeouts per phase |
| `idle_timeout=120s` | Keep-alive wait timeout between requests |
| `max_header_bytes=1MiB` | Counted while reading. Over the limit returns 431 and closes. |
| Connection cap | `ThreadingHTTPServer` subclass with a bounded semaphore (default 64 concurrent connections, `server_max_connections` / `BEDROCK_SERVER_MAX_CONNECTIONS`) and reject-on-full: a rejected connection gets `503` and is then closed. Python threads are more expensive than goroutines, so this guard is needed here. |
| Graceful shutdown | `shutdown()` stops accepting, waits for in-flight requests up to `shutdown_timeout`, then closes. |

The server can also be run manually:

```python
from pyrock import server
srv = server.Server(bedrock.metrics, server.Config(addr="0.0.0.0:9090"))  # explicit opt-in to wildcard
srv.start()          # background thread; srv.serve_forever() to block
...
srv.shutdown(timeout=15)
```

`server.metrics_app(registry)` returns a plain WSGI app, so `/metrics` can be
mounted inside an existing web server instead.

---

## 12. Profiling

Go gets pprof from the runtime. Python doesn't, so pyrock builds each profile
type from stdlib pieces and encodes the results **natively** as OTLP Profiles
(`v1development` v0.2.0, the same target and dictionaries as Go) and as pprof
`profile.proto`. Both encoders are hand-written protobuf.

### 12.1 Profile types

| `ProfileType` | Go meaning | Python implementation | Sample types |
|---|---|---|---|
| `CPU` | CPU samples | A sampler thread wakes at 100 Hz (configurable), reads `sys._current_frames()`, and keeps threads whose CPU clock advanced (`time.pthread_getcpuclockid` + `clock_gettime` on Linux, with a wall-clock fallback marked by a `profile.note` attribute). Samples carry no asyncio task label (the planned 3.12+ task-identity label was not implemented). | `samples/count`, `cpu/nanoseconds` |
| `WALL` *(Python extra)* | — | The same sampler without the CPU filter. Useful for I/O-bound async apps. | `samples/count`, `wall/nanoseconds` |
| `HEAP` | allocation samples | `tracemalloc` snapshot diffs per interval (`traceback_limit` 32). tracemalloc starts only when `HEAP` is selected; on export stop it is stopped only if pyrock started it. The cost is documented (around 10–30% allocation overhead). | `alloc_space/bytes`, `inuse_space/bytes`, `alloc_objects/count` |
| `GOROUTINE` | goroutine stacks | Stacks of all threads (`sys._current_frames`) plus pending asyncio tasks (`Task.get_stack`) on tracked loops. Exposed as `ProfileType.THREADS`, with `GOROUTINE` as an alias for parity. | `threads/count`, `tasks/count` |
| `MUTEX` / `BLOCK` | contention | Python exposes no global contention data. pyrock provides **opt-in instrumented primitives** as drop-in replacements that record wait time when contended: `pyrock.profile.Lock` and `RLock` feed `MUTEX` (sampled by `mutex_sample_fraction`, one in N, scaled by N, as in Go), `Condition` and `Semaphore` feed `BLOCK` (sampled by `block_sample_rate` seconds, as in Go). Both are disabled (and counted as skipped by the exporter) while their sampling setting is 0. With no instrumented locks they are empty and say so in a `profile.note` attribute. | `contentions/count`, `delay/nanoseconds` |

The sampling profiler is a Python thread, so it measures what the interpreter
can see. Time in C extensions shows up under the Python frame that called
them. Overhead at 100 Hz is expected to be about 1–3% per busy thread (this
is not benchmarked in CI). Unlike `sys.setprofile`, it doesn't conflict with debuggers or coverage.
`cProfile` users are not affected. The Go rule "CPU profiling is process-wide
and conflicting collection is skipped, never stopped" becomes a check for
another pyrock sampler in the process (or a `PYROCK_PROFILER_OWNER` marker).
If one exists, collection is skipped and counted. The slot is per process:
a claim inherited through `fork` does not block the child.

### 12.2 OTLP export

```python
from pyrock.profile import ProfileExportConfig, ProfileType

pyrock.init(
    server_enabled=False,
    profiles=ProfileExportConfig(
        endpoint="https://collector.example/v1/profiles",
        headers={"Authorization": f"Bearer {token}"},
        types=[ProfileType.CPU, ProfileType.HEAP],
        interval=60.0,
        cpu_sample_duration=10.0,          # must be > 0 and < interval
        timeout=10.0, max_bytes=8 << 20, retries=3,
        ssl_context=None, allow_http=False,
        mutex_sample_fraction=0, block_sample_rate=0.0,
    ),
)
```

This matches Go's `ProfileExportConfig`, including its limits: `retries` is
0–3 and `max_bytes` at most 32 MiB (PLAN decision 36). The default types are
CPU, HEAP and THREADS. `types=[]` disables collection. Redirects are never
followed. Unlike Go, the exporter collects every type once immediately on start,
then once per `interval` (an overrunning collection drops missed ticks). Each
upload carries an `x-profile-type` header.
`bedrock.profile_stats()` returns exports, failures and skips.
`bedrock.start_profile_export(cfg)` starts an extra exporter that is also
stopped by `shutdown()`. Profile labels go through the privacy policy only if
`allow_profile_labels` is set.

### 12.3 Optional debug handler

```python
from pyrock.profile import debug
pyrock.init(server_pprof=True, server_profile_handler=debug.app())
```

`/debug/pprof/profile?seconds=N` (`N` above 30 is rejected with `400`, as in Go), `/heap`, `/goroutine`
(threads and tasks), `/mutex` and `/block` return **gzipped pprof** that works
with `go tool pprof` and Pyroscope. `?debug=1` on `goroutine` returns text
stacks. GET only. `/heap` reports allocations since the previous `/heap`
request and needs tracemalloc to be tracing already (HEAP export or
`PYTHONTRACEMALLOC`); otherwise the profile is empty with a note. `/profile`
answers `503` while another sampler owns the process. `/debug/pprof` without a
trailing slash is 404 (PLAN decision 38). Importing `pyrock` registers
nothing. This module has to be imported explicitly, matching Go's isolation of
`net/http/pprof`.

---

## 13. Environment auto-detection

This runs during `init()` unless `disable_environment_detection`. Every
detector runs on its own daemon thread under a **500 ms total budget**; a
detector that misses it is abandoned and counted. Exceptions are caught and
counted, and init never blocks or fails because of detection. Cloud metadata
is probed only when a cheap local hint (DMI identity files, platform
environment variables) suggests that cloud, so ordinary hosts never wait on
link-local addresses.

| Attribute | Source |
|---|---|
| `process.pid` | `os.getpid()` |
| `process.executable.name` | `sys.argv[0]` basename, or the `-m` module name |
| `process.runtime.name` / `.version` | `sys.implementation` *(Python extra; OTel semconv)* |
| `host.name` | `socket.gethostname()` |
| `service.version` | `git rev-parse` on the source tree (subprocess, 200 ms timeout), else `importlib.metadata.version(<top-level dist>)`, else unset. The first 12 characters are kept, matching Go's `vcs.revision`. |
| `k8s.pod.name`, `k8s.namespace`, `k8s.node.name`, `k8s.container.name` | The same env vars and files as Go |
| `container.id` | `/proc/self/cgroup` and `/proc/self/mountinfo` (cgroup v2), skipped in k8s |
| `cloud.provider`, `cloud.region`, `host.id` | AWS IMDSv2 (token PUT, then GET) and GCP metadata (`Metadata-Flavor: Google`) via `urllib` with a 500 ms timeout |

Privacy defaults are the same as Go: resource keys that identify a host,
process, container or k8s object are **dropped** unless the policy's
`resource_keys` allowlists them. They are never added to metric labels
automatically.

---

## 14. Privacy

```python
from pyrock import privacy

pyrock.init(data_policy=privacy.Policy(
    attribute_keys=None,              # None = keep non-sensitive fields; list = strict allowlist (supports "group.*")
    resource_keys=["service.version", "cloud.region"],
    allow_error_details=False,
    allow_source_paths=False,
    allow_profile_labels=False,
    max_string_length=1024,
    redact=lambda path, value: (value, True),   # return (new_value, keep)
))
```

This is the same model as Go's `privacy.Policy`. One walker enforces it on span
attrs, events, status messages, log records (including `extra` and nested
dicts/lists), metric label values and profile labels.

- Traversal is limited to 4,096 nodes. Cycles and repeated aliases are replaced
  with `"[cycle]"` / `"[alias]"` markers (tracked by `id()`).
- `redact` gets qualified paths such as `"request.headers.authorization"`, must
  be thread-safe, and any exception it raises drops the value (fail closed).
- The `error.*` and `exception.*` namespaces stay private through group
  ancestry unless `allow_error_details` is set.
- Unknown object types are suppressed. pyrock never calls `str()` or `repr()`
  on arbitrary objects, which is a common way secrets leak in Python logging.
- The profile encoder rejects post-policy label sets above 64 items, 4,096
  nodes or depth 8, rather than silently truncating them.

---

## 15. Concurrency, async and process model

- **Thread safety:** all shared state (registry, series, batch queue, numbering
  counters) is protected by `threading.Lock`. CI runs the test suite on
  free-threaded 3.14t with `PYTHON_GIL=0`. The equivalent of Go's race
  detector is a CI stress job (`make stress`) that repeats the fork, context,
  batch and server tests 10 times on 3.14t, plus `sys.setswitchinterval(1e-6)`
  stress inside individual concurrency tests.
- **Thread context:** pyrock does not monkey-patch `threading` (PLAN decision
  2). Context crosses threads only through `bind`, `ContextThreadPoolExecutor`
  and `pyrock.Thread`, or through the middlewares, which activate their
  captured instance per request (decision 18). When the interpreter inherits
  context into new threads (`sys.flags.thread_inherit_context`, the default on
  free-threaded 3.14), plain threads see the active instance; pyrock does not
  fight that.
- **Async:** nothing pyrock does blocks the event loop. Spans are queued with
  `put_nowait`, metrics take a short lock, logs go through `logging`, and
  network I/O happens on worker threads. `ashutdown()` runs the flush in
  `asyncio.to_thread`.
- **`fork()` (gunicorn, uWSGI, `multiprocessing` fork):** threads don't survive
  a fork, so pyrock registers `os.register_at_fork`:
  - *before:* hold the live-instance registry lock and note which profile
    exporters are running;
  - *after in parent:* release it;
  - *after in child:* replace every lock a parent thread might have held,
    reset process-wide profiling state, build a fresh batch pipeline (its
    worker starts lazily), forward the reset to wrapping exporters and cloud
    wiring, restart the push and profile threads, reseed nothing (IDs come
    from `os.urandom`), zero all metric series, and **don't** start the
    observability server, because the parent owns the port. `process.pid` is
    refreshed.

  Pre-fork servers therefore need a metrics strategy. The recommended practice
  (PLAN decision 3) is to call `pyrock.init()` in the worker's post-fork hook
  (`post_worker_init` / `@postfork`), with `server_addr="127.0.0.1:0"` plus
  service discovery or with `server_enabled=False` and the cloud metrics push.
  There is no generic push-gateway mode and no `{worker}` substitution in
  `server_addr` in v0.
- **Subinterpreters (3.12+/3.14 `concurrent.interpreters`):** each interpreter
  that calls `init()` gets its own instance. Nothing is shared.

---

## 16. Cloud backend

```python
pyrock.init(cloud=pyrock.cloud("brk_live_abc123",
    endpoint="https://ingest.bedrock.dev",
    insecure=False,
    push_interval=15.0,
    profile_interval=60.0,
    profile_cpu_sample_duration=10.0,
    profiles=[ProfileType.CPU, ProfileType.HEAP, ProfileType.THREADS],   # [] disables cloud profiling
))
```

This is the same wiring as Go's `wireCloud()`:

- Traces use OTLP/gRPC over `_h2` with the `x-bedrock-key` metadata header. The
  batch processor is swapped for a larger cloud one.
- Traces go to `traces_endpoint` when set (a bare `host:port` or a URL; a
  URL's scheme is stripped and the default port added, as in Go's endpoint
  normalisation). Otherwise the target is derived from `endpoint`.
- `RetryExporter` makes one initial attempt plus one retry per
  `retry_backoff` entry: by default `(1.0, 2.0, 4.0)` seconds, so 4 calls in
  all, as in Go. `retry_backoff` is validated (each entry finite and `>= 0`, at
  most 10 entries, a list is accepted and stored as a tuple), and `()` disables
  retries. It honours shutdown deadlines (it waits on a `threading.Event`, not
  `time.sleep`).
- `FanOutExporter`: when `trace_url` is also set, each batch goes to both
  exporters. Every exporter is always called, and the first error is returned.
- Metrics push sends Prometheus text with `Authorization: Bearer` at each
  interval. Each request has a bounded lifetime. If a push is still in flight
  the next one is skipped. There is a final flush on shutdown, bounded by the
  caller's timeout.
- Profile push runs the collectors from §12 at `profile_interval` and uses
  OTLP profiles.

`pyrock.cloud(...)` takes every `CloudConfig` field as a keyword (including
`retry_backoff` and `traces_endpoint`) and returns a `CloudConfig` dataclass. The `api_key` field is
excluded from `repr`/`__eq__` (`field(repr=False)`), so keys don't show up in
logs or tracebacks.

---

## 17. Testing support (`pyrock.testing`)

Go tests reach into `MetricsRegistry()`. Python users expect fixtures and
helpers, so pyrock provides an in-memory harness:

```python
from pyrock.testing import capture

def test_process_user():
    with capture(service="test") as t:               # in-memory exporters; no server, no detection, no network
        process_user("123")

    op = t.spans.one(name="process_user")
    assert op.attrs["user_id"] == "123"
    assert not op.failed
    assert t.metrics.value("process_user_count", status="active") == 1
    assert t.logs.find(msg="processing request")["trace_id"] == op.trace_id

async def test_async_path():
    with capture() as t:
        await handler()
    assert t.spans.names() == ["db.get_user", "db.get_user[1]", "http.request"]   # end order
```

Spans are listed in the order they ended, so children come before their
parent. `capture()` defaults to `server_enabled=False`,
`disable_environment_detection=True`, `runtime_metrics=False` and
`log_install="pyrock"`, ignores the environment, and exports spans
synchronously to memory.

Spans are complete once the block exits, while `t.metrics.text()` and
`t.metrics.value()` also work inside it. `t.spans.find(kind=...)` filters by
kind. The filter helpers (`metrics.value`, `logs.find`, `logs.find_all`,
`spans.find`, `spans.one`) accept an optional positional mapping for dotted
keys, as in `t.metrics.value("worker_jobs", {"worker.type": "async"})`, in
addition to keywords. On exit, `capture()` restores the previously active
instance, so nested captures compose.

`capture()` is an ordinary context manager, so it needs no pytest plugin. A
pytest entry point (`pyrock_capture` fixture) is registered through
`entry_points` metadata only. It loads only when pytest is present, which keeps
the package dependency-free. Loading the plugin imports the pyrock core (about
40 ms once per pytest session); avoiding that would need an entry point
outside the package and is deferred (PLAN decision 40).

---

## 18. End-to-end examples

### 18.1 FastAPI / Starlette (ASGI)

```python
import pyrock
from pyrock.asgi import Middleware
from fastapi import FastAPI

bedrock = pyrock.Bedrock(service="users-api", static_attrs={"env": "prod"})   # live, not activated
app = FastAPI(lifespan=lambda app: bedrock)        # Bedrock is an async context manager → shutdown on exit
app.add_middleware(Middleware, bedrock=bedrock)    # pinned: active in every request

@app.get("/users/{user_id}")
@pyrock.operation("get_user", metric_labels=["tier"])
async def get_user(user_id: str):
    user = await fetch_user(user_id)
    pyrock.register(tier=user.tier)
    pyrock.info("user loaded", user_id=user_id)
    return user

@pyrock.step("fetch_user")
async def fetch_user(user_id: str):
    resp = await asyncio.to_thread(pyrock.http.get, f"http://profiles/{user_id}")  # context copied by to_thread
    return User(**resp.json())
```

### 18.2 Flask (WSGI)

```python
app = Flask(__name__)
app.wsgi_app = pyrock.wsgi.Middleware(app.wsgi_app,
                                      route=lambda env: env.get("werkzeug.request").url_rule.rule
                                      if env.get("werkzeug.request") and env["werkzeug.request"].url_rule else None)
pyrock.init(service="billing")
```

### 18.3 Background worker

```python
def main():
    with pyrock.init(service="worker"), \
         pyrock.source("background.worker", attrs={"worker.type": "async"},
                       metric_labels=["worker.type"]) as src, \
         pyrock.ContextThreadPoolExecutor(8) as pool:
        for job in queue:
            src.sum("jobs_received", 1)
            pool.submit(process_job, job)       # context (source + bedrock) copied per submit

@pyrock.operation("process_job")                # → background.worker.process_job
def process_job(job):
    pyrock.register({"job.id": job.id})
    job.run()                                   # raise → failure metric + error span + re-raise
```

---

## 19. Go → Python API map

| Go | Python |
|---|---|
| `bedrock.Init(ctx, opts…)` → `(ctx, close)` | `pyrock.init(config, **overrides)` → `Bedrock` (context manager) |
| `bedrock.New(cfg, attrs…)` | `pyrock.Bedrock(config, static_attrs=…)` (not activated) |
| `WithConfig`, `WithStaticAttrs`, `WithLogLevel`, `WithCloud` | positional `config`, `static_attrs=`, `log_level=`, `cloud=` |
| `FromContext(ctx)` | `pyrock.current()` |
| `Operation(ctx, name, Attrs, MetricLabels, NoTrace, WithRemoteParent, Success, Failure)` | `pyrock.operation(name, attrs=, metric_labels=, no_trace=, remote_parent=)`, `op.succeed()`, `op.fail()` |
| `op.Register(ctx, …)` / `op.Event` / `op.Done` / `op.End(EndSuccess/EndFailure)` | `op.register(...)` / `op.event(...)` / `with`-exit or `op.done()` / `op.end(success=, error=)` |
| `Step` / `StepFromContext` | `pyrock.step(name, attrs=, no_trace=)` |
| `Source`, `SourceAttrs`, `SourceMetricLabels`, `src.Sum/Gauge/Histogram` | `pyrock.source(name, attrs=, metric_labels=)`, `src.sum/gauge/histogram` |
| `attr.String/Int/…/Any` | plain values |
| `attr.Error`, `attr.ErrorWithStack`, `attr.NewEvent`, `attr.Set` | exceptions, `attr.error(exc, stack=True)`, `attr.Event`, `attr.Attrs` |
| `Debug/Info/Warn/Error/Log(ctx, …)` | `pyrock.debug/info/warning/error/log(...)` and stdlib `logging` |
| `Counter/Gauge/Histogram(ctx, name, help, labels…)`, `.With(...)` | `pyrock.counter/gauge/histogram(name, help, labels=[...])`, `.labels(...)` |
| `HTTPMiddleware` + `WithOperationName/AdditionalLabels/AdditionalAttrs/SuccessCodes/TracePropagation` | `pyrock.wsgi.Middleware` / `pyrock.asgi.Middleware` with matching kwargs |
| `NewClient`, `Get`, `Post`, `Do` | `pyrock.http.build_opener`, `get`, `post`, `request`, `TracingHandler` |
| `trace.Propagator`, `trace/w3c`, `trace/http` | `pyrock.trace.Propagator`, `pyrock.trace.w3c`, `pyrock.trace.http` |
| `trace.Sampler`, `AlwaysSampler`, `NeverSampler`, `RatioSampler`, `ParentBasedSampler` | `sampler.AlwaysOn`, `AlwaysOff`, `Ratio`, `ParentBased` |
| `Config`, `DefaultConfig`, `FromEnv`, `MustFromEnv` | `Config`, `Config.default()`, `Config.from_env()` (raises; there is no separate "must" form) |
| `env.Parse[T]()` | `pyrock.env.parse(T)` |
| `privacy.Policy` | `pyrock.privacy.Policy` |
| `server.New(reg, server.Config)` | `pyrock.server.Server(registry, server.Config)` |
| `ProfileExportConfig`, `ProfileType*`, `StartProfileExport`, `ProfileStats` | `pyrock.profile.ProfileExportConfig`, `ProfileType.*`, `bedrock.start_profile_export`, `bedrock.profile_stats()` |
| `profile.Handler()` | `pyrock.profile.debug.app()` |
| `b.Shutdown(ctx)` | `bedrock.shutdown(timeout=)` / `await bedrock.ashutdown()` |
| `b.Tracer()`, `b.Metrics()`, `b.Logger()`, `b.TraceExportStats()` | `bedrock.tracer`, `bedrock.metrics`, `bedrock.logger`, `bedrock.trace_export_stats()` |

---

## 20. Quality bar and tooling

These mirror Bedrock's CI rules:

- **Matrix:** CPython 3.11, 3.12, 3.13, 3.14, the 3.15 pre-release and
  free-threaded 3.14t (`PYTHON_GIL=0`) on Linux; 3.14 on macOS and Windows
  (fork tests and Linux-only detectors skip there). PyPy is not tested.
- **Checks:** `ruff` (lint + format), `mypy --strict` on `src`, 80% branch
  coverage (forked children and subprocesses included), a stdlib-only import
  test, a dependency-boundary test, a lazy-import and import-cost test, a
  public-API contract test, and the `compat` suite (official OTLP protos and a
  real grpcio collector) with test-only deps. A wheel check asserts a pure
  `py3-none-any` wheel with no `Requires-Dist`. `make ci` mirrors the gating
  jobs; `make matrix` runs every interpreter locally through uv.
- **Deviations (PLAN decision 41):** there is no `pyright` gate and no
  separate `bench/` project. In their place, a CI stress job repeats the
  concurrency-sensitive tests 10 times on 3.14t (`make stress`), unit
  performance tests in `tests/unit/perf/` pin the fast paths to the reference
  behaviour, and an e2e test asserts the noop path does not allocate.
- **Tests** check observable contracts: emitted OTLP bytes, exposition text,
  log JSON. They avoid testing internals and use events and barriers instead of
  `sleep`. Tests are the contract: implementation changes may not edit them to
  pass.
- `CHANGELOG.md` with an *Unreleased* section, plus `CONTRIBUTING.md` and
  `SECURITY.md` adapted from Bedrock's policies. Releases are
  maintainer-driven tags, and tags are never moved. Publishing to PyPI is
  manual for now; trusted publishing is planned.

---

## 21. Open questions (resolved)

The questions this design left open were decided during implementation; the
binding record is `PLAN.md`.

1. **Distribution and import name:** both are `pyrock` (PLAN decision 1).
2. **Thread context default:** no monkey-patching and no
   `inherit_thread_context` option. Context crosses threads only through
   `bind`, `ContextThreadPoolExecutor` and `pyrock.Thread` (decision 2), and
   the middlewares activate their captured instance per request (decision 18
   and its amendment for interpreters that inherit context).
3. **Pre-fork metrics:** the fork hook resets state and restarts background
   threads; the documented practice is to call `init()` after the fork. There
   is no `{worker}` substitution and no push-gateway mode in v0 (decision 3).
4. **gRPC `_h2` scope:** the minimal in-house HTTP/2 client in
   `trace/otlp/_h2.py` stays, for parity with Go (decision 4).
5. **Expected exceptions:** per scope only, through `expected=`; there is no
   process-wide setting (decision 5).
