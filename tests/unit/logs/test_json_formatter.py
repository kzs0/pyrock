"""JSONFormatter: Go-compatible record shape, values, privacy and errors.

DESIGN §8 (JSON shape, level names, source) and §14 (privacy); Go ``log/handler.go``.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import re

import pytest

from pyrock import privacy
from pyrock.logs import ATTRS_KEY, JSONFormatter
from tests.unit.logs._helpers import (
    FIXED_TIME,
    SPAN_ID,
    TRACE_ID,
    as_json,
    detached_logger,
    exc_info_of,
    in_span,
    make_record,
)

# ------------------------------------------------------------------- shape


def test_minimal_record_keys_and_order() -> None:
    record = as_json(JSONFormatter(), make_record("hello"))
    assert list(record) == ["time", "level", "msg", "logger"]
    assert record == {"time": FIXED_TIME, "level": "INFO", "msg": "hello", "logger": "myapp.users"}


def test_documented_key_order() -> None:
    fmt = JSONFormatter(service="users-api", static_attrs={"env": "production"})
    rec = make_record(
        "processing request",
        extra={ATTRS_KEY: {"user_id": "123"}, "trace_id": TRACE_ID, "span_id": SPAN_ID},
    )
    record = as_json(fmt, rec)
    assert list(record) == [
        "time",
        "level",
        "msg",
        "service",
        "env",
        "user_id",
        "trace_id",
        "span_id",
        "logger",
    ]
    assert record["service"] == "users-api"
    assert record["env"] == "production"
    assert record["trace_id"] == TRACE_ID
    assert record["span_id"] == SPAN_ID


def test_one_line_per_record_with_escaped_newlines() -> None:
    line = JSONFormatter().format(make_record("multi\nline", extra={"note": "a\nb"}))
    assert "\n" not in line
    assert json.loads(line)["msg"] == "multi\nline"
    assert json.loads(line)["note"] == "a\nb"


def test_message_args_are_interpolated() -> None:
    assert as_json(JSONFormatter(), make_record("user %s has %d items", "u1", 3))["msg"] == (
        "user u1 has 3 items"
    )


def test_non_ascii_message_round_trips() -> None:
    assert as_json(JSONFormatter(), make_record("caf\u00e9 \u2603"))["msg"] == "caf\u00e9 \u2603"


# -------------------------------------------------------------------- time


def test_time_is_rfc3339_utc_with_milliseconds() -> None:
    assert as_json(JSONFormatter(), make_record())["time"] == FIXED_TIME


def test_time_of_a_live_record_is_close_to_now() -> None:
    record = as_json(JSONFormatter(), make_record(created=None))
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record["time"])
    parsed = datetime.datetime.fromisoformat(record["time"].replace("Z", "+00:00"))
    now = datetime.datetime.now(datetime.UTC)
    assert abs((now - parsed).total_seconds()) < 60


def test_whole_second_time_keeps_three_decimals() -> None:
    record = as_json(JSONFormatter(), make_record(created=1793880000.0))
    assert record["time"] == "2026-11-05T12:00:00.000Z"


# ------------------------------------------------------------------ levels


@pytest.mark.parametrize(
    ("level", "name"),
    [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARN"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
    ],
)
def test_level_names_follow_go(level: int, name: str) -> None:
    assert as_json(JSONFormatter(), make_record(level=level))["level"] == name


# ------------------------------------------------------------------ fields


def test_attrs_key_fields_become_top_level() -> None:
    record = as_json(
        JSONFormatter(), make_record(extra={ATTRS_KEY: {"user.id": "u1", "attempts": 3}})
    )
    assert record["user.id"] == "u1"
    assert record["attempts"] == 3
    assert ATTRS_KEY not in record


def test_extra_fields_become_top_level() -> None:
    record = as_json(JSONFormatter(), make_record(extra={"user.id": "u1", "ok": True}))
    assert record["user.id"] == "u1"
    assert record["ok"] is True


def test_standard_logrecord_attributes_are_not_emitted() -> None:
    record = as_json(JSONFormatter(), make_record("m %s", "x"))
    for key in (
        "args",
        "msg_args",
        "levelno",
        "levelname",
        "pathname",
        "filename",
        "module",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "process",
        "processName",
        "name",
        "message",
        "exc_info",
        "exc_text",
        "stack_info",
        "taskName",
    ):
        assert key not in record, key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("s", "s"),
        (3, 3),
        (1.5, 1.5),
        (True, True),
        (False, False),
        (["a", 1, True], ["a", 1, True]),
        (("t", 2), ["t", 2]),
        ({"nested": {"deep": [1, {"x": "y"}]}}, {"nested": {"deep": [1, {"x": "y"}]}}),
    ],
)
def test_value_types(value: object, expected: object) -> None:
    assert as_json(JSONFormatter(), make_record(extra={ATTRS_KEY: {"v": value}}))["v"] == expected


def test_timedelta_is_written_in_milliseconds() -> None:
    v = as_json(
        JSONFormatter(),
        make_record(extra={ATTRS_KEY: {"timeout": datetime.timedelta(seconds=5)}}),
    )["timeout"]
    assert v == "5000ms"


def test_datetime_is_rfc3339() -> None:
    when = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=datetime.UTC)
    v = as_json(JSONFormatter(), make_record(extra={ATTRS_KEY: {"at": when}}))["at"]
    assert isinstance(v, str)
    assert datetime.datetime.fromisoformat(v.replace("Z", "+00:00")) == when


def test_bytes_are_base64() -> None:
    v = as_json(JSONFormatter(), make_record(extra={ATTRS_KEY: {"raw": b"\x00\x01\xff"}}))["raw"]
    assert base64.b64decode(v) == b"\x00\x01\xff"


def test_non_finite_floats_still_produce_valid_json() -> None:
    line = JSONFormatter().format(
        make_record(extra={ATTRS_KEY: {"a": float("nan"), "b": float("inf")}})
    )
    json.loads(line)
    assert "\n" not in line


class _Leaky:
    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def __repr__(self) -> str:
        self.calls.append("repr")
        return "PASSWORD=hunter2"

    def __str__(self) -> str:
        self.calls.append("str")
        return "PASSWORD=hunter2"

    def __format__(self, spec: str) -> str:
        self.calls.append("format")
        return "PASSWORD=hunter2"


def test_unknown_objects_are_suppressed_not_stringified() -> None:
    leaky = _Leaky()
    line = JSONFormatter().format(
        make_record(
            extra={
                "creds": leaky,
                ATTRS_KEY: {"also": leaky, "nested": {"inner": leaky, "ok": 1}, "list": [leaky]},
            }
        )
    )
    record = json.loads(line)
    assert "hunter2" not in line
    assert leaky.calls == []
    assert "creds" not in record
    assert "also" not in record
    assert record["nested"]["ok"] == 1
    assert "inner" not in record["nested"]


def test_cycles_do_not_hang_or_crash() -> None:
    loop: dict[str, object] = {"a": 1}
    loop["self"] = loop
    record = as_json(JSONFormatter(), make_record(extra={ATTRS_KEY: {"loop": loop}}))
    assert record["loop"]["a"] == 1
    assert record["loop"]["self"] == privacy.CYCLE_MARKER


# ----------------------------------------------------------------- statics


def test_service_and_static_attrs_are_top_level() -> None:
    fmt = JSONFormatter(service="svc", static_attrs={"env": "prod", "region": "eu"})
    record = as_json(fmt, make_record())
    assert (record["service"], record["env"], record["region"]) == ("svc", "prod", "eu")


def test_no_service_key_without_service() -> None:
    assert "service" not in as_json(JSONFormatter(), make_record())


def test_record_fields_cannot_override_reserved_keys() -> None:
    record = as_json(
        JSONFormatter(service="svc"),
        make_record("real", extra={ATTRS_KEY: {"level": "FAKE", "time": "x", "service": "evil"}}),
    )
    assert record["level"] == "INFO"
    assert record["time"] == FIXED_TIME
    assert record["msg"] == "real"
    assert record["service"] == "svc"


# ----------------------------------------------------------------- privacy


def test_policy_allowlist_filters_fields() -> None:
    fmt = JSONFormatter(policy=privacy.Policy(attribute_keys=["user.*"]))
    record = as_json(fmt, make_record(extra={ATTRS_KEY: {"user.id": "u1", "secret": "s"}}))
    assert record["user.id"] == "u1"
    assert "secret" not in record
    assert (record["level"], record["msg"]) == ("INFO", "hello")


def test_policy_redact_hook_applies_to_fields() -> None:
    def redact(path: str, value: object) -> tuple[object, bool]:
        if path == "password":
            return "[redacted]", True
        if path == "drop.me":
            return value, False
        return value, True

    fmt = JSONFormatter(policy=privacy.Policy(redact=redact))
    record = as_json(
        fmt, make_record(extra={ATTRS_KEY: {"password": "p", "drop.me": 1, "kept": 2}})
    )
    assert record["password"] == "[redacted]"
    assert "drop.me" not in record
    assert record["kept"] == 2


def test_policy_truncates_long_strings() -> None:
    fmt = JSONFormatter(policy=privacy.Policy(max_string_length=8))
    v = as_json(fmt, make_record(extra={ATTRS_KEY: {"note": "x" * 100}}))["note"]
    assert len(v) < 100


# ------------------------------------------------------------------ errors


def test_exc_info_becomes_error_fields_without_details_by_default() -> None:
    line = JSONFormatter().format(
        make_record("boom", level=logging.ERROR, exc_info=exc_info_of(ValueError("secret-detail")))
    )
    record = json.loads(line)
    assert record["error"] is True
    assert record["error.type"] == "builtins.ValueError"
    assert re.fullmatch(r"[0-9a-f]{16}", record["error.fingerprint"])
    assert "error.message" not in record
    assert "secret-detail" not in line
    assert "Traceback" not in line
    assert "/srv/app" not in line


def test_exc_info_message_with_allow_error_details() -> None:
    fmt = JSONFormatter(policy=privacy.Policy(allow_error_details=True))
    record = as_json(fmt, make_record(exc_info=exc_info_of(ValueError("visible-detail"))))
    assert record["error.message"] == "visible-detail"
    assert record["error.type"] == "builtins.ValueError"


def test_exc_info_chain() -> None:
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        exc = outer
    record = as_json(JSONFormatter(), make_record(exc_info=(type(exc), exc, exc.__traceback__)))
    assert record["error.type"] == "builtins.RuntimeError"
    assert record["error.chain"] == ["builtins.RuntimeError", "builtins.KeyError"]


def test_exc_info_true_resolves_the_current_exception() -> None:
    from tests.unit.logs._helpers import Capture

    cap = Capture()
    handler = logging.StreamHandler(cap.stream)
    handler.setFormatter(JSONFormatter())
    log = detached_logger("t", handler)
    try:
        raise TimeoutError
    except TimeoutError:
        log.exception("failed")
    record = cap.one()
    assert record["level"] == "ERROR"
    assert record["error.type"] == "builtins.TimeoutError"


def test_error_namespace_in_fields_is_private_by_default() -> None:
    record = as_json(
        JSONFormatter(),
        make_record(extra={ATTRS_KEY: {"error.message": "leak", "error.type": "x.Y"}}),
    )
    assert "error.message" not in record


def test_stack_info_is_not_emitted_without_source_paths() -> None:
    rec = make_record()
    rec.stack_info = 'Stack (most recent call last):\n  File "/srv/app/x.py", line 1'
    line = JSONFormatter().format(rec)
    assert "/srv/app" not in line


# ------------------------------------------------------------------ source


def test_no_source_by_default() -> None:
    assert "source" not in as_json(JSONFormatter(), make_record())


def test_add_source_is_module_function_line() -> None:
    record = as_json(JSONFormatter(add_source=True), make_record())
    assert record["source"] == "users:load_user:42"


def test_add_source_with_allow_source_paths_uses_full_path() -> None:
    fmt = JSONFormatter(add_source=True, policy=privacy.Policy(allow_source_paths=True))
    source = as_json(fmt, make_record())["source"]
    assert source.startswith("/srv/app/myapp/users.py")
    assert source.endswith(":42")


def test_add_source_from_a_real_call_site() -> None:
    from tests.unit.logs._helpers import Capture

    cap = Capture()
    handler = logging.StreamHandler(cap.stream)
    handler.setFormatter(JSONFormatter(add_source=True))
    detached_logger("t", handler).info("here")
    source = cap.one()["source"]
    assert re.fullmatch(r"test_json_formatter:test_add_source_from_a_real_call_site:\d+", source)


# --------------------------------------------------------------- correlation


def test_formatter_uses_correlation_fields_set_on_the_record() -> None:
    record = as_json(JSONFormatter(), make_record(extra={"trace_id": TRACE_ID, "span_id": SPAN_ID}))
    assert (record["trace_id"], record["span_id"]) == (TRACE_ID, SPAN_ID)


def test_empty_correlation_fields_are_omitted() -> None:
    record = as_json(JSONFormatter(), make_record(extra={"trace_id": "", "span_id": ""}))
    assert "trace_id" not in record
    assert "span_id" not in record


def test_format_is_pure_with_respect_to_the_active_span() -> None:
    # Formatting the same record inside a span must not raise; correlation is the
    # handler's / filter's job, and a pre-correlated record keeps its IDs.
    rec = make_record(extra={"trace_id": TRACE_ID, "span_id": SPAN_ID})
    record = in_span(lambda: as_json(JSONFormatter(), rec))
    assert record["trace_id"] == TRACE_ID
