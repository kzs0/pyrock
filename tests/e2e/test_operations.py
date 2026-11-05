"""Operations: metrics, outcomes, errors, nesting, numbering, no_trace, decorators.

DESIGN.md §4.2, §4.5, §5; Go: ``Operation``, ``Register``, ``Done``, ``End``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
from collections.abc import AsyncIterator, Iterator

import pytest

import pyrock
from pyrock.testing import capture
from tests.e2e._core_helpers import count, isolated, prom_text, quiet, run, spans_named
from tests.support.collectors import OTLPHTTPCollector


class NotFoundError(Exception):
    """A user-defined error type, to check qualified ``error.type`` names."""


def _qualified(tp: type[BaseException]) -> str:
    return f"{tp.__module__}.{tp.__qualname__}"


def _raise_value_error() -> None:
    raise ValueError("bad input")


def _raise_not_found() -> None:
    raise NotFoundError("missing")


def _raise_wrapped(*, explicit: bool) -> None:
    try:
        raise KeyError("inner")
    except KeyError as inner:
        if explicit:
            raise RuntimeError("outer") from inner
        raise RuntimeError("outer")  # noqa: B904 - implicit __context__ on purpose


def _event_names(span: object) -> list[str]:
    return [event.name for event in span.events]  # type: ignore[attr-defined]


# -- the four metrics ---------------------------------------------------------


def test_operation_records_four_metrics_with_exact_names() -> None:
    with capture() as t, pyrock.operation("process_user"):
        pass
    exposition = prom_text(t)
    assert exposition.type("process_user_count") == "counter"
    assert exposition.type("process_user_successes") == "counter"
    assert exposition.type("process_user_failures") == "counter"
    assert exposition.type("process_user_duration_ms") == "histogram"
    assert exposition.value("process_user_count") == 1
    assert exposition.value("process_user_successes") == 1
    assert count(exposition.value("process_user_failures")) == 0
    assert exposition.value("process_user_duration_ms_count") == 1
    assert exposition.value("process_user_duration_ms_sum") >= 0
    assert exposition.has("process_user_duration_ms_bucket")


def test_metric_prefix_is_prepended_to_operation_metrics() -> None:
    with capture(metric_prefix="myapp") as t, pyrock.operation("process"):
        pass
    exposition = prom_text(t)
    assert exposition.value("myapp_process_count") == 1
    assert not exposition.has("process_count")


def test_static_attrs_become_labels_with_dots_as_underscores() -> None:
    with (
        capture(static_attrs={"env": "prod", "deploy.region": "eu"}) as t,
        pyrock.operation("job"),
    ):
        pass
    assert t.metrics.value("job_count", env="prod", deploy_region="eu") == 1


def test_declared_labels_default_to_underscore() -> None:
    with (
        capture() as t,
        pyrock.operation("process_user", metric_labels=["status", "tier"]) as op,
    ):
        op.register(status="active")
    assert t.metrics.value("process_user_count", status="active", tier="_") == 1


def test_dotted_label_keys_are_exposed_with_underscores() -> None:
    with (
        capture() as t,
        pyrock.operation("handler", attrs={"http.method": "GET"}, metric_labels=["http.method"]),
    ):
        pass
    assert prom_text(t).label_sets("handler_count") == [{"http_method": "GET"}]


def test_undeclared_attrs_never_become_labels() -> None:
    with (
        capture() as t,
        pyrock.operation(
            "process_user", attrs={"user_id": "12345", "status": "ok"}, metric_labels=["status"]
        ),
    ):
        pass
    assert prom_text(t).label_sets("process_user_count") == [{"status": "ok"}]
    assert t.spans.one(name="process_user").attrs["user_id"] == "12345"


def test_label_value_registered_later_overrides_initial_attr() -> None:
    with (
        capture() as t,
        pyrock.operation("job", attrs={"status": "pending"}, metric_labels=["status"]) as op,
    ):
        op.register(status="done")
    assert t.metrics.value("job_count", status="done") == 1
    assert t.metrics.value("job_count", status="pending") is None


# -- outcomes -------------------------------------------------------------------


def test_operation_succeeds_by_default() -> None:
    with capture() as t, pyrock.operation("quiet") as op:
        assert not op.failed
    span = t.spans.one(name="quiet")
    assert not span.failed
    assert "error" not in span.attrs
    assert t.metrics.value("quiet_successes") == 1
    assert count(t.metrics.value("quiet_failures")) == 0


def test_escaping_exception_marks_failure_and_is_reraised_unchanged() -> None:
    original = ValueError("bad input")
    with (
        capture() as t,
        pytest.raises(ValueError, match="bad input") as excinfo,
        pyrock.operation("boom"),
    ):
        raise original
    assert excinfo.value is original
    span = t.spans.one(name="boom")
    assert span.failed
    assert span.attrs["error"] is True
    assert span.attrs["error.type"] == "builtins.ValueError"
    assert t.metrics.value("boom_failures") == 1
    assert count(t.metrics.value("boom_successes")) == 0


def test_error_message_is_private_by_default() -> None:
    with capture() as t, pytest.raises(ValueError, match="secret"), pyrock.operation("boom"):
        raise ValueError("secret-token-123")
    span = t.spans.one(name="boom")
    assert "error.message" not in span.attrs
    assert all("secret-token-123" not in str(value) for value in span.attrs.values())


def test_error_message_is_kept_when_policy_allows_details() -> None:
    from pyrock import privacy

    with (
        capture(data_policy=privacy.Policy(allow_error_details=True)) as t,
        pytest.raises(ValueError, match="visible"),
        pyrock.operation("boom"),
    ):
        raise ValueError("visible detail")
    assert t.spans.one(name="boom").attrs["error.message"] == "visible detail"


def test_error_type_is_the_qualified_class_name() -> None:
    with capture() as t, pytest.raises(NotFoundError), pyrock.operation("lookup"):
        _raise_not_found()
    assert t.spans.one(name="lookup").attrs["error.type"] == _qualified(NotFoundError)


def test_error_fingerprint_is_stable_across_calls_and_distinguishes_sites() -> None:
    with capture() as t:
        for name, raiser in (("a", _raise_value_error), ("b", _raise_value_error)):
            with pytest.raises(ValueError, match="bad input"), pyrock.operation(name):
                raiser()
        with pytest.raises(NotFoundError), pyrock.operation("c"):
            _raise_not_found()
    first = t.spans.one(name="a").attrs["error.fingerprint"]
    second = t.spans.one(name="b").attrs["error.fingerprint"]
    other = t.spans.one(name="c").attrs["error.fingerprint"]
    assert isinstance(first, str)
    assert first
    assert first == second
    assert first != other


@pytest.mark.parametrize("explicit", [True, False], ids=["cause", "context"])
def test_error_chain_lists_wrapped_exception_types(explicit: bool) -> None:
    with capture() as t, pytest.raises(RuntimeError), pyrock.operation("wrapped"):
        _raise_wrapped(explicit=explicit)
    attrs = t.spans.one(name="wrapped").attrs
    assert attrs["error.type"] == "builtins.RuntimeError"
    assert "builtins.KeyError" in str(attrs["error.chain"])


def test_expected_exception_is_reraised_but_counts_as_success() -> None:
    with (
        capture() as t,
        pytest.raises(KeyError),
        pyrock.operation("cache.get", expected=(KeyError,)),
    ):
        raise KeyError("miss")
    span = t.spans.one(name="cache.get")
    assert not span.failed
    assert "exception" in _event_names(span)
    assert t.metrics.value("cache_get_successes") == 1
    assert count(t.metrics.value("cache_get_failures")) == 0


def test_expected_matches_subclasses() -> None:
    with (
        capture() as t,
        pytest.raises(KeyError),
        pyrock.operation("lookup", expected=(LookupError,)),
    ):
        raise KeyError("miss")
    assert not t.spans.one(name="lookup").failed


def test_unexpected_exception_still_fails_when_expected_is_set() -> None:
    with (
        capture() as t,
        pytest.raises(ValueError, match="other"),
        pyrock.operation("lookup", expected=(KeyError,)),
    ):
        raise ValueError("other")
    assert t.spans.one(name="lookup").failed


def test_fail_with_exception_marks_failure_without_raising() -> None:
    with capture() as t, pyrock.operation("handled") as op:
        op.fail(ValueError("bad user"))
        assert op.failed
    span = t.spans.one(name="handled")
    assert span.failed
    assert span.attrs["error.type"] == "builtins.ValueError"
    assert t.metrics.value("handled_failures") == 1


def test_fail_without_exception_marks_failure() -> None:
    with capture() as t, pyrock.operation("handled") as op:
        op.fail()
    assert t.spans.one(name="handled").failed
    assert t.metrics.value("handled_failures") == 1


def test_succeed_clears_an_earlier_failure() -> None:
    with capture() as t, pyrock.operation("retry_loop") as op:
        op.fail(TimeoutError("attempt 1"))
        op.succeed()
        assert not op.failed
    assert not t.spans.one(name="retry_loop").failed
    assert t.metrics.value("retry_loop_successes") == 1
    assert count(t.metrics.value("retry_loop_failures")) == 0


def test_registering_an_exception_marks_failure() -> None:
    with capture() as t, pyrock.operation("registered") as op:
        op.register(ValueError("x"))
    span = t.spans.one(name="registered")
    assert span.failed
    assert span.attrs["error.type"] == "builtins.ValueError"


def test_end_with_error_marks_failure() -> None:
    with capture() as t:
        op = pyrock.start_operation("manual")
        op.end(error=ConnectionError("down"))
    span = t.spans.one(name="manual")
    assert span.failed
    assert span.attrs["error.type"] == "builtins.ConnectionError"
    assert t.metrics.value("manual_failures") == 1


@pytest.mark.parametrize(("success", "failed"), [(True, False), (False, True)])
def test_end_with_explicit_success_flag(success: bool, failed: bool) -> None:
    with capture() as t:
        op = pyrock.start_operation("manual")
        op.end(success=success)
    assert t.spans.one(name="manual").failed is failed


def test_done_is_idempotent_and_records_once() -> None:
    with capture() as t:
        op = pyrock.start_operation("once")
        op.done()
        op.done()
        op.end(error=ValueError("too late"))
        assert op.ended
    assert len(t.spans.find(name="once")) == 1
    assert t.metrics.value("once_count") == 1
    assert not t.spans.one(name="once").failed


def test_ending_early_inside_with_block_records_once() -> None:
    with capture() as t, pyrock.operation("early") as op:
        op.end(success=True)
    assert len(t.spans.find(name="early")) == 1
    assert t.metrics.value("early_count") == 1


def test_generator_closed_early_is_not_a_failure() -> None:
    with capture() as t:

        @pyrock.operation("gen")
        def numbers() -> Iterator[int]:
            yield 1
            yield 2

        iterator = numbers()
        assert next(iterator) == 1
        iterator.close()  # raises GeneratorExit inside the scope
    assert not t.spans.one(name="gen").failed
    assert t.metrics.value("gen_successes") == 1


def test_system_exit_zero_is_not_a_failure() -> None:
    with capture() as t, pytest.raises(SystemExit), pyrock.operation("exit"):
        raise SystemExit(0)
    assert not t.spans.one(name="exit").failed


def test_keyboard_interrupt_is_a_failure() -> None:
    with capture() as t, pytest.raises(KeyboardInterrupt), pyrock.operation("interrupted"):
        raise KeyboardInterrupt
    span = t.spans.one(name="interrupted")
    assert span.failed
    assert span.attrs["error.type"] == "builtins.KeyboardInterrupt"


def test_cancelled_task_marks_operation_failed() -> None:
    async def main() -> None:
        entered = asyncio.Event()

        async def work() -> None:
            async with pyrock.operation("cancelled"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with capture() as t:
        run(main())
    span = t.spans.one(name="cancelled")
    assert span.failed
    assert span.attrs["error.type"] == _qualified(asyncio.CancelledError)


def test_exception_group_is_a_failure_summarizing_leaves() -> None:
    with capture() as t, pytest.raises(ExceptionGroup), pyrock.operation("batch"):
        raise ExceptionGroup("several", [ValueError("a"), KeyError("b")])
    attrs = t.spans.one(name="batch").attrs
    assert t.spans.one(name="batch").failed
    assert attrs["error.group.count"] == 2
    types = str(attrs["error.types"])
    assert "builtins.ValueError" in types
    assert "builtins.KeyError" in types


def test_exception_caught_by_parent_fails_only_the_child() -> None:
    with (
        capture() as t,
        pyrock.operation("parent"),
        contextlib.suppress(ValueError),
        pyrock.operation("child"),
    ):
        raise ValueError("inner")
    assert t.spans.one(name="child").failed
    assert not t.spans.one(name="parent").failed


# -- nesting and numbering ------------------------------------------------------


def test_nested_operations_share_trace_and_link_parent() -> None:
    with (
        capture() as t,
        pyrock.operation("parent") as parent,
        pyrock.operation("child") as child,
    ):
        assert child.trace_id == parent.trace_id
    parent_span = t.spans.one(name="parent")
    child_span = t.spans.one(name="child")
    assert parent_span.parent_span_id is None
    assert child_span.parent_span_id == parent_span.span_id
    assert child_span.trace_id == parent_span.trace_id
    assert len(parent_span.trace_id) == 32
    assert len(parent_span.span_id) == 16


def test_separate_root_operations_start_separate_traces() -> None:
    with capture() as t:
        with pyrock.operation("first"):
            pass
        with pyrock.operation("second"):
            pass
    assert t.spans.one(name="first").trace_id != t.spans.one(name="second").trace_id


def test_operation_accessors_match_exported_span() -> None:
    with capture() as t, pyrock.operation("accessors") as op:
        assert op.name == "accessors"
        trace_id, span_id = op.trace_id, op.span_id
        assert op.span_context is not None
        assert op.span_context.trace_id_hex == trace_id
    span = t.spans.one(name="accessors")
    assert (span.trace_id, span.span_id) == (trace_id, span_id)


def test_sibling_duplicates_are_numbered_in_span_names_only() -> None:
    with capture() as t, pyrock.operation("parent"):
        names = []
        for _ in range(3):
            with pyrock.operation("fetch") as op:
                names.append((op.name, op.metric_name))
    assert names == [("fetch", "fetch"), ("fetch[1]", "fetch"), ("fetch[2]", "fetch")]
    assert {"fetch", "fetch[1]", "fetch[2]"} <= set(t.spans.names())
    exposition = prom_text(t)
    assert exposition.value("fetch_count") == 3
    assert not any("[" in name for name in exposition.names())


def test_numbering_is_per_parent() -> None:
    with capture() as t:
        for parent in ("left", "right"):
            with pyrock.operation(parent), pyrock.operation("fetch"):
                pass
    assert t.spans.names().count("fetch") == 2
    assert "fetch[1]" not in t.spans.names()


def test_span_kind_defaults_to_internal_and_can_be_set() -> None:
    with capture() as t:
        with pyrock.operation("internal"):
            pass
        with pyrock.operation("consume", kind=pyrock.SpanKind.CONSUMER):
            pass
    assert t.spans.one(name="internal").kind == pyrock.SpanKind.INTERNAL
    assert t.spans.one(name="consume").kind == pyrock.SpanKind.CONSUMER


# -- no_trace -----------------------------------------------------------------


def test_no_trace_records_metrics_but_no_span() -> None:
    with capture() as t, pyrock.operation("hot_path", no_trace=True):
        pass
    assert t.spans.find(name="hot_path") == []
    assert t.metrics.value("hot_path_count") == 1


def test_no_trace_is_inherited_by_children() -> None:
    with (
        capture() as t,
        pyrock.operation("hot_path", no_trace=True),
        pyrock.operation("child"),
        pyrock.step("helper"),
    ):
        pass
    assert len(t.spans) == 0
    assert t.metrics.value("child_count") == 1


def test_no_trace_keeps_enclosing_trace_ids_for_logs() -> None:
    with (
        capture() as t,
        pyrock.operation("outer") as outer,
        pyrock.operation("hot_path", no_trace=True) as hot,
    ):
        assert hot.trace_id == outer.trace_id
        pyrock.info("inside hot path")
    record = t.logs.find(msg="inside hot path")
    assert record["trace_id"] == outer.trace_id
    assert record["span_id"] == outer.span_id
    assert t.spans.names() == ["outer"]


# -- decorators -----------------------------------------------------------------


def test_decorated_sync_function_runs_in_an_operation_per_call() -> None:
    with capture() as t:

        @pyrock.operation("db.get_user", metric_labels=["table"], attrs={"table": "users"})
        def get_user(user_id: str) -> str:
            pyrock.register(user_id=user_id)
            return f"user:{user_id}"

        assert get_user("1") == "user:1"
        assert get_user("2") == "user:2"
    assert t.metrics.value("db_get_user_count", table="users") == 2
    assert sorted(span.attrs["user_id"] for span in spans_named(t, "db.get_user")) == ["1", "2"]


def test_decorated_function_exception_marks_failure_and_propagates() -> None:
    with capture() as t:

        @pyrock.operation("explodes")
        def explodes() -> None:
            raise NotFoundError("gone")

        with pytest.raises(NotFoundError):
            explodes()
    assert t.spans.one(name="explodes").attrs["error.type"] == _qualified(NotFoundError)


def test_decorated_coroutine_function() -> None:
    with capture() as t:

        @pyrock.operation("fetch_async")
        async def fetch(n: int) -> int:
            await asyncio.sleep(0)
            pyrock.register(n=n)
            return n * 2

        assert inspect.iscoroutinefunction(fetch)
        assert run(fetch(21)) == 42
    span = t.spans.one(name="fetch_async")
    assert span.attrs["n"] == 21
    assert not span.failed


def test_decorated_generator_spans_the_whole_iteration() -> None:
    with capture() as t:

        @pyrock.operation("stream")
        def stream(n: int) -> Iterator[int]:
            yield from range(n)
            pyrock.register(finished=True)

        assert inspect.isgeneratorfunction(stream)
        assert list(stream(3)) == [0, 1, 2]
    span = t.spans.one(name="stream")
    assert span.attrs["finished"] is True
    assert t.metrics.value("stream_count") == 1


def test_decorated_async_generator_spans_the_whole_iteration() -> None:
    with capture() as t:

        @pyrock.operation("astream")
        async def astream(n: int) -> AsyncIterator[int]:
            for i in range(n):
                await asyncio.sleep(0)
                yield i
            pyrock.register(finished=True)

        async def consume() -> list[int]:
            return [i async for i in astream(3)]

        assert inspect.isasyncgenfunction(astream)
        assert run(consume()) == [0, 1, 2]
    assert t.spans.one(name="astream").attrs["finished"] is True


def test_decorated_generator_failure_mid_iteration() -> None:
    with capture() as t:

        @pyrock.operation("bad_stream")
        def bad_stream() -> Iterator[int]:
            yield 1
            raise ValueError("broken")

        with pytest.raises(ValueError, match="broken"):
            list(bad_stream())
    assert t.spans.one(name="bad_stream").failed


def _documented(x: int, *, y: str = "a") -> str:
    """Original docstring."""
    return f"{x}{y}"


async def _documented_async(x: int) -> int:
    """Async docstring."""
    return x


@pytest.mark.parametrize("original", [_documented, _documented_async], ids=["sync", "async"])
def test_decorator_preserves_function_metadata(original: object) -> None:
    with capture():
        wrapped = pyrock.operation("meta")(original)  # type: ignore[type-var]
    assert wrapped.__name__ == original.__name__  # type: ignore[attr-defined]
    assert wrapped.__qualname__ == original.__qualname__  # type: ignore[attr-defined]
    assert wrapped.__doc__ == original.__doc__
    assert wrapped.__module__ == original.__module__  # type: ignore[attr-defined]
    assert inspect.signature(wrapped) == inspect.signature(original)  # type: ignore[arg-type]
    if wrapped is not original:
        assert getattr(wrapped, "__wrapped__", None) is original


# -- register / event ---------------------------------------------------------


def test_register_accepts_kwargs_mappings_and_events() -> None:
    with capture() as t, pyrock.operation("reg") as op:
        op.register(status="active", count=3)
        op.register({"user.tier": "gold"})
        op.register(pyrock.Event("retry", attempt=2))
    span = t.spans.one(name="reg")
    assert span.attrs["status"] == "active"
    assert span.attrs["count"] == 3
    assert span.attrs["user.tier"] == "gold"
    retry = next(event for event in span.events if event.name == "retry")
    assert retry.attrs["attempt"] == 2
    assert "retry" not in span.attrs


def test_module_register_targets_the_current_operation() -> None:
    with capture() as t, pyrock.operation("outer"):
        with pyrock.operation("inner"):
            pyrock.register(where="inner")
        pyrock.register(where="outer")
    assert t.spans.one(name="inner").attrs["where"] == "inner"
    assert t.spans.one(name="outer").attrs["where"] == "outer"


def test_event_is_recorded_on_span_not_in_attrs() -> None:
    with capture() as t, pyrock.operation("evented") as op:
        op.event("cache_miss", key="u:123")
        op.event("dotted", {"cache.layer": "l2"})
    span = t.spans.one(name="evented")
    events = {event.name: event for event in span.events}
    assert events["cache_miss"].attrs["key"] == "u:123"
    assert events["dotted"].attrs["cache.layer"] == "l2"
    assert "key" not in span.attrs
    assert events["cache_miss"].timestamp_ns >= span.start_time_ns


def test_attribute_values_keep_their_types() -> None:
    with capture() as t, pyrock.operation("typed") as op:
        op.register(
            s="text",
            i=7,
            f=1.5,
            b=True,
            tags=["a", "b"],
            elapsed=datetime.timedelta(seconds=2),
        )
    attrs = t.spans.one(name="typed").attrs
    assert attrs["s"] == "text"
    assert attrs["i"] == 7
    assert attrs["f"] == 1.5
    assert attrs["b"] is True
    assert list(attrs["tags"]) == ["a", "b"]  # type: ignore[arg-type]
    assert "elapsed" in attrs


def test_unsupported_attribute_values_are_dropped_not_stringified() -> None:
    class Secretive:
        def __repr__(self) -> str:
            return "SECRET-REPR"

        __str__ = __repr__

    with capture() as t, pyrock.operation("dropping") as op:
        op.register({"obj": Secretive()})  # type: ignore[dict-item]
        op.register(kept="yes")
    attrs = t.spans.one(name="dropping").attrs
    assert "obj" not in attrs
    assert attrs["kept"] == "yes"
    assert all("SECRET-REPR" not in str(value) for value in attrs.values())


# -- remote parents and manual lifecycle ----------------------------------------


def test_remote_parent_continues_the_trace() -> None:
    remote = pyrock.SpanContext.from_hex(
        "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", is_remote=True
    )
    with (
        capture() as t,
        pyrock.operation("consume", remote_parent=remote, kind=pyrock.SpanKind.CONSUMER),
        pyrock.operation("handle"),
    ):
        pass
    consume = t.spans.one(name="consume")
    assert consume.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert consume.parent_span_id == "00f067aa0ba902b7"
    assert t.spans.one(name="handle").parent_span_id == consume.span_id


def test_start_operation_is_not_current_until_activated() -> None:
    with capture() as t:
        op = pyrock.start_operation("upload", attrs={"size": 10})
        assert pyrock.current_operation() is None
        with pyrock.operation("unrelated"):
            pass
        token = op.activate()
        assert pyrock.current_operation() is op
        with pyrock.operation("chunk"):
            pass
        op.deactivate(token)
        assert pyrock.current_operation() is None
        op.end()
    upload = t.spans.one(name="upload")
    assert upload.attrs["size"] == 10
    assert t.spans.one(name="chunk").parent_span_id == upload.span_id
    assert t.spans.one(name="unrelated").parent_span_id is None


def test_operation_scope_can_be_reused_and_nested() -> None:
    with capture() as t:
        scope = pyrock.operation("reusable")
        with scope:
            pass
        with scope as outer, scope as inner:
            assert outer.span_id != inner.span_id
    assert t.metrics.value("reusable_count") == 3


# -- real export path -------------------------------------------------------------


@isolated
def test_operations_reach_otlp_collector_with_status_and_error_attrs(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    with (
        pyrock.init(**quiet(service="ops-e2e", trace_url=otlp_collector.traces_url)),
        pyrock.operation("parent", attrs={"user_id": "u1"}),
        contextlib.suppress(ValueError),
        pyrock.operation("child"),
    ):
        raise ValueError("nope")
    otlp_collector.wait_for_spans(2)
    parent = otlp_collector.span("parent")
    child = otlp_collector.span("child")
    assert parent["resource"]["service.name"] == "ops-e2e"
    assert parent["kind"] == 1
    assert parent["parent_span_id"] is None
    assert parent["attributes"]["user_id"] == "u1"
    assert parent["status_code"] != 2
    assert child["parent_span_id"] == parent["span_id"]
    assert child["trace_id"] == parent["trace_id"]
    assert child["status_code"] == 2
    assert child["attributes"]["error"] is True
    assert child["attributes"]["error.type"] == "builtins.ValueError"
    assert child["end_time_unix_nano"] >= child["start_time_unix_nano"]


@isolated
def test_remote_parent_and_events_reach_otlp_collector(
    clean_env: pytest.MonkeyPatch, otlp_collector: OTLPHTTPCollector
) -> None:
    remote = pyrock.SpanContext.from_hex(
        "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331", is_remote=True
    )
    with (
        pyrock.init(**quiet(trace_url=otlp_collector.traces_url)),
        pyrock.operation("consume", remote_parent=remote, kind=pyrock.SpanKind.CONSUMER) as op,
    ):
        op.event("cache_miss", key="u:1")
    span = otlp_collector.wait_for_spans(1)[0]
    assert span["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert span["parent_span_id"] == "b7ad6b7169203331"
    assert span["kind"] == 5
    assert [event["name"] for event in span["events"]] == ["cache_miss"]
    assert span["events"][0]["attributes"] == {"key": "u:1"}
