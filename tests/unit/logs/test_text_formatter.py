"""TextFormatter: ``key=value`` lines with the same fields as JSONFormatter (Go slog text)."""

from __future__ import annotations

import logging

from pyrock import privacy
from pyrock.logs import ATTRS_KEY, TextFormatter
from tests.unit.logs._helpers import FIXED_TIME, SPAN_ID, TRACE_ID, exc_info_of, make_record


def _line(fmt: TextFormatter, record: logging.LogRecord) -> str:
    line = fmt.format(record)
    assert "\n" not in line, line
    return line


def test_core_fields_in_go_order() -> None:
    line = _line(TextFormatter(), make_record("hello"))
    assert line.startswith(f"time={FIXED_TIME} level=INFO msg=hello")
    assert "logger=myapp.users" in line


def test_values_with_spaces_are_quoted() -> None:
    line = _line(TextFormatter(), make_record("hello world", extra={ATTRS_KEY: {"note": "a b"}}))
    assert 'msg="hello world"' in line
    assert 'note="a b"' in line


def test_newlines_are_escaped_onto_one_line() -> None:
    line = _line(TextFormatter(), make_record("multi\nline", extra={ATTRS_KEY: {"n": "x\ny"}}))
    assert "multi\\nline" in line


def test_fields_match_json_formatter_fields() -> None:
    fmt = TextFormatter(service="svc", static_attrs={"env": "prod"})
    line = _line(
        fmt,
        make_record(
            level=logging.WARNING,
            extra={ATTRS_KEY: {"user_id": "123", "n": 3}, "trace_id": TRACE_ID, "span_id": SPAN_ID},
        ),
    )
    for part in (
        "level=WARN",
        "service=svc",
        "env=prod",
        "user_id=123",
        "n=3",
        f"trace_id={TRACE_ID}",
        f"span_id={SPAN_ID}",
    ):
        assert part in line.split(" "), part


def test_text_formatter_applies_policy_and_suppresses_unknown_objects() -> None:
    class Secret:
        def __repr__(self) -> str:
            return "hunter2"

        __str__ = __repr__

    fmt = TextFormatter(policy=privacy.Policy(attribute_keys=["ok"]))
    line = _line(fmt, make_record(extra={ATTRS_KEY: {"ok": 1, "hidden": "x"}, "s": Secret()}))
    assert "ok=1" in line
    assert "hidden" not in line
    assert "hunter2" not in line


def test_text_formatter_error_fields() -> None:
    line = _line(TextFormatter(), make_record(exc_info=exc_info_of(ValueError("secret"))))
    assert "error.type=builtins.ValueError" in line
    assert "secret" not in line
    assert "Traceback" not in line


def test_text_formatter_add_source() -> None:
    line = _line(TextFormatter(add_source=True), make_record())
    assert "source=users:load_user:42" in line.split(" ")
