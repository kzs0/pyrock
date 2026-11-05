"""Lifecycle: init from env and keywords, activation, shutdown, the observability server.

DESIGN.md §4.1, §4.7, §6.1, §11; Go: ``Init``, ``New``, ``Shutdown``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import urllib.error
import urllib.request

import pytest

import pyrock
from tests.e2e._core_helpers import isolated, prom_of, quiet, run, span_attr_or_resource
from tests.support.collectors import OTLPHTTPCollector
from tests.support.logs import LogCapture

# -- building the config ------------------------------------------------------


@isolated
def test_init_reads_service_from_environment(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    clean_env.setenv("BEDROCK_SERVICE", "env-service")
    with pyrock.init(**quiet(log_install="pyrock", log_output=log_capture.stream)):
        pyrock.info("hello")
    assert log_capture.find(msg="hello")["service"] == "env-service"


@isolated
def test_keyword_overrides_win_over_environment_and_keep_the_rest(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("BEDROCK_SERVICE", "env-service")
    clean_env.setenv("BEDROCK_METRIC_PREFIX", "envprefix")
    with pyrock.init(**quiet(service="kw-service")) as bedrock:
        assert bedrock.config.service == "kw-service"
        with pyrock.operation("job"):
            pass
        # The env-derived prefix survives the unrelated keyword override.
        assert prom_of(bedrock).value("envprefix_job_count") == 1


@isolated
def test_explicit_config_is_used_instead_of_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("BEDROCK_SERVICE", "env-service")
    with pyrock.init(pyrock.Config(service="cfg-service", **quiet())) as bedrock:
        assert bedrock.config.service == "cfg-service"


@isolated
def test_keyword_overrides_apply_on_top_of_explicit_config(clean_env: pytest.MonkeyPatch) -> None:
    config = pyrock.Config(service="cfg-service", metric_prefix="cfg", **quiet())
    with pyrock.init(config, service="kw-service") as bedrock:
        assert bedrock.config.service == "kw-service"
        assert bedrock.config.metric_prefix == "cfg"


@isolated
def test_unknown_override_raises_type_error(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(TypeError, match="not_a_config_field"):
        pyrock.init(**quiet(), not_a_config_field=1)


@isolated
def test_unknown_override_on_explicit_instance_raises_type_error(
    clean_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError, match="not_a_config_field"):
        pyrock.Bedrock(**quiet(), not_a_config_field=1)


@isolated
def test_invalid_environment_raises_config_error_listing_every_problem(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("BEDROCK_TRACE_SAMPLE_RATE", "2.5")
    clean_env.setenv("BEDROCK_SERVER_READ_TIMEOUT", "banana")
    clean_env.setenv("BEDROCK_LOG_LEVEL", "loud")
    with pytest.raises(pyrock.ConfigError) as excinfo:
        pyrock.init(**quiet())
    error = excinfo.value
    assert isinstance(error, ValueError)
    assert len(error.problems) >= 3
    text = " ".join(error.problems).lower()
    for env_var, field in (
        ("BEDROCK_TRACE_SAMPLE_RATE", "trace_sample_rate"),
        ("BEDROCK_SERVER_READ_TIMEOUT", "server_read_timeout"),
        ("BEDROCK_LOG_LEVEL", "log_level"),
    ):
        assert env_var.lower() in text or field in text, (env_var, error.problems)


@isolated
def test_invalid_override_raises_config_error(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(pyrock.ConfigError):
        pyrock.init(**quiet(trace_sample_rate=1.5))


# -- activation -----------------------------------------------------------------


@isolated
def test_init_activates_the_instance_in_the_current_context(clean_env: pytest.MonkeyPatch) -> None:
    with pyrock.init(**quiet()) as bedrock:
        assert pyrock.current() is bedrock
        assert not bedrock.is_noop


@isolated
def test_with_block_exit_shuts_down_and_deactivates(clean_env: pytest.MonkeyPatch) -> None:
    with pyrock.init(**quiet()) as bedrock:
        assert not bedrock.closed
    assert bedrock.closed
    assert pyrock.current() is not bedrock
    assert pyrock.current().is_noop


@isolated
def test_activate_false_leaves_context_untouched(clean_env: pytest.MonkeyPatch) -> None:
    bedrock = pyrock.init(**quiet(), activate=False)
    try:
        assert pyrock.current().is_noop
        with bedrock.operation("explicit"):
            pass
        assert prom_of(bedrock).value("explicit_count") == 1
    finally:
        bedrock.shutdown()


@isolated
def test_explicit_instance_is_not_activated_and_its_methods_record(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    bedrock = pyrock.Bedrock(**quiet(service="library", trace_url=otlp_collector.traces_url))
    try:
        assert pyrock.current().is_noop
        with bedrock.operation("lib.call", metric_labels=["kind"]) as op:
            op.register(kind="read")
            with bedrock.step("lib.helper"):
                pass
        bedrock.counter("lib_events", "Library events").inc()
        exposition = prom_of(bedrock)
        assert exposition.value("lib_call_count", kind="read") == 1
        assert exposition.value("lib_events") == 1
    finally:
        bedrock.shutdown()
    assert set(otlp_collector.span_names()) == {"lib.call", "lib.helper"}


@isolated
def test_module_helpers_accept_a_pinned_instance(clean_env: pytest.MonkeyPatch) -> None:
    bedrock = pyrock.Bedrock(**quiet())
    try:
        with pyrock.operation("pinned", bedrock=bedrock):
            pass
        assert prom_of(bedrock).value("pinned_count") == 1
    finally:
        bedrock.shutdown()


@isolated
def test_activate_and_deactivate_an_explicit_instance(clean_env: pytest.MonkeyPatch) -> None:
    bedrock = pyrock.Bedrock(**quiet())
    try:
        token = bedrock.activate()
        assert pyrock.current() is bedrock
        bedrock.deactivate(token)
        assert pyrock.current().is_noop
    finally:
        bedrock.shutdown()


@isolated
def test_static_attrs_reach_spans_metrics_and_logs(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector, log_capture: LogCapture
) -> None:
    with pyrock.init(
        **quiet(
            service="static-svc",
            trace_url=otlp_collector.traces_url,
            log_install="pyrock",
            log_output=log_capture.stream,
        ),
        static_attrs={"env": "production", "version": "1.2.3"},
    ) as bedrock:
        with pyrock.operation("static_op"):
            pyrock.info("inside")
        exposition = prom_of(bedrock)
    assert exposition.value("static_op_count", env="production", version="1.2.3") == 1
    record = log_capture.find(msg="inside")
    assert record["env"] == "production"
    assert record["version"] == "1.2.3"
    span = otlp_collector.wait_for_spans(1)[0]
    assert span["resource"]["service.name"] == "static-svc"
    assert span_attr_or_resource(span, "env") == "production"
    assert span_attr_or_resource(span, "version") == "1.2.3"


@isolated
def test_user_static_attrs_win_over_detected_environment(clean_env: pytest.MonkeyPatch) -> None:
    overrides = quiet(disable_environment_detection=False)
    with pyrock.init(**overrides, static_attrs={"host.name": "chosen-by-user"}) as bedrock:
        assert bedrock.static_attrs["host.name"] == "chosen-by-user"


# -- shutdown -----------------------------------------------------------------


@isolated
def test_shutdown_flushes_pending_spans(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    bedrock = pyrock.init(**quiet(trace_url=otlp_collector.traces_url))
    with pyrock.operation("flush_me"):
        pass
    bedrock.shutdown(timeout=5.0)
    # No waiting: shutdown returns only after the batch has been exported.
    assert otlp_collector.span_names() == ["flush_me"]
    assert bedrock.closed


@isolated
def test_shutdown_is_idempotent(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    bedrock = pyrock.init(**quiet(trace_url=otlp_collector.traces_url))
    with pyrock.operation("once"):
        pass
    bedrock.shutdown()
    bedrock.shutdown()
    bedrock.shutdown(timeout=1.0)
    assert otlp_collector.span_names() == ["once"]


@isolated
def test_concurrent_shutdown_is_safe_and_exports_once(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    bedrock = pyrock.init(**quiet(trace_url=otlp_collector.traces_url))
    with pyrock.operation("racy"):
        pass
    workers = 8
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def stop() -> None:
        barrier.wait()
        try:
            bedrock.shutdown(timeout=5.0)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=stop) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert bedrock.closed
    assert otlp_collector.span_names() == ["racy"]


@isolated
def test_ashutdown_flushes_without_blocking_the_loop(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    bedrock = pyrock.init(**quiet(trace_url=otlp_collector.traces_url))

    async def main() -> None:
        with pyrock.operation("async_flush"):
            await asyncio.sleep(0)
        await bedrock.ashutdown(timeout=5.0)

    run(main())
    assert bedrock.closed
    assert otlp_collector.span_names() == ["async_flush"]


@isolated
def test_async_context_manager_shuts_down_on_exit(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    async def main() -> pyrock.Bedrock:
        async with pyrock.init(**quiet(trace_url=otlp_collector.traces_url)) as bedrock:
            assert pyrock.current() is bedrock
            async with pyrock.operation("async_op"):
                await asyncio.sleep(0)
        return bedrock

    bedrock = run(main())
    assert bedrock.closed
    assert otlp_collector.span_names() == ["async_op"]


@isolated
def test_shutdown_restores_root_logging_handlers_and_level(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    bedrock = pyrock.init(
        **quiet(log_install="root", log_level="debug", log_output=log_capture.stream)
    )
    assert any(handler not in handlers_before for handler in root.handlers)
    assert root.level == logging.DEBUG
    bedrock.shutdown()
    assert root.handlers == handlers_before
    assert root.level == level_before


@isolated
def test_logs_after_shutdown_no_longer_reach_the_pyrock_stream(
    clean_env: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    bedrock = pyrock.init(**quiet(log_install="root", log_output=log_capture.stream))
    bedrock.shutdown()
    logging.getLogger("after.shutdown").warning("late message")
    assert "late message" not in log_capture.text


# -- observability server -------------------------------------------------------


@isolated
def test_server_binds_ephemeral_port_and_serves_metrics_and_health(
    clean_env: pytest.MonkeyPatch,
) -> None:
    overrides = quiet(server_enabled=True, server_addr="127.0.0.1:0")
    with pyrock.init(**overrides) as bedrock:
        address = bedrock.server_address
        assert address is not None
        host, port = address
        assert host == "127.0.0.1"
        assert port > 0
        with pyrock.operation("served"):
            pass
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
        assert "served_count" in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            assert resp.status == 200
    assert bedrock.server_address is None
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)


@isolated
def test_disabled_server_has_no_address(clean_env: pytest.MonkeyPatch) -> None:
    with pyrock.init(**quiet()) as bedrock:
        assert bedrock.server_address is None
