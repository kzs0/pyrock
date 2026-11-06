# pyrock delivery plan

Branch: `feat/pyrock-implementation`, created from `main`. The design it
implements is `DESIGN.md`.

The work goes in strict phases, and each phase starts only when the one before
it is green and committed. Within a phase, subagents run in parallel, but only
on **disjoint file sets**, so no two agents ever edit the same file. Agents do
not commit. The orchestrator reviews, runs the gates and commits once per phase
or wave.

## Decisions on DESIGN.md §21 open questions

1. **Name:** the distribution and import name are both `pyrock`.
2. **Thread context:** we do not monkey-patch. Context crosses threads only
   through the explicit helpers (`bind`, `ContextThreadPoolExecutor`, `Thread`).
3. **Pre-fork servers:** the fork hook resets state and restarts background
   threads. Recommended practice is to call `init()` after the fork; this is
   documented. There is no `{worker}` substitution in v0.
4. **gRPC:** keep the minimal in-house HTTP/2 client in `trace/otlp/_h2.py`,
   for parity with Go.
5. **Expected exceptions:** set per scope only (`expected=`).

## Supported Pythons

| Python | Status |
|---|---|
| 3.11, 3.12, 3.13, 3.14 | Required |
| 3.14t (free-threaded) | Required. Run with `PYTHON_GIL=0`. |
| 3.15 (pre-release) | Required in CI with `allow-prereleases` |

The rest of the matrix:

- **Linux:** runs the full matrix.
- **macOS and Windows:** run 3.14 only. Fork tests and Linux-only detectors are
  skipped there.

Language rules: code must stay valid on 3.11. Newer stdlib APIs (for example
`sys._is_gil_enabled`) are allowed only behind feature detection.

## Tooling

- `uv` with a PEP 735 `[dependency-groups]` table. The `hatchling` build
  backend. **No** `[project.dependencies]`.
- **dev group:** `pytest`, `coverage`, `ruff`, `mypy`.
- **compat group** (test-only): `protobuf` and `opentelemetry-proto`, used to
  decode our OTLP bytes with the official schema; `grpcio`, used as a real gRPC
  collector. These tests skip cleanly when the packages can't be installed, for
  example on 3.15 beta or 3.14t.
- `make ci` mirrors CI: `ruff format --check`, `ruff check`, `mypy --strict src`,
  `pytest` with ≥80% branch coverage, plus the boundary tests.

## Phases

### Phase 0: Scaffold and API contract (1 agent, sequential)

The deliverable is a package that imports cleanly on every supported Python and
fixes the **public contract**:

- **Project files:** `pyproject.toml`, `src/pyrock/**` (every module from
  DESIGN §3), `py.typed`, `Makefile`, and `.github/workflows/ci.yml`.
- **Public API:** every public class, function and signature from the design,
  fully typed and documented. Bodies `raise NotImplementedError`. The exception
  is pure data types, which are written for real: `Config` fields and
  validation stubs, `privacy.Policy`, `ProfileExportConfig`, `ProfileType`,
  `SpanKind`, `SpanContext`, and the error classes.
- **Shared test support in `tests/support/`:** this is the only shared test
  code, and later phases may not edit it:
  - an OTLP/HTTP fake collector (JSON);
  - a profiles collector;
  - a metrics-push sink;
  - a fake AWS IMDS and GCP metadata server;
  - a gRPC collector built on grpcio, which skips when grpcio is unavailable;
  - log capture helpers;
  - a minimal WSGI and ASGI driver, so tests need no frameworks.
- **Structural tests in `tests/structure/`:**
  - stdlib-only imports;
  - internal dependency boundaries (DESIGN §3);
  - `import pyrock` is lazy (heavy modules are not loaded);
  - the public names in `pyrock.__all__` match the contract.

Gate: the package imports on 3.11–3.15 and 3.14t, structural tests pass, and
`ruff` and `mypy --strict` are clean.

### Phase 1: End-to-end functional tests (3 parallel agents, `tests/e2e/`)

These are black-box tests. They use only the public API and observe only
external output: the bytes a collector receives, `/metrics` text, log lines,
HTTP headers, and process behaviour. They are expected to **fail** (red) until
Phase 3.

| Agent | Files | Covers |
|---|---|---|
| E1 core | `test_lifecycle.py`, `test_operations.py`, `test_steps_sources.py`, `test_context.py`, `test_logging.py`, `test_noop.py` | init/shutdown, operations (exception → failure, expected, fail/succeed, numbering, no_trace, decorators on sync/async/generators), steps, sources, contextvars across asyncio/threads/helpers, logging + canonical log, noop |
| E2 HTTP | `test_wsgi.py`, `test_asgi.py`, `test_client.py`, `test_propagation.py`, `test_server.py` | middleware attrs/labels/status/route/streaming/exceptions, client spans + header injection, two-service trace continuity, observability server endpoints and hardening (slow headers, header limit, connection cap) |
| E3 export | `test_trace_export.py`, `test_metrics.py`, `test_cloud.py`, `test_profiles.py`, `test_privacy.py`, `test_envdetect.py`, `test_fork.py`, `test_config_env.py` | OTLP HTTP and gRPC export, sampling, exposition, runtime metrics, limits, cloud wiring (gRPC + push + profiles, retry, fan-out), profile export, privacy policy, environment detection, fork safety, env-var config and `env.parse` |

Gate: the tests collect on every Python, fail only with `NotImplementedError`
or assertion failures (never import or syntax errors), and the orchestrator has
reviewed them for coverage of DESIGN.md.

### Phase 2: Unit and component tests (4 parallel agents, `tests/unit/<area>/`)

These are contract tests for each module. They test observable behaviour, not
private internals.

| Agent | Area |
|---|---|
| U1 | `attr`, `env`, `config`, `privacy` |
| U2 | `trace` (w3c, sampler, span/tracer, propagator), `trace/otlp` (`_pb`, `_h2`, http, grpc, batch) |
| U3 | `metric` (registry, types, limits), `prometheus`, `runtime`, `log` |
| U4 | `profile` (sampler, heap, stacks, locks, pprof and OTLP encoders, export, debug), `server` |

The core modules (`_operation`, `_bedrock`, `wsgi`, `asgi`, `http`, `_cloud`) are
covered mainly by E2E tests. U-agents add component tests for them only when
the logic can be isolated.

Gate: same as Phase 1.

### Phase 3: Implementation (waves follow the dependency order)

Each agent owns its `src/` files. To be done, an agent must:

- make its unit tests green;
- keep `ruff` and `mypy --strict` clean;
- not break any other test.

**Tests are the contract:** an agent may not edit tests. If it believes a test
is wrong, it reports this, and the orchestrator decides.

| Wave | Agents (parallel within a wave) |
|---|---|
| 3a leaves | I1 `attr`, `env`, `config`, `privacy` · I2 `trace` core + `trace/otlp` (`_pb`, http, batch) · I3 `metric` + `prometheus` + `runtime` · I4 `trace/otlp/_h2` + grpc exporter |
| 3b services | I5 `logs` · I6 `server` · I7 `profile/*` |
| 3c core | I8 `_context`, `_noop`, `_bedrock`, `_operation`, `__init__`, `testing`, `_envdetect`, fork hook |
| 3d integrations | I9 `wsgi`, `asgi`, `http` · I10 `_cloud`, `_push`, profile push wiring |

The orchestrator commits after each wave and pushes.

### Phase 4: Validation

1. Full gates on 3.11, 3.12, 3.13, 3.14, 3.14t (`PYTHON_GIL=0`) and 3.15b,
   run locally through `uv run --python`.
2. Concurrency-sensitive tests repeated (`--count`-style loops) on 3.14t.
3. Compat suite: decode with the official OTLP protos and run a real grpcio
   collector.
4. Review agent: coherence, idiom, API consistency against DESIGN.md, and dead
   code. The orchestrator triages its findings and agents apply the fixes.
5. Docs: `README.md`, `CHANGELOG.md` (Unreleased), `CONTRIBUTING.md`,
   `SECURITY.md`, and `examples/`.
6. Push and confirm CI is green.

## Rules for every agent

- Only standard library code in `src/`, which must stay valid on Python 3.11.
  Use `from __future__ import annotations`, PEP 604 unions, `typing.Self`, and
  `dataclass(slots=True)`. Do not use 3.12+ syntax such as `type X = ...` or
  PEP 695 generics.
- Every module must be safe without the GIL: shared mutable state is guarded by
  `threading.Lock`.
- Wait with events, barriers and test servers, not `time.sleep`. Every network
  test binds to `127.0.0.1:0`.
- Do not touch files outside the agent's assignment. Report blockers instead.

## Contract decisions log

These were decided during the phases and are binding for every later phase.

### From E1 (core e2e)

1. **Decorators always wrap.** `@operation`, `@step` and `@source` return a
   `functools.wraps` wrapper and look up the active instance **at call time**,
   so functions decorated at import time, before `init()`, are instrumented.
   The noop path costs one `ContextVar.get()` plus a direct call. Returning the
   function unchanged applies only when an explicit noop `bedrock=` is given.
   (This supersedes DESIGN §4.8, which said noop decorators return the function
   unchanged.)
2. **Exiting `with pyrock.init()`** shuts down and restores the previous
   context value, which deactivates the instance.
3. **`Bedrock(...)` is fully live but not activated.** The constructor starts
   exporters, the server and log install according to config. It is not
   activated and must be shut down.
4. **Duplicate-name numbering** applies only among children of the same parent
   operation or step. Root operations and direct children of a source are
   never numbered, so span names stay bounded in loops.
5. **Log level names** in JSON follow Go: `DEBUG`, `INFO`, `WARN`, `ERROR`.
   Python's `CRITICAL` stays `CRITICAL`.
6. **A failed canonical log** includes `error`, `error.type` and
   `error.fingerprint`. `error.message` is added only with
   `allow_error_details`.
7. **Error attribute types:**
   - `error.chain` and `error.types` are `list[str]`.
   - `error.fingerprint` is 16 lowercase hex characters.
   - `error.group.count` is `int`.
8. **Static attrs** are *resource* attributes on exported spans (as in Go),
   top-level fields in logs, and labels on metrics.
9. **`pyrock.testing` filters** (`CapturedMetrics.value` and
   `CapturedLogs.find`) accept an optional positional mapping, for dotted
   keys, in addition to `**kwargs`.
10. **`pyrock.Thread`** accepts `context=`, with the same meaning as on 3.14's
    `threading.Thread`.

### From E2 (HTTP e2e)

11. **`http.user_agent`** is sensitive and emitted only when the policy's
    `attribute_keys` allowlist matches it (`"http.user_agent"` or `"http.*"`).
12. **`raise_for_status=True`** raises `urllib.error.HTTPError`.
13. **Raw outgoing paths and URLs** (`http.target`, `http.url`) are emitted
    only when allowlisted in `attribute_keys`. Credentials, query and fragment
    are always stripped. Userinfo is never turned into auth.
14. **ASGI `websocket=True`** gives one SERVER operation named `http.websocket`
    (overridable), with `http.route` and `http.scheme` (`ws`/`wss`). It ends
    when the connection closes.
15. **Connection cap:** a rejected connection gets `503` and is then closed.
    `pyrock.Config.server_max_connections` (env
    `BEDROCK_SERVER_MAX_CONNECTIONS`, default 64) maps to the server config.
16. **The profile handler** receives `/debug/pprof/*` with `PATH_INFO`
    unchanged.
17. **An exception after headers are sent** marks failure. `http.status_code`
    stays the status already sent, and the exception is re-raised.
18. **Middleware and threaded servers.** There is **no** process-wide default
    instance, so a plain `threading.Thread` still sees the noop.
    `wsgi.Middleware`, `asgi.Middleware` and `http.instrument_http_server`
    capture the instance when they are constructed: either `bedrock=` or
    `pyrock.current()` at that point. If that is the noop, they resolve
    `current()` per request instead. Each request is handled inside a context
    with that instance active, so handler code on server worker threads is
    instrumented.
19. **A `route=` callable returning `None`** falls back to the framework
    route key, then to `"_"`.
20. **Metric family names** turn dots into underscores (`http.request` →
    `http_request_count`).
21. **`testing.capture`:**
    - spans are complete after the block exits;
    - `metrics.text()` works inside the block;
    - `spans.find(kind=...)` filters by kind.

### From E3 (export e2e)

22. **The detection env var is `BEDROCK_DISABLE_ENVIRONMENT_DETECTION`**, the
    Go name.
23. **Cloud trace retry:** one initial attempt plus up to 3 retries (4 calls in
    all), with backoff `(1, 2, 4)` seconds, as in Go.
24. **`CloudConfig.retry_backoff`**, a `tuple[float, ...]` defaulting to
    `(1.0, 2.0, 4.0)`, is public and validated (non-negative, at most 10
    entries). Its length is the number of retries.
25. **`CloudConfig.traces_endpoint`**, a `str | None` defaulting to `None`,
    optionally overrides the gRPC trace target. Without it, traces derive
    their target from `endpoint`.
26. **Samplers follow DESIGN, not Go:**
    - `ParentBased` drops spans whose parent is unsampled.
    - `Ratio` is deterministic on the trace ID.
    - The differences from Go are documented in the README.
27. **The debug profile's `seconds`** above 30 is rejected with `400`, as in
    Go. The DESIGN text saying "capped" is changed to match.
28. **Exposition numbers** follow Go's `%g` formatting.
29. **The OTLP scope** is name `pyrock` plus the package version.
30. **HEAP stops tracemalloc** on export stop only if pyrock started it.
    Profile uploads carry `x-profile-type`.
31. **Nested `capture()` / `with init()` blocks** restore the outer active
    instance when they exit. This is the same rule as decision 2.
32. **`pyrock.cloud(...)` takes every `CloudConfig` field** as a keyword,
    including `retry_backoff` and `traces_endpoint`.
    - `traces_endpoint` accepts either a bare `host:port` or a URL.
    - With a URL, the scheme is stripped and the default port added, the same
      as Go's endpoint normalisation.

### From U3 (metric/log unit tests)

33. **Metric registry and log formatting rules**, as U3's tests pin them down:
    - **Re-registration:** a different `help` text is *not* a conflict. The
      first registration wins, as in Go; the docstring will be fixed in
      Phase 3.
    - **Counter input:** NaN and negative increments raise `ValueError`,
      following Python convention (Go ignores them).
    - **Exposition order:** series are sorted by label values, and labels by
      name with `le` last.
    - **JSON log keys** come in this order: `time, level, msg, service`, then
      static attrs, record attrs, `trace_id`, `span_id`, `logger`.
      Empty IDs are omitted.
    - **Runtime metric families** take the registry prefix.

### From U1 (attr/env/config/privacy unit tests)

34. **U1's assumptions are accepted as the contract.** The tests in
    `tests/unit/{attr,env,config,privacy}` define it. The rules:
    - **Error keys:** flat `error`, `error.type` and `error.fingerprint`
      survive the default policy. `error.message`, `exception.*` and nested
      error groups are private.
    - **`error.stack`** needs both `allow_error_details` and
      `allow_source_paths`.
    - **Exception groups:** `error.group.count` and `error.types` count leaf
      exceptions.
    - **Durations:** `parse_duration` rejects nan, inf and exponents.
      Negative values are rejected by `Config` validation.
    - **Allowlists:** a group entry admits the nested values under it. A leaf
      entry filters its parent group down to that child.
    - **`resource_keys`** adds to the defaults. Resource keys that aren't
      infrastructure pass by default.
    - **The walker** is iterative, so depth 5000 must not raise
      `RecursionError`. Cycle and alias tracking is per call.
    - **`sanitize_url`** keeps the path and port and never raises.

### From U2 (trace/otlp unit tests)

35. **U2's choices are accepted as the contract**, as written in
    `tests/unit/trace*`. Go parity applies wherever the docs are silent:
    - **tracestate:** duplicate keys make `parse_tracestate` raise.
    - **Empty batches:** exporters send nothing for an empty batch, and nothing
      after shutdown.
    - **Batch queue:** when full, the newest span is dropped. `failed` counts
      export calls, not spans.
    - **Retry classes:**
      - HTTP 4xx other than 429 is a permanent failure.
      - gRPC status 3, 7 and 16 are permanent.
      - `H2Error` wraps connect failures and timeouts.
    - **Span lifecycle:** spans ignore mutation after `end()`. Unsampled spans
      never reach the processor.

### From U4 (profile/server unit tests)

36. **U4's tested behaviours are accepted** as written in
    `tests/unit/{profile,server}`. Open points, decided:
    - **Heap frames:** frames are identified by (filename, line). The function
      name is resolved from code objects when possible, otherwise `file:line`.
    - **`ProfileExportConfig` limits:** following Go, `retries` must be 0–3
      and `max_bytes` at most 32 MiB. Validation is added in Phase 3 together
      with unit tests.
    - **Compat profile tests:** these only check fields shared between
      v0.2.0 and the newer schema, so `opentelemetry-proto` is not pinned.

## Phase 3 wave adjustment

Nearly every module depends on `attr` and `privacy`, so wave 3a is split in
two:

- **3a-0:** I1 (`attr`, `env`, `config`, `privacy`) runs alongside I4
  (`trace/otlp/_h2.py`), which depends on nothing.
- **3a-1:** I2 (trace core plus `_pb`, http, batch **and the grpc exporter**)
  runs alongside I3 (`metric`, `prometheus`, `runtime`).

Waves 3b–3d are unchanged.

## Interface notes (between implementation agents)

### `trace/otlp/_h2` (from I4)

- **Construction:** `H2Connection(host, port, *, ssl_context=None,
  server_hostname=None, connect_timeout=10.0)` connects **immediately**, so
  exporters must create it lazily on first use.
- **Requests:** `conn.request(method, path, headers, body, timeout=)` returns
  `H2Response`, which has `.status`, `.header(name)` and `.body`. The client
  builds the pseudo-headers itself. A non-200 status is returned in the
  response, not raised.
- **Errors:**
  - Every failure is an `H2Error`.
  - `StreamReset` means only that stream failed; the connection is still
    usable.
  - `ProtocolError` means the connection was torn down.
  - After any `H2Error` other than `StreamReset`, check `conn.closed` and
    reconnect if it is true. A timeout leaves the connection open, so it is
    reused.
- **Concurrency:** calls are serialised by a lock (see the design note in the
  module).
- **TLS:** a passed `ssl_context` gets ALPN `["h2"]` set on it.

### `attr` / `privacy` / `env` (from I1)

- **`attr._normalize_value(v)`** returns an `AttrValue`, or `None` meaning
  drop and count. Exceptions are expanded with
  `attr.error(exc, allow_details=, allow_source_paths=, stack=)`.
- **`attr._text_value(v)`** returns label or log text. Sequences, exceptions
  and unknown values give `"_"`.
- **`Policy.apply(attrs, path="")`** returns a new dict in input order.
  - Sensitive values become `"[redacted]"`; cycles and aliases become
    `"[cycle]"` / `"[alias]"`.
  - Tuples come back as lists.
  - Values may still be `timedelta`, `datetime`, `bytes` or `None`, so
    encoders still call `_normalize_value`.
- **Single values:** `Policy.value(path, v)` returns `(v, keep)`.
  `Policy.label_value(key, text)` returns `"_"` when the value is dropped.
  `Policy.message(text)` and `Policy.profile_labels(labels)` round out the
  helpers.
- **`Policy.resource(attrs)`:** only `host.`, `process.`, `k8s.` and
  `container.` keys need `resource_keys`. `attribute_keys` is not applied to
  resources.
- **`env.parse`** builds a nested type through its `from_env(environ)` method
  when it has one. This is how a nested `pyrock.Config` is parsed.

### `metric` (from I3)

- **Building the registry:**
  `Registry(prefix, buckets=, const_labels=<static attrs after the policy>,
  series_limit=, family_limit=, label_filter=policy.label_value)`.
  `buckets=None` means the defaults.
- **Operation metrics:** register `reg.counter(f"{name}_count", help,
  labels=...)`, plus `_successes`, `_failures`, and
  `reg.histogram(f"{name}_duration_ms", ...)`. Registration is cached for
  the same schema.
  - A declared label that collides with a constant (static) label raises.
    The core must drop static-attr keys from `metric_labels` before
    registering.
- **Recording:** `metric.labels({k: v ...}).inc()` and `.observe(ms)`.
  Missing keys become `"_"`; undeclared keys raise `ValueError`.
- **Exposition:** `prometheus.encode(reg)` returns bytes, served with
  `prometheus.CONTENT_TYPE`. On `ExpositionError`, respond 500 with an empty
  body.
- **Fork:** `reg.reset()` is safe in `after_in_child`.
- **Runtime metrics:** `RuntimeCollector(reg)` registers itself.
  `.track_loop(loop)` adds a loop, and `.close()` unregisters the collector.

### `trace` / `trace.otlp` (from I2)

- **Setup:**
  - `BatchProcessor(exporter, max_queue_size=, max_batch_size=,
    schedule_delay=, export_timeout=)`.
  - `Tracer(service=, resource=static_attrs, sampler=, processor=,
    policy=)`. A tracer has one processor slot, replaceable with
    `tracer.set_processor(p)`. Fan-out happens at the exporter level.
  - Exporters take `tracer.resource`, which is already filtered by the
    policy:
    - `HTTPExporter(url, service=, resource=, headers=, timeout=,
      ssl_context=)`;
    - `GRPCExporter(endpoint, service=, resource=, headers=, insecure=,
      timeout=, ssl_context=)`. Import its module lazily.
- **Spans:**
  - Start with `tracer.start_span(name, parent=Span|SpanContext|None,
    kind=, attrs=)`. A remote parent is passed as a `SpanContext`.
  - Activate with `token = use_span(span)`, deactivate with
    `token.var.reset(token)`. Read back with `current_span()` and
    `current_span_context()`.
  - Data: `span.set_attrs(mapping)`, `span.add_event(name, attrs)`,
    `span.record_exception(exc, attrs)`, `span.set_status(StatusCode.ERROR,
    msg)`.
  - Exception-valued attributes must be expanded with `attr.error()` before
    setting them; the policy drops raw exceptions.
  - `span.end()` exports only sampled spans.
- **Lifecycle:** `bp.stats()` returns `BatchStats`.
  `tracer.force_flush(timeout=)` returns a bool, and
  `tracer.shutdown(timeout=)` also shuts down the processor and exporter.
- **Fork:** call `bp._after_fork_in_child()` from `after_in_child`. Wrapping
  exporters (retry and fan-out in `_cloud`) must forward
  `_after_fork_in_child` to the exporters they wrap.

### `server` (from I6)

- **Construction:** `server.Server(registry, server.Config(addr=,
  metrics=, pprof=, profile_handler=, read_timeout=,
  read_header_timeout=, write_timeout=, idle_timeout=,
  max_header_bytes=, shutdown_timeout=, max_connections=))`.
- **Lifecycle:**
  - `.start()` runs on daemon threads and raises `OSError` if the port is
    busy.
  - `.address` is `(host, port)` once started.
  - `.shutdown(timeout=)` is idempotent.

**Amendment to decision 18.** "A plain `threading.Thread` sees the noop"
holds only when the interpreter does not inherit context into new threads.
When `sys.flags.thread_inherit_context` is set (the default on free-threaded
3.14), new threads inherit the caller's context, and with it the active
instance. That is Python's own behaviour, and pyrock does not fight it.
Tests that need an empty context run their thread target through
`contextvars.Context().run`.

### `logs` (from I5)

- **Install:**
  `restore = logs._install(target, level=, log_format=, stream=, service=,
  static_attrs=, policy=, add_source=)`.
  - `target` is `"root"`, `"pyrock"` or `"none"`.
  - `restore()` is idempotent and safe in any order.
- **Helpers:**
  `logging.getLogger(logs.LOGGER_NAME).log(level, msg,
  extra={logs.ATTRS_KEY: attrs}, exc_info=exc, stacklevel=N)`.
- **Canonical log:** `logs.log_canonical(logger, operation=, duration_ms=,
  success=, attributes=, span_context=)`. Failure fields must already be
  expanded with `attr.error` inside `attributes`.

### `profile` (from I7)

- **Starting an exporter:** `profile.start_export(cfg, *, service, resource=None,
  policy=None, loops=None)` returns a `ProfileExporter`.
  - The exporter has `.stats()`, `.stop(timeout=)` and `.running`.
  - `ProfileExportStats` supports `+`.
- **Debug handler:** `profile.debug.app(*, max_seconds=30.0,
  prefix="/debug/pprof", loops=None)`.
- **Fork:** the fork hook calls `profile._locks.reset()` in the child.
- **Deviations from DESIGN**, to be recorded in the Phase 4 docs:
  - CPU samples carry no asyncio task label.
  - The first profile collection runs immediately on start.
