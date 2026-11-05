"""Privacy policy end to end (DESIGN §14, Go ``privacy.Policy``).

The same policy must govern every surface: exported span attributes and
resources, log records, and metric label values.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import pytest

from tests.e2e._export_helpers import exposition, prom, running
from tests.support.collectors import OTLPHTTPCollector
from tests.support.logs import LogCapture
from tests.support.wsgi_driver import call_wsgi

pytestmark = pytest.mark.usefixtures("clean_env")

SECRET = "hunter2-very-secret"


def _policy(**kw: Any) -> Any:
    from pyrock import privacy

    return privacy.Policy(**kw)


def _span_with(collector: OTLPHTTPCollector, attrs: dict[str, Any], **overrides: Any) -> Any:
    with running(trace_url=collector.traces_url, **overrides) as b, b.operation("guarded") as op:
        op.register(attrs)
    return collector.span("guarded")


def _wire_text(collector: OTLPHTTPCollector) -> str:
    return "\n".join(r.text for r in collector.trace_requests())


class Opaque:
    """An arbitrary object whose str/repr would leak a secret."""

    def __init__(self) -> None:
        self.rendered = threading.Event()

    def __repr__(self) -> str:
        self.rendered.set()
        return f"Opaque({SECRET})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        self.rendered.set()
        return SECRET


@pytest.fixture
def logger_name() -> str:
    """A stdlib logger below ``pyrock``, so ``log_install="pyrock"`` handles it."""
    return "pyrock.e2e_privacy"


# ------------------------------------------------------------------- defaults


def test_error_message_dropped_by_default(otlp_collector: OTLPHTTPCollector) -> None:
    with (
        pytest.raises(ValueError, match="boom|secret"),
        running(trace_url=otlp_collector.traces_url) as b,
        b.operation("failing"),
    ):
        raise ValueError(SECRET)
    span = otlp_collector.span("failing")
    assert span["attributes"]["error.type"] == "builtins.ValueError"
    assert SECRET not in _wire_text(otlp_collector)


def test_error_message_kept_with_allow_error_details(otlp_collector: OTLPHTTPCollector) -> None:
    with (
        pytest.raises(ValueError, match="boom|secret"),
        running(
            trace_url=otlp_collector.traces_url,
            data_policy=_policy(allow_error_details=True),
        ) as b,
        b.operation("failing"),
    ):
        raise ValueError("detailed boom")
    assert otlp_collector.span("failing")["attributes"]["error.message"] == "detailed boom"


def test_error_message_dropped_from_logs_by_default(log_capture: LogCapture) -> None:
    with running(log_output=log_capture.stream, log_install="pyrock") as b:
        b.error("job failed", RuntimeError(SECRET))
    record = log_capture.find(msg="job failed")
    assert record["error.type"] == "builtins.RuntimeError"
    assert SECRET not in log_capture.text


def test_sensitive_attribute_keys_are_scrubbed(otlp_collector: OTLPHTTPCollector) -> None:
    attrs = {
        "user.password": SECRET,
        "http.authorization": SECRET,
        "session.cookie": SECRET,
        "api_key": SECRET,
        "refresh_token": SECRET,
        "plain": "visible",
    }
    span = _span_with(otlp_collector, attrs)
    assert span["attributes"]["plain"] == "visible"
    assert SECRET not in _wire_text(otlp_collector)


def test_user_agent_is_not_recorded_by_default(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.wsgi import Middleware

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    agent = "SecretAgent/9.9 (" + SECRET + ")"
    with running(trace_url=otlp_collector.traces_url) as b:
        token = b.activate()
        try:
            call_wsgi(Middleware(app), "GET", "/", headers={"User-Agent": agent})
        finally:
            b.deactivate(token)
    assert otlp_collector.spans()
    assert SECRET not in _wire_text(otlp_collector)


def test_log_source_is_module_function_by_default(log_capture: LogCapture) -> None:
    with running(log_output=log_capture.stream, log_install="pyrock", log_add_source=True) as b:
        b.info("where am i")
    source = log_capture.find(msg="where am i")["source"]
    assert __file__ not in source
    assert "/" not in source
    assert "\\" not in source


def test_log_source_is_full_path_when_allowed(log_capture: LogCapture) -> None:
    with running(
        log_output=log_capture.stream,
        log_install="pyrock",
        log_add_source=True,
        data_policy=_policy(allow_source_paths=True),
    ) as b:
        b.info("where am i")
    assert __file__ in log_capture.find(msg="where am i")["source"]


def test_error_stack_omits_source_paths_by_default(log_capture: LogCapture) -> None:
    with running(
        log_output=log_capture.stream,
        log_install="pyrock",
        data_policy=_policy(allow_error_details=True),
    ):
        try:
            raise ValueError("stacked")
        except ValueError:
            logging.getLogger("pyrock").exception("with stack")
    assert __file__ not in log_capture.text


def test_infrastructure_resource_keys_dropped_by_default(
    otlp_collector: OTLPHTTPCollector,
) -> None:
    with (
        running(trace_url=otlp_collector.traces_url, disable_environment_detection=False) as b,
        b.operation("res"),
    ):
        pass
    resource = otlp_collector.span("res")["resource"]
    assert "service.name" in resource
    assert not [k for k in resource if k.startswith(("host.", "process.", "k8s.", "container."))]


def test_resource_keys_allowlist_admits_named_keys(otlp_collector: OTLPHTTPCollector) -> None:
    with (
        running(
            trace_url=otlp_collector.traces_url,
            disable_environment_detection=False,
            data_policy=_policy(resource_keys=["host.name"]),
        ) as b,
        b.operation("res"),
    ):
        pass
    resource = otlp_collector.span("res")["resource"]
    assert "host.name" in resource
    assert "process.pid" not in resource
    assert "service.name" in resource


# --------------------------------------------------------------- allowlists


def test_attribute_keys_is_strict_allowlist(otlp_collector: OTLPHTTPCollector) -> None:
    span = _span_with(
        otlp_collector,
        {"status": "ok", "other": "x"},
        data_policy=_policy(attribute_keys=["status"]),
    )
    assert span["attributes"].get("status") == "ok"
    assert "other" not in span["attributes"]


def test_attribute_keys_group_wildcard(otlp_collector: OTLPHTTPCollector) -> None:
    span = _span_with(
        otlp_collector,
        {"user.id": "1", "user.tier": "gold", "username": "nope", "order.id": "9"},
        data_policy=_policy(attribute_keys=["user.*"]),
    )
    attrs = span["attributes"]
    assert attrs.get("user.id") == "1"
    assert attrs.get("user.tier") == "gold"
    assert "username" not in attrs
    assert "order.id" not in attrs


def test_attribute_allowlist_applies_to_logs(log_capture: LogCapture) -> None:
    with running(
        log_output=log_capture.stream,
        log_install="pyrock",
        data_policy=_policy(attribute_keys=["user.*"]),
    ) as b:
        b.info("filtered", {"user.id": "7", "card.number": "4111"})
    record = log_capture.find(msg="filtered")
    assert record["user.id"] == "7"
    assert "card.number" not in record


# --------------------------------------------------------------------- redact


def test_redact_rewrites_span_value(otlp_collector: OTLPHTTPCollector) -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return ("[email]", True) if path == "user.email" else (value, True)

    span = _span_with(
        otlp_collector, {"user.email": "a@b.c", "kept": "yes"}, data_policy=_policy(redact=redact)
    )
    assert span["attributes"]["user.email"] == "[email]"
    assert span["attributes"]["kept"] == "yes"


def test_redact_can_drop_span_value(otlp_collector: OTLPHTTPCollector) -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return (None, False) if path == "drop.me" else (value, True)

    span = _span_with(
        otlp_collector, {"drop.me": "x", "kept": "yes"}, data_policy=_policy(redact=redact)
    )
    assert "drop.me" not in span["attributes"]
    assert span["attributes"]["kept"] == "yes"


def test_redact_exception_drops_value(otlp_collector: OTLPHTTPCollector) -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        if path == "fragile":
            raise RuntimeError("redactor bug")
        return value, True

    span = _span_with(
        otlp_collector, {"fragile": SECRET, "kept": "yes"}, data_policy=_policy(redact=redact)
    )
    assert "fragile" not in span["attributes"]
    assert span["attributes"]["kept"] == "yes"
    assert SECRET not in _wire_text(otlp_collector)


def test_redact_receives_qualified_nested_paths(log_capture: LogCapture, logger_name: str) -> None:
    seen: list[str] = []
    lock = threading.Lock()

    def redact(path: str, value: Any) -> tuple[Any, bool]:
        with lock:
            seen.append(path)
        return value, True

    with running(
        log_output=log_capture.stream,
        log_install="pyrock",
        data_policy=_policy(redact=redact),
    ):
        logging.getLogger(logger_name).info(
            "nested", extra={"request": {"headers": {"x-request-id": "r-1"}}}
        )
    assert "request.headers.x-request-id" in seen
    assert log_capture.find(msg="nested")["request"] == {"headers": {"x-request-id": "r-1"}}


def test_redact_rewrites_nested_log_value(log_capture: LogCapture, logger_name: str) -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return ("***", True) if path == "request.headers.x-api-token" else (value, True)

    with running(
        log_output=log_capture.stream,
        log_install="pyrock",
        data_policy=_policy(redact=redact),
    ):
        logging.getLogger(logger_name).info(
            "nested", extra={"request": {"headers": {"x-api-token": SECRET, "accept": "*/*"}}}
        )
    headers = log_capture.find(msg="nested")["request"]["headers"]
    assert headers.get("accept") == "*/*"
    assert SECRET not in log_capture.text


# ------------------------------------------------------------ walker limits


def test_cycles_are_replaced_with_marker(log_capture: LogCapture, logger_name: str) -> None:
    from pyrock.privacy import CYCLE_MARKER

    loop: dict[str, Any] = {"name": "root"}
    loop["self"] = loop
    with running(log_output=log_capture.stream, log_install="pyrock"):
        logging.getLogger(logger_name).info("cyclic", extra={"payload": loop})
    payload = log_capture.find(msg="cyclic")["payload"]
    assert payload["name"] == "root"
    assert payload["self"] == CYCLE_MARKER


def test_list_cycles_are_replaced_with_marker(log_capture: LogCapture, logger_name: str) -> None:
    from pyrock.privacy import CYCLE_MARKER

    items: list[Any] = [1]
    items.append(items)
    with running(log_output=log_capture.stream, log_install="pyrock"):
        logging.getLogger(logger_name).info("cyclic list", extra={"items": items})
    assert log_capture.find(msg="cyclic list")["items"] == [1, CYCLE_MARKER]


def test_repeated_aliases_are_replaced_with_marker(
    log_capture: LogCapture, logger_name: str
) -> None:
    from pyrock.privacy import ALIAS_MARKER

    shared = {"x": 1}
    with running(log_output=log_capture.stream, log_install="pyrock"):
        logging.getLogger(logger_name).info(
            "aliased", extra={"payload": {"a": shared, "b": shared}}
        )
    payload = log_capture.find(msg="aliased")["payload"]
    assert payload["a"] == {"x": 1}
    assert payload["b"] == ALIAS_MARKER


def test_unknown_object_in_span_is_suppressed(otlp_collector: OTLPHTTPCollector) -> None:
    obj = Opaque()
    span = _span_with(otlp_collector, {"obj": obj, "kept": "yes"})
    assert "obj" not in span["attributes"]
    assert span["attributes"]["kept"] == "yes"
    assert not obj.rendered.is_set()
    assert SECRET not in _wire_text(otlp_collector)


def test_unknown_object_in_log_is_never_rendered(log_capture: LogCapture, logger_name: str) -> None:
    obj = Opaque()
    with running(log_output=log_capture.stream, log_install="pyrock"):
        logging.getLogger(logger_name).info("opaque", extra={"thing": obj, "nested": {"o": obj}})
    log_capture.find(msg="opaque")
    assert not obj.rendered.is_set()
    assert SECRET not in log_capture.text


def test_unknown_object_in_metric_label_is_suppressed() -> None:
    obj = Opaque()
    with running() as b:
        with b.operation("opaque_label", metric_labels=["thing"]) as op:
            op.register({"thing": obj})
        text = exposition(b)
    assert not obj.rendered.is_set()
    assert SECRET not in text


def test_max_string_length_truncates_span_values(otlp_collector: OTLPHTTPCollector) -> None:
    span = _span_with(
        otlp_collector, {"long": "a" * 500}, data_policy=_policy(max_string_length=16)
    )
    value = span["attributes"]["long"]
    assert value.startswith("a" * 8)
    assert len(value) <= 16 + len("[truncated]")


def test_default_max_string_length_is_applied(otlp_collector: OTLPHTTPCollector) -> None:
    from pyrock.privacy import DEFAULT_MAX_STRING_LENGTH

    span = _span_with(otlp_collector, {"long": "b" * (DEFAULT_MAX_STRING_LENGTH * 4)})
    assert len(span["attributes"]["long"]) < DEFAULT_MAX_STRING_LENGTH * 2


def test_max_string_length_truncates_log_values(log_capture: LogCapture) -> None:
    with running(
        log_output=log_capture.stream,
        log_install="pyrock",
        data_policy=_policy(max_string_length=16),
    ) as b:
        b.info("long", payload="c" * 500)
    assert len(log_capture.find(msg="long")["payload"]) <= 16 + len("[truncated]")


# ---------------------------------------------------------------- consistency


def _email_redactor(path: str, value: Any) -> tuple[Any, bool]:
    if path.endswith("user.email") or path == "user_email":
        return "[email]", True
    return value, True


def test_redact_applies_to_metric_labels() -> None:
    with running(data_policy=_policy(redact=_email_redactor)) as b:
        with b.operation("signup", metric_labels=["user.email"]) as op:
            op.register({"user.email": "a@b.c"})
        text = prom(b)
    assert text.value("signup_count", user_email="[email]") == 1
    assert "a@b.c" not in exposition(b)


def test_redact_applies_to_logs(log_capture: LogCapture) -> None:
    with running(
        log_output=log_capture.stream,
        log_install="pyrock",
        data_policy=_policy(redact=_email_redactor),
    ) as b:
        b.info("signup", {"user.email": "a@b.c"})
    assert log_capture.find(msg="signup")["user.email"] == "[email]"
    assert "a@b.c" not in log_capture.text


def test_redact_applies_to_span_events(otlp_collector: OTLPHTTPCollector) -> None:
    with (
        running(
            trace_url=otlp_collector.traces_url,
            data_policy=_policy(redact=_email_redactor),
        ) as b,
        b.operation("evented") as op,
    ):
        op.event("signup", {"user.email": "a@b.c"})
    event = otlp_collector.span("evented")["events"][0]
    assert event["attributes"]["user.email"] == "[email]"
    assert "a@b.c" not in _wire_text(otlp_collector)


def test_sensitive_metric_label_value_is_hidden() -> None:
    with running() as b:
        with b.operation("login", metric_labels=["password"]) as op:
            op.register(password=SECRET)
        text = exposition(b)
    assert SECRET not in text
    assert prom(b).value("login_count") == 1


def test_sensitive_log_field_is_hidden(log_capture: LogCapture) -> None:
    with running(log_output=log_capture.stream, log_install="pyrock") as b:
        b.info("login", password=SECRET, user="alice")
    assert log_capture.find(msg="login")["user"] == "alice"
    assert SECRET not in log_capture.text


def test_policy_applies_to_canonical_log(log_capture: LogCapture) -> None:
    with (
        running(
            log_output=log_capture.stream,
            log_install="pyrock",
            log_canonical=True,
            data_policy=_policy(redact=_email_redactor),
        ) as b,
        b.operation("canon") as op,
    ):
        op.register({"user.email": "a@b.c"})
    record = log_capture.find(msg="operation.complete")
    assert "a@b.c" not in json.dumps(record)
