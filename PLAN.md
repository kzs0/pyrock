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
