"""log_canonical: the ``operation.complete`` record (Go ``logCanonical``; PLAN decision 6)."""

from __future__ import annotations

import logging

import pytest

from pyrock import privacy
from pyrock.logs import CANONICAL_MESSAGE, BedrockHandler, JSONFormatter, log_canonical
from tests.unit.logs._helpers import SPAN_ID, TRACE_ID, Capture, detached_logger, span_context

BASE_KEYS = {
    "time",
    "level",
    "msg",
    "operation",
    "duration_ms",
    "success",
    "attributes",
    "trace_id",
    "span_id",
    "logger",
}

FAILURE_ATTRS = {
    "error": True,
    "error.type": "builtins.TimeoutError",
    "error.fingerprint": "0123456789abcdef",
    "error.message": "secret-detail",
    "job.id": "j1",
}


def _logger(cap: Capture, policy: privacy.Policy | None = None) -> logging.Logger:
    handler = BedrockHandler(cap.stream, formatter=JSONFormatter(policy=policy))
    return detached_logger("pyrock", handler)


def test_success_record_has_exact_keys() -> None:
    cap = Capture()
    log_canonical(
        _logger(cap),
        operation="http.request",
        duration_ms=12.5,
        success=True,
        attributes={"http.method": "GET", "http.status_code": 200},
        span_context=span_context(),
    )
    record = cap.one()
    assert set(record) == BASE_KEYS
    assert record["msg"] == CANONICAL_MESSAGE == "operation.complete"
    assert record["level"] == "INFO"
    assert record["operation"] == "http.request"
    assert record["duration_ms"] == pytest.approx(12.5)
    assert record["success"] is True
    assert record["attributes"] == {"http.method": "GET", "http.status_code": 200}
    assert (record["trace_id"], record["span_id"]) == (TRACE_ID, SPAN_ID)
    assert record["logger"] == "pyrock"


def test_failure_record_adds_top_level_error_fields() -> None:
    cap = Capture()
    log_canonical(
        _logger(cap),
        operation="job",
        duration_ms=1.0,
        success=False,
        attributes=FAILURE_ATTRS,
        span_context=span_context(),
    )
    record = cap.one()
    assert set(record) == BASE_KEYS | {"error", "error.type", "error.fingerprint"}
    assert record["success"] is False
    assert record["error.type"] == "builtins.TimeoutError"
    assert record["error.fingerprint"] == "0123456789abcdef"
    assert "secret-detail" not in cap.text
    assert record["attributes"]["job.id"] == "j1"


def test_failure_record_includes_message_with_allow_error_details() -> None:
    cap = Capture()
    log_canonical(
        _logger(cap, privacy.Policy(allow_error_details=True)),
        operation="job",
        duration_ms=1.0,
        success=False,
        attributes=FAILURE_ATTRS,
        span_context=span_context(),
    )
    record = cap.one()
    assert set(record) == BASE_KEYS | {"error", "error.type", "error.fingerprint", "error.message"}
    assert record["error.message"] == "secret-detail"


def test_success_record_has_no_error_fields_even_if_attributes_have_them() -> None:
    cap = Capture()
    log_canonical(
        _logger(cap),
        operation="op",
        duration_ms=0.0,
        success=True,
        attributes={"error.type": "x.Y", "k": 1},
        span_context=span_context(),
    )
    record = cap.one()
    assert "error.type" not in record
    assert "error.fingerprint" not in record


def test_without_span_context_ids_are_absent_or_empty() -> None:
    cap = Capture()
    log_canonical(_logger(cap), operation="op", duration_ms=0.0, success=True, attributes={})
    record = cap.one()
    assert not record.get("trace_id")
    assert not record.get("span_id")
    assert record["attributes"] == {}


def test_canonical_attributes_pass_through_the_policy() -> None:
    class Secret:
        def __repr__(self) -> str:
            return "hunter2"

        __str__ = __repr__

    cap = Capture()
    log_canonical(
        _logger(cap),
        operation="op",
        duration_ms=0.0,
        success=True,
        attributes={"http.route": "/u", "obj": Secret()},
        span_context=span_context(),
    )
    record = cap.one()
    assert record["attributes"] == {"http.route": "/u"}
    assert "hunter2" not in cap.text


def test_canonical_respects_logger_level() -> None:
    cap = Capture()
    log = _logger(cap)
    log.setLevel(logging.WARNING)
    log_canonical(log, operation="op", duration_ms=0.0, success=True, attributes={})
    assert cap.text == ""
