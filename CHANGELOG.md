# Changelog

Notable changes to pyrock are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Because pyrock is
pre-1.0, minor releases may include intentional API or behavior changes; each
one is listed with a migration note.

## Unreleased

The initial implementation: a zero-dependency Python port of
[Bedrock](https://github.com/kzs0/bedrock) for CPython 3.11+, including the
free-threaded build.

### Added

- Core API: `pyrock.init()`, `pyrock.Bedrock`, and `operation`, `step` and
  `source` as sync/async context managers and as decorators for functions,
  coroutines, generators and async generators. Also `start_operation` for a
  manual lifecycle, `register`, `current`, `current_operation`,
  `current_span` and `current_span_context`.
- Operation metrics with the same names as Go (`<name>_count`, `_successes`,
  `_failures`, `_duration_ms`), labels declared up front with a `"_"` default,
  static labels, and series and family limits.
- Success by default: an escaping exception marks failure and is re-raised;
  `expected=` exceptions do not. Error attributes (`error`, `error.type`,
  `error.fingerprint`, `error.chain`, `error.group.count`, `error.types`) come
  from `attr.error`.
- `no_trace`, numbered duplicate span names, and preallocated noop objects
  when nothing is active.
- Context helpers for threads: `bind`, `ContextThreadPoolExecutor` and
  `pyrock.Thread` (with `context=`).
- Tracing: `Tracer`/`Span`/`SpanContext`, W3C Trace Context (`trace.w3c`), an
  HTTP propagator, a `Propagator` protocol with `CompositePropagator`, and the
  `AlwaysOn`, `AlwaysOff`, `Ratio` and `ParentBased` samplers.
- OTLP trace export over HTTP/JSON (`urllib`) and gRPC, through a minimal
  in-house HTTP/2 client (TLS with ALPN or h2c) and a hand-written protobuf
  encoder; a bounded batch processor with export statistics.
- Metrics: a thread-safe registry of counters, gauges and histograms with
  `.labels()`, histogram timers, runtime metrics (`python_*`, `process_*`), and
  validated Prometheus text exposition.
- Logging on stdlib `logging`: a JSON or text handler, `CorrelationFilter`,
  the `debug`/`info`/`warning`/`warn`/`error`/`log` helpers, canonical
  `operation.complete` records, and `log_install` modes (`root`, `pyrock`,
  `none`).
- HTTP: WSGI and ASGI 3 middleware (opt-in websocket operations), stdlib
  `http.server` instrumentation (`http.instrument_http_server`), and an
  instrumented `urllib` client (`http.get`/`post`/`request`, `build_opener`,
  `TracingHandler`) plus `inject_headers` for third-party clients.
- The observability server (`/metrics`, `/health`, `/ready`, optional
  `/debug/pprof/*`) with Go's security defaults and a connection cap.
- Profiles: CPU, WALL, HEAP, THREADS (`GOROUTINE`), MUTEX and BLOCK, encoded
  as OTLP Profiles (v0.2.0) and pprof, exported over HTTP
  (`ProfileExportConfig`) and served by the optional `profile.debug` handler,
  which works with `go tool pprof`. Instrumented `Lock`, `RLock`, `Condition`
  and `Semaphore` feed MUTEX and BLOCK.
- The cloud backend (`pyrock.cloud`, `CloudConfig`): gRPC traces with
  `x-bedrock-key`, retry with configurable `retry_backoff`, fan-out to a local
  `trace_url`, metrics push and profile push.
- `privacy.Policy` enforced on spans, events, logs, metric labels and profile
  labels.
- Environment detection (process, git, Kubernetes, container, AWS, GCP) under
  a 500 ms budget.
- `Config` with `BEDROCK_*` environment variables in Go's syntax, plus the
  Python-only `BEDROCK_LOG_INSTALL` and `BEDROCK_SERVER_MAX_CONNECTIONS`;
  `ConfigError` lists every problem. `env.parse` reads typed dataclasses.
- `pyrock.testing.capture()` and the `pyrock_capture` pytest fixture
  (registered by entry point; pyrock never imports pytest).
- Fork safety: an `os.register_at_fork` hook resets state and restarts
  background threads in the child.
- Runnable stdlib examples (`examples/http_server.py`, `asgi_app.py`,
  `worker.py`) and copy-in recipes for httpx, requests, grpcio, Flask, Django
  and FastAPI.
- `README.md`, `CONTRIBUTING.md` and `SECURITY.md`; CI on Python 3.11–3.15
  and 3.14t (`PYTHON_GIL=0`), a 3.14t stress job, macOS and Windows runs, the
  compat suite (official OTLP protos, a real grpcio collector) and a
  pure-wheel build check.

### Behavior decisions

These differ from Go Bedrock, or settle points the design left open:

- `Ratio` samples deterministically on the trace ID, and `ParentBased` drops
  spans whose parent is unsampled.
- JSON log levels use Go's names (`DEBUG`, `INFO`, `WARN`, `ERROR`);
  `CRITICAL` stays `CRITICAL`.
- Counters raise `ValueError` on negative or NaN increments. A different
  `help` on re-registration is not a conflict (the first registration wins,
  as in Go).
- Decorators always wrap and resolve the active instance at call time, so
  functions decorated before `init()` are instrumented.
- Numbering applies only to siblings under the same operation or step; root
  operations and direct children of a source are never numbered.
- There is no process-wide default instance and no monkey-patching of
  `threading`. Middleware captures the instance at construction and activates
  it per request. On interpreters that inherit context into new threads
  (free-threaded 3.14), threads see the active instance.
- `http.user_agent`, `http.target` and `http.url` are emitted only when the
  privacy policy allowlists them.
- `Config.log_level` takes level names only; the log helpers also take ints.
- `/debug/pprof` without a trailing slash is 404, and a CPU profile longer
  than 30 s is rejected with 400, as in Go.
- CPU samples carry no asyncio task label; the profile exporter collects the
  first profiles immediately on start.
- `Activation`, `MetricConflictError` and `MetricLimitError` can be imported
  from `pyrock` but are not part of `__all__`.
