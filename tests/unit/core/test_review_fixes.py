"""Core behaviour added by the Phase 4 review fixes (WP-1)."""

from __future__ import annotations

import contextvars
import gc
import io
import logging
import threading
from typing import Any

import pytest

import pyrock
from pyrock import logs
from pyrock.testing import capture

QUIET: dict[str, Any] = {
    "server_enabled": False,
    "disable_environment_detection": True,
    "runtime_metrics": False,
    "log_install": "none",
}


def _in_empty_context(fn: Any) -> Any:
    return contextvars.Context().run(fn)


# -- P0-2: metric registration errors never escape operations or sources -------


def test_conflicting_metric_labels_do_not_fail_the_operation() -> None:
    with capture(service="t") as t:
        with pyrock.operation("job", metric_labels=["a"]):
            pass
        for _ in range(3):
            with pyrock.operation("job", metric_labels=["b"]) as op:
                op.register(b="x")
        assert t.metrics.value("job_count") == 1
    assert len(t.spans.find(name="job")) == 4
    warnings = [r for r in t.logs if "not recorded" in r["msg"]]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "WARN"


def test_family_limit_does_not_fail_operations() -> None:
    with capture(service="t", metric_family_limit=8) as t:
        for i in range(5):  # four families each: the second already overflows
            with pyrock.operation(f"op{i}"):
                pass
    assert len(t.spans) == 5
    assert t.metrics.value("op0_count") == 1
    assert t.metrics.value("op4_count") is None


def test_source_aggregate_conflict_is_logged_not_raised() -> None:
    with capture(service="t") as t:
        with pyrock.source("worker") as src:
            src.gauge("depth", 3)
            src.sum("depth", 1)  # worker_depth is already a gauge
            src.histogram("depth", 2.0)
        assert t.metrics.value("worker_depth") == 3
    assert len([r for r in t.logs if "not recorded" in r["msg"]]) == 1


def test_direct_metric_helpers_still_raise_on_conflict() -> None:
    with capture(service="t"):
        pyrock.counter("hits", labels=["a"])
        with pytest.raises(pyrock.MetricConflictError):
            pyrock.counter("hits", labels=["b"])


# -- P0-4 / P2-15: one level table -------------------------------------------------


def test_critical_level_works_end_to_end() -> None:
    stream = io.StringIO()
    with pyrock.init(**{**QUIET, "log_install": "pyrock"}, log_level="critical", log_output=stream):
        pyrock.error("hidden")
        pyrock.log(logging.CRITICAL, "shown")
    assert "shown" in stream.getvalue()
    assert "hidden" not in stream.getvalue()


def test_level_problem_lists_every_accepted_level() -> None:
    with pytest.raises(pyrock.ConfigError) as info:
        pyrock.Config(log_level="loud")
    (problem,) = info.value.problems
    for name in ("debug", "info", "warn", "warning", "error", "critical"):
        assert name in problem
    assert logs.parse_level("CRITICAL") == logging.CRITICAL
    with pytest.raises(ValueError, match="critical"):
        logs.parse_level("loud")


# -- P1-9: a shut-down active instance behaves as inactive ------------------------


def test_shut_down_active_instance_is_treated_as_noop() -> None:
    def body() -> None:
        b = pyrock.init(**QUIET)
        assert pyrock.current() is b
        b.shutdown()
        assert pyrock.current().is_noop
        with pyrock.operation("after") as op:
            assert op.trace_id is None
        assert pyrock.start_operation("manual").trace_id is None
        assert pyrock.counter("c") is pyrock.current().counter("c")

    _in_empty_context(body)


# -- P1-12 / P1-14: config validation -------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profiles", "cpu"),
        ("trace_sampler", 0.5),
        ("server_profile_handler", "not callable"),
        ("log_output", 42),
    ],
)
def test_object_fields_are_type_checked(field: str, value: object) -> None:
    with pytest.raises(pyrock.ConfigError) as info:
        pyrock.Config(**{field: value})
    assert any(field in p for p in info.value.problems)


@pytest.mark.parametrize("key", ["1abc", "le", "__x"])
def test_bad_static_attr_keys_raise_config_error_without_leaking(key: str) -> None:
    callbacks = len(gc.callbacks)
    root_handlers = list(logging.getLogger().handlers)
    with pytest.raises(pyrock.ConfigError) as info:
        pyrock.Bedrock(
            pyrock.Config.default(),
            static_attrs={key: "v"},
            **{**QUIET, "runtime_metrics": True, "log_install": "root"},
        )
    assert key in str(info.value)
    assert len(gc.callbacks) == callbacks
    assert logging.getLogger().handlers == root_handlers


def test_bad_grpc_target_is_a_config_error() -> None:
    with pytest.raises(pyrock.ConfigError, match="trace_url"):
        pyrock.Bedrock(trace_url="ftp://collector:4317", trace_protocol="grpc", **QUIET)


# -- P1-4b: shutdown does not hold the lifecycle lock during I/O --------------------


class _BlockingProcessor:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def on_end(self, span: Any, /) -> None:
        pass

    def force_flush(self, *, timeout: float | None = None) -> bool:
        return True

    def shutdown(self, *, timeout: float | None = None) -> None:
        self.entered.set()
        self.release.wait(10)


def test_shutdown_keeps_accessors_responsive_and_concurrent_callers_wait() -> None:
    b = pyrock.Bedrock(**QUIET)
    processor = _BlockingProcessor()
    b.tracer.set_processor(processor)
    first = threading.Thread(target=b.shutdown)
    first.start()
    assert processor.entered.wait(5)
    done = threading.Event()
    stats = threading.Thread(target=lambda: (b.profile_stats(), done.set()))
    stats.start()
    assert done.wait(5), "profile_stats() blocked behind shutdown"
    second_done = threading.Event()
    second = threading.Thread(target=lambda: (b.shutdown(), second_done.set()))
    second.start()
    assert not second_done.wait(0.2), "a concurrent shutdown() returned before the first ended"
    processor.release.set()
    first.join(5)
    second.join(5)
    stats.join(5)
    assert second_done.is_set()
    assert b.closed


def test_concurrent_shutdown_waits_at_most_its_timeout() -> None:
    b = pyrock.Bedrock(**QUIET)
    processor = _BlockingProcessor()
    b.tracer.set_processor(processor)
    first = threading.Thread(target=b.shutdown)
    first.start()
    assert processor.entered.wait(5)
    b.shutdown(timeout=0.05)  # returns although the first is still running
    processor.release.set()
    first.join(5)


# -- P1-11b: cached series per label-value tuple ----------------------------------


def test_label_values_that_compare_equal_stay_distinct_series() -> None:
    with capture(service="t") as t:
        for value in ("1", 1, True, 1.0, [1], [1]):
            with pyrock.operation("v", metric_labels=["k"]) as op:
                op.register(k=value)
        with pyrock.operation("v", metric_labels=["k"]):
            pass
        text = t.metrics.text()
    assert 'k="true"' in text
    assert 'k="1.0"' in text
    assert t.metrics.value("v_count", k="1") == 2  # "1" and 1
    assert t.metrics.value("v_count", k="_") == 3  # the lists and the missing label
    assert t.metrics.value("v_successes") == 7


# -- P2-5: unbalanced activations ---------------------------------------------------


def test_unbalanced_activation_still_ends_the_scope_and_warns() -> None:
    with capture(service="t") as t:
        with pyrock.operation("outer") as outer:
            inner = pyrock.start_operation("inner")
            inner.activate()  # never deactivated
        assert outer.ended
        assert pyrock.current_operation() is None
        inner.end()
    assert t.spans.one(name="outer")
    assert any("not the innermost" in r["msg"] for r in t.logs)


# -- P3-5 / P3-6 / P3-7 / P3-15 ----------------------------------------------------


def test_atexit_registration_is_dropped_on_shutdown() -> None:
    from pyrock import _bedrock

    def body() -> None:
        b = pyrock.init(**QUIET, shutdown_atexit=True)
        assert id(b) in _bedrock._atexit_instances._instances
        b.shutdown()
        assert id(b) not in _bedrock._atexit_instances._instances

    _in_empty_context(body)


def test_bind_with_arguments_keeps_the_function_metadata() -> None:
    def greet(name: str, punctuation: str = "!") -> str:
        """Say hello."""
        return f"hi {name}{punctuation}"

    bound = pyrock.bind(greet, "ann")
    assert bound.__name__ == "greet"
    assert bound.__doc__ == "Say hello."
    assert bound(punctuation="?") == "hi ann?"


def test_extra_public_names() -> None:
    assert pyrock.Activation is type(pyrock.start_operation("x").activate())
    assert issubclass(pyrock.MetricConflictError, ValueError)
    assert issubclass(pyrock.MetricLimitError, RuntimeError)
    executor = pyrock.ContextThreadPoolExecutor(max_workers=1)
    var: contextvars.ContextVar[int] = contextvars.ContextVar("v", default=0)
    var.set(7)
    try:
        assert executor.submit(var.get).result(5) == 7
        assert type(executor).__qualname__ == "ContextThreadPoolExecutor"
    finally:
        executor.shutdown()


def test_capture_installs_logging_only_when_entered() -> None:
    logger = logging.getLogger(logs.LOGGER_NAME)
    before = list(logger.handlers)
    t = capture(service="t")
    assert logger.handlers == before
    with t:
        assert logger.handlers != before
        pyrock.info("inside")
    assert logger.handlers == before
    assert t.logs.find(msg="inside")
