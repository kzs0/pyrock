"""Logging: installation modes, JSON shape, stdlib correlation, helpers, canonical logs.

DESIGN.md §8; Go: ``bedrock.Info/Warn/...``, the slog JSON shape and ``operation.complete``.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import re
from typing import Any

import pytest

import pyrock
from pyrock import privacy
from tests.e2e._core_helpers import isolated, quiet
from tests.support.logs import LogCapture

WARN = "WARN"
"""JSON level names follow Go (PLAN.md decision E1-5): ``WARNING`` is written as ``WARN``."""


def _init(log_capture: LogCapture, **overrides: Any) -> pyrock.Bedrock:
    settings = quiet(service="log-svc", log_install="root", log_output=log_capture.stream)
    settings.update(overrides)
    return pyrock.init(**settings)


def _raise(exc: BaseException) -> None:
    raise exc


# -- JSON shape -----------------------------------------------------------------


@isolated
def test_helper_record_has_go_compatible_json_shape(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with (
        _init(log_capture, static_attrs={"env": "production"}),
        pyrock.operation("handle") as op,
    ):
        pyrock.info("processing request", user_id="123")
    record = log_capture.find(msg="processing request")
    assert record["level"] == "INFO"
    assert record["service"] == "log-svc"
    assert record["env"] == "production"
    assert record["user_id"] == "123"
    assert record["trace_id"] == op.trace_id
    assert record["span_id"] == op.span_id
    assert record["logger"] == "pyrock"
    assert record["time"].endswith("Z")
    datetime.datetime.fromisoformat(record["time"].replace("Z", "+00:00"))


@isolated
def test_every_line_is_one_json_object(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture):
        pyrock.info("one")
        pyrock.info("two", note="has\nnewline")
    assert [record["msg"] for record in log_capture.records()] == ["one", "two"]
    assert log_capture.find(msg="two")["note"] == "has\nnewline"


@isolated
def test_record_outside_an_operation_has_no_trace_ids(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture):
        pyrock.info("no trace here")
    record = log_capture.find(msg="no trace here")
    assert not record.get("trace_id")
    assert not record.get("span_id")


@isolated
def test_step_logs_carry_the_step_span_id(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with (
        _init(log_capture),
        pyrock.operation("handle") as op,
        pyrock.step("helper") as step,
    ):
        pyrock.info("in step")
    record = log_capture.find(msg="in step")
    assert record["trace_id"] == op.trace_id
    assert record["span_id"] == step.span_id


# -- stdlib logging -------------------------------------------------------------


@isolated
def test_stdlib_loggers_are_structured_and_correlated_with_root_install(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    log = logging.getLogger("myapp.users")
    with _init(log_capture), pyrock.operation("load_user") as op:
        log.warning("slow upstream")
    record = log_capture.find(msg="slow upstream")
    assert record["logger"] == "myapp.users"
    assert record["level"] == WARN
    assert record["service"] == "log-svc"
    assert record["trace_id"] == op.trace_id
    assert record["span_id"] == op.span_id


@isolated
def test_stdlib_extra_fields_become_structured_fields(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture):
        logging.getLogger("myapp").info("user loaded", extra={"user.id": "u1", "attempts": 3})
    record = log_capture.find(msg="user loaded")
    assert record["user.id"] == "u1"
    assert record["attempts"] == 3


@isolated
def test_unknown_extra_objects_are_suppressed_not_stringified(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    class Credentials:
        def __repr__(self) -> str:
            return "PASSWORD=hunter2"

        __str__ = __repr__

    with _init(log_capture):
        logging.getLogger("myapp").info("connecting", extra={"creds": Credentials()})
    assert log_capture.find(msg="connecting")
    assert "hunter2" not in log_capture.text


@isolated
def test_log_exception_produces_error_fields_without_message_by_default(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture):
        try:
            _raise(ValueError("secret-detail"))
        except ValueError:
            logging.getLogger("myapp").exception("boom")
    record = log_capture.find(msg="boom")
    assert record["level"] == "ERROR"
    assert record["error.type"] == "builtins.ValueError"
    assert "error.message" not in record
    assert "secret-detail" not in log_capture.text


@isolated
def test_log_exception_includes_message_when_policy_allows(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    policy = privacy.Policy(allow_error_details=True)
    with _init(log_capture, data_policy=policy):
        try:
            _raise(ValueError("visible-detail"))
        except ValueError:
            logging.getLogger("myapp").exception("boom")
    assert log_capture.find(msg="boom")["error.message"] == "visible-detail"


# -- helpers ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("helper", "level"),
    [
        ("debug", "DEBUG"),
        ("info", "INFO"),
        ("warning", WARN),
        ("warn", WARN),
        ("error", "ERROR"),
    ],
)
@isolated
def test_level_helpers(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture, helper: str, level: str
) -> None:
    with _init(log_capture, log_level="debug"):
        getattr(pyrock, helper)(f"via {helper}", key="value")
    record = log_capture.find(msg=f"via {helper}")
    assert record["level"] == level
    assert record["key"] == "value"
    assert record["logger"] == "pyrock"


@pytest.mark.parametrize("level", [logging.INFO, "info", "INFO"])
@isolated
def test_log_helper_with_explicit_level_and_mapping(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture, level: int | str
) -> None:
    with _init(log_capture):
        pyrock.log(level, "custom", {"http.route": "/users"}, n=1)
    record = log_capture.find(msg="custom")
    assert record["level"] == "INFO"
    assert record["http.route"] == "/users"
    assert record["n"] == 1


@isolated
def test_error_helper_with_positional_exception(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture):
        pyrock.error("job failed", KeyError("job-42"), job="sync")
    record = log_capture.find(msg="job failed")
    assert record["level"] == "ERROR"
    assert record["error.type"] == "builtins.KeyError"
    assert record["job"] == "sync"
    assert "job-42" not in log_capture.text


@isolated
def test_explicit_instance_log_methods(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    bedrock = pyrock.Bedrock(
        **quiet(service="lib", log_install="pyrock", log_output=log_capture.stream)
    )
    try:
        bedrock.info("from instance", k="v")
        assert bedrock.logger.name == "pyrock"
    finally:
        bedrock.shutdown()
    record = log_capture.find(msg="from instance")
    assert record["service"] == "lib"
    assert record["k"] == "v"


# -- levels and formats ---------------------------------------------------------


@isolated
def test_log_level_filters_helpers_and_stdlib_loggers(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture, log_level="warning"):
        pyrock.info("dropped helper")
        pyrock.debug("dropped debug")
        logging.getLogger("myapp").info("dropped stdlib")
        pyrock.warning("kept helper")
        logging.getLogger("myapp").error("kept stdlib")
    assert log_capture.messages() == ["kept helper", "kept stdlib"]


@isolated
def test_log_level_from_environment(clean_env: pytest.MonkeyPatch, log_capture: LogCapture) -> None:
    clean_env.setenv("BEDROCK_LOG_LEVEL", "error")
    with _init(log_capture):
        pyrock.warning("dropped")
        pyrock.error("kept")
    assert log_capture.messages() == ["kept"]


@isolated
def test_text_format_writes_key_value_lines(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture, log_format="text"):
        pyrock.info("plain text", user_id="123")
    text = log_capture.text
    assert "plain text" in text
    assert "user_id=123" in text
    assert "service=log-svc" in text
    with pytest.raises(json.JSONDecodeError):
        json.loads(log_capture.lines()[0])


@isolated
def test_log_add_source_records_module_function_and_line(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture, log_add_source=True):
        pyrock.info("with source")
    source = log_capture.find(msg="with source")["source"]
    assert re.fullmatch(r".+:test_log_add_source_records_module_function_and_line:\d+", source)
    assert "/" not in source  # full paths only with allow_source_paths


# -- installation modes -----------------------------------------------------------


@isolated
def test_log_install_pyrock_leaves_root_logger_alone(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    with _init(log_capture, log_install="pyrock"):
        assert root.handlers == handlers_before
        pyrock.info("pyrock helper")
        logging.getLogger("myapp").warning("app logger")
    assert log_capture.messages() == ["pyrock helper"]


@isolated
def test_log_install_none_installs_nothing(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    pyrock_handlers_before = list(logging.getLogger("pyrock").handlers)
    with _init(log_capture, log_install="none"):
        assert root.handlers == handlers_before
        assert logging.getLogger("pyrock").handlers == pyrock_handlers_before
        pyrock.warning("goes nowhere pyrock owns")
    assert log_capture.text == ""


@isolated
def test_correlation_filter_on_an_app_owned_handler(clean_env: pytest.MonkeyPatch) -> None:
    from pyrock import logs

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(trace_id)s|%(span_id)s|%(message)s"))
    handler.addFilter(logs.CorrelationFilter())
    app_logger = logging.getLogger("tests.e2e.app_owned")
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    try:
        with pyrock.init(**quiet(log_install="pyrock")), pyrock.operation("owned") as op:
            app_logger.info("correlated")
    finally:
        app_logger.removeHandler(handler)
        app_logger.propagate = True
    assert stream.getvalue().strip() == f"{op.trace_id}|{op.span_id}|correlated"


# -- canonical logs -------------------------------------------------------------


@isolated
def test_canonical_log_on_success(clean_env: pytest.MonkeyPatch, log_capture: LogCapture) -> None:
    with (
        _init(log_capture, log_canonical=True),
        pyrock.operation("http.request", attrs={"http.method": "GET"}) as op,
    ):
        op.register({"http.status_code": 200})
    record = log_capture.find(msg="operation.complete")
    assert {
        "time",
        "level",
        "msg",
        "service",
        "operation",
        "duration_ms",
        "success",
        "attributes",
        "trace_id",
        "span_id",
    } <= set(record)
    assert record["level"] == "INFO"
    assert record["operation"] == "http.request"
    assert record["success"] is True
    assert isinstance(record["duration_ms"], (int, float))
    assert record["duration_ms"] >= 0
    assert record["attributes"] == {"http.method": "GET", "http.status_code": 200}
    assert record["trace_id"] == op.trace_id
    assert record["span_id"] == op.span_id


@isolated
def test_canonical_log_on_failure_carries_error_fields(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with (
        _init(log_capture, log_canonical=True),
        pytest.raises(TimeoutError),
        pyrock.operation("job"),
    ):
        raise TimeoutError
    record = log_capture.find(msg="operation.complete", operation="job")
    assert record["success"] is False
    assert "error" in record
    assert record["error.type"] == "builtins.TimeoutError"
    assert re.fullmatch(r"[0-9a-f]{16}", record["error.fingerprint"])
    assert "error.message" not in record


@isolated
def test_canonical_log_once_per_operation_and_not_for_steps(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture, log_canonical=True):
        with (
            pyrock.operation("parent"),
            pyrock.step("helper"),
            pyrock.operation("child"),
        ):
            pass
        op = pyrock.start_operation("manual")
        op.done()
        op.done()
    completed = [r["operation"] for r in log_capture.find_all(msg="operation.complete")]
    assert sorted(completed) == ["child", "manual", "parent"]


@isolated
def test_canonical_log_is_off_by_default(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    with _init(log_capture), pyrock.operation("quiet"):
        pass
    assert log_capture.find_all(msg="operation.complete") == []


@isolated
def test_canonical_log_from_environment(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    clean_env.setenv("BEDROCK_LOG_CANONICAL", "true")
    with _init(log_capture), pyrock.operation("from_env"):
        pass
    assert log_capture.find(msg="operation.complete")["operation"] == "from_env"
