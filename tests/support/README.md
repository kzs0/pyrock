# Shared test support

`tests/support/` is the only shared test code. Later phases use it but may
not edit it. Nothing here imports pyrock: the fakes observe only what reaches
the network, a stream or a WSGI/ASGI caller. `tests/support/test_support.py`
proves the fakes work.

Import from the repository root package, for example
`from tests.support.prom import parse`. New test directories need an
`__init__.py`, because `tests/` is a package and pytest uses the default
`prepend` import mode.

## Fixtures (`tests/conftest.py`)

| Fixture | Yields | Notes |
|---|---|---|
| `otlp_collector` | `OTLPHTTPCollector` | OTLP/HTTP fake on `127.0.0.1:0`. Use `traces_url` / `profiles_url` / `metrics_url`. |
| `push_sink` | `MetricsPushSink` | Same server. `endpoint` is the base URL to give a cloud config; pushes arrive at `/v1/metrics`. |
| `metadata_server` | `FakeMetadataServer` | AWS-flavoured. Build `FakeMetadataServer(provider="gcp"/"both"/"none")` yourself for the others. |
| `grpc_collector` | `GRPCCollector` | A real grpcio `TraceService` over h2c. **Skips** when `grpcio` / `opentelemetry-proto` are missing (compat group). Mark these tests `@pytest.mark.compat`. |
| `log_capture` | `LogCapture` | Pass `log_output=log_capture.stream` to pyrock. |
| `clean_env` | `MonkeyPatch` | Removes every `BEDROCK_*` / `PYROCK_*` variable and sets `PYROCK_AWS_METADATA_ENDPOINT=""` and `PYROCK_GCP_METADATA_ENDPOINT=""`, so cloud metadata probes are disabled. Returns the monkeypatch so you can call `setenv` with more. |
| *(autouse)* `_no_thread_leaks` | – | Fails a test whose **non-daemon** threads are still alive 2 s after it and its fixtures finish. Shut down executors and use `with` blocks. |

Markers are applied by directory: `tests/e2e/**` gets `e2e`, and
`tests/unit/**`, `tests/structure/**` and `tests/support/**` get `unit`. Other
markers are set explicitly: `compat`, `slow`, `linux_only`, `fork`. Unknown
markers are rejected (`--strict-markers`), and `xfail` is strict.

## `collectors.py`

Every HTTP fake extends `FakeHTTPServer`, a threaded `http.server` on a daemon
thread. All of them are context managers, and fixtures close them for you.

- **Recording.** `requests` returns every `RecordedRequest`, which has
  `method`, `path`, `query`, `headers` (lower-cased keys, repeated headers
  joined with `", "`), `raw_headers`, `body`, `json()`, `text` and
  `header(name)`. `requests_to(path)` filters by path. To wait without sleeping,
  use `wait_for_requests(n, path=None, timeout=5)`, which raises
  `AssertionError` on timeout.
- **Failure injection.**
  - `fail_next(n, status=503, body=b"", headers=(), delay=0.0, path=None)`
    queues the next responses in order. A queued response with `path` set is
    used only by requests to that path.
  - `set_default_status(code)` sets the steady-state status.
  - `hang()` and `release()` make requests block after they are recorded,
    which lets you test client timeouts. `close()` always releases.
- **`OTLPHTTPCollector`**
  - `spans()` returns flattened span dicts (see `otlp_decode.py`). It decodes
    JSON, or protobuf when the content type isn't `application/json` (that
    path needs the compat group).
  - `span(name)` returns the one span with that name.
  - `span_names()` lists the names.
  - `wait_for_spans(n, timeout=5)` waits on a `Condition`.
  - `trace_requests()` and `profile_requests()` return the raw requests, and
    `wait_for_profiles(n)` waits for profile uploads. Profile bodies are raw
    protobuf: decode them with `otlp_decode.decode_profiles_request_pb`.
  - `metric_pushes()` returns the metrics pushes.
- **`MetricsPushSink`** adds `pushes()`, `texts()` and `wait_for_pushes(n)`.
- **`FakeMetadataServer`**
  - **AWS (IMDSv2):** `PUT /latest/api/token` needs the
    `X-aws-ec2-metadata-token-ttl-seconds` header. Then
    `GET /latest/meta-data/{instance-id,placement/region}` with
    `X-aws-ec2-metadata-token` returns the values. Without the token it returns
    401, unless you pass `require_token=False`.
  - **GCP:** `GET /computeMetadata/v1/instance/{id,zone}` needs
    `Metadata-Flavor: Google`, and returns 403 without it. The zone is
    `projects/123456/zones/us-central1-a`, so the region is `us-central1`.
  - **Configuration:** set `instance_id`, `region`, `gcp_instance_id`,
    `gcp_zone` and `token` when you build the server. `environ()` returns
    `PYROCK_{AWS,GCP}_METADATA_ENDPOINT` values that point pyrock at this
    server, with an empty value for the provider you didn't enable. Call
    `hang()` to test the 500 ms budget.
- **`GRPCCollector`**
  - `endpoint` is a bare `127.0.0.1:port`, which pyrock treats as gRPC.
  - `requests` returns the `ExportTraceServiceRequest` messages.
  - `metadata` returns a dict of invocation metadata for each call.
  - `spans()` and `wait_for_spans(n)` work as on the HTTP collector, and
    `wait_for_calls(n)` waits for calls.
  - `fail_next(n, code="UNAVAILABLE")` makes the next calls fail with that
    status.
- **`wait_until(predicate, timeout=5)`** is a last-resort poll for state that
  has no event to wait on.

## `otlp_decode.py`

- `decode_any_value` / `decode_attributes` turn OTLP JSON `AnyValue`s into
  Python values. `intValue` becomes an `int`, `bytesValue` (base64) becomes
  `bytes`, arrays become lists and kvlists become dicts.
- `flatten_spans(request, id_encoding="hex")` returns one dict per span with
  these keys:
  - IDs and naming: `name`, `trace_id`, `span_id`, `parent_span_id` (`None`
    for a root span), `trace_state`, `flags` and `kind`, an int
    (1 = internal, 2 = server, 3 = client, 4 = producer, 5 = consumer).
  - Timing: `start_time_unix_nano` and `end_time_unix_nano`, both ints.
  - Content: `attributes` (a dict), `events` (each `{name, time_unix_nano,
    attributes}`) and `links`.
  - Status: `status_code` (0 = unset, 1 = ok, 2 = error) and `status_message`.
  - Context: `dropped_attributes_count`, `resource` (a dict), `scope_name` and
    `scope_version`.
- `decode_trace_request_pb(bytes)` returns the same dicts from protobuf, and
  `decode_profiles_request_pb(bytes)` returns the
  `ExportProfilesServiceRequest` message. Both use the official schema and
  **skip** the test without the compat group. `proto_available()` and
  `require_proto()` are also available.

## `prom.py`

`parse(text_or_bytes)` returns a `PromText` with these methods:

- `value(name, {"dotted.label": v}, **labels)` returns the value of the single
  sample whose labels include the ones given. It returns `None` if there is no
  match and raises `AssertionError` if more than one sample matches.
- `find(...)` returns every matching sample.
- `type(family)` and `help(family)` return the family's TYPE and HELP.
- `label_sets(name)` returns the label sets for a name.
- `names()` returns all sample names, and `has(name)` checks for one.

It handles label escapes, `+Inf`/`-Inf`/`NaN` and timestamps, and raises
`ValueError` on a malformed line.

## `logs.py`

`LogCapture` gives you a thread-safe `.stream` to pass to pyrock and these
helpers for reading it back:

- `records(strict=True)` parses one JSON object per line. A line that isn't
  JSON raises `AssertionError`, unless you pass `strict=False`.
- `find(msg=..., **kv)` returns the first record whose fields equal the ones
  given. Pass a dict for dotted keys: `find({"user.id": "1"})`.
- `find_all(...)` returns every matching record.
- `messages()` returns the message of each record.
- `clear()` empties the stream.

`parse_json_lines(text)` does the same parsing for any string.

## `wsgi_driver.py`

`call_wsgi(app, method, path, headers=..., body=..., environ=..., consume=True,
**make_environ_kwargs)` returns a `WSGIResponse` with these members:

- `status`, from the last `start_response` call, and `status_code`.
- `headers` and `header(name)`.
- `chunks`: the `write()` calls first, then the iterable's items. `body` joins
  them.
- `start_response_calls` and `closed`.

With `consume=False`, pull the body yourself with `next_chunk()` and
`consume()`, then call `close()`, which calls the iterable's `close()` once,
as a server does. Exceptions from the app or from iteration propagate, and
`close()` still runs.

`make_environ` builds a PEP 3333 environ. A query string in `path` is split
off, headers become `HTTP_*` keys, and repeated headers are joined with `,`.

## `asgi_driver.py`

- `await call_asgi(app, method, path, headers=..., body=b"" | [chunks],
  scope=..., on_message=...)` returns an `ASGIResponse`:
  - `status` is the final status. `informational` lists the 1xx statuses sent
    before it.
  - `headers`, `chunks`, `body` and `messages` hold what the app sent.
    `complete` is true once the app sends `more_body=False`.
  - After the request body, `receive()` waits until the response is complete
    and then returns `http.disconnect`.
  - If the app raises, the exception propagates with `.asgi_response`
    attached.
- `run_asgi(...)` is the synchronous wrapper, using `asyncio.run`.
- `call_lifespan(app)` sends startup and then shutdown, and records the
  replies.
- `call_websocket(app, path, incoming=[...])` sends `connect`, delivers the
  frames once the app accepts, then sends `disconnect`. It records `accept`,
  `send` and `close`.
