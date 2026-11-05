"""BedrockHandler and CorrelationFilter: trace correlation, statics, levels, thread safety."""

from __future__ import annotations

import io
import json
import logging
import sys
import threading

from pyrock.logs import ATTRS_KEY, BedrockHandler, CorrelationFilter, JSONFormatter
from tests.unit.logs._helpers import (
    SPAN_ID,
    TRACE_ID,
    Capture,
    detached_logger,
    in_span,
    make_record,
)

# ---------------------------------------------------------------- handler


def test_handler_is_a_stream_handler_defaulting_to_stderr() -> None:
    handler = BedrockHandler()
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_handler_default_formatter_writes_json_lines() -> None:
    cap = Capture()
    log = detached_logger("app", BedrockHandler(cap.stream))
    log.info("one")
    log.warning("two", extra={ATTRS_KEY: {"k": "v"}})
    records = cap.records()
    assert [r["msg"] for r in records] == ["one", "two"]
    assert records[1]["level"] == "WARN"
    assert records[1]["k"] == "v"
    assert records[0]["logger"] == "app"


def test_handler_uses_given_formatter() -> None:
    cap = Capture()
    handler = BedrockHandler(cap.stream, formatter=JSONFormatter(service="svc"))
    detached_logger("app", handler).info("hi")
    assert cap.one()["service"] == "svc"


def test_handler_level_filters_records() -> None:
    cap = Capture()
    log = detached_logger("app", BedrockHandler(cap.stream, level=logging.WARNING))
    log.info("dropped")
    log.error("kept")
    assert [r["msg"] for r in cap.records()] == ["kept"]


def test_handler_adds_trace_correlation_inside_a_span() -> None:
    cap = Capture()
    log = detached_logger("app", BedrockHandler(cap.stream))
    in_span(lambda: log.info("inside"))
    log.info("outside")
    inside, outside = cap.records()
    assert (inside["trace_id"], inside["span_id"]) == (TRACE_ID, SPAN_ID)
    assert not outside.get("trace_id")
    assert not outside.get("span_id")


def test_handler_static_attrs() -> None:
    cap = Capture()
    log = detached_logger("app", BedrockHandler(cap.stream, static_attrs={"env": "prod"}))
    log.info("hi")
    assert cap.one()["env"] == "prod"


def test_handler_correlation_is_per_context_across_threads() -> None:
    cap = Capture()
    log = detached_logger("app", BedrockHandler(cap.stream))
    done = threading.Event()

    def other_thread() -> None:
        log.info("thread")
        done.set()

    def in_span_body() -> None:
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        log.info("span")

    in_span(in_span_body)
    assert done.is_set()
    by_msg = {r["msg"]: r for r in cap.records()}
    assert by_msg["span"]["trace_id"] == TRACE_ID
    assert not by_msg["thread"].get("trace_id")


def test_handler_is_thread_safe() -> None:
    stream = io.StringIO()
    log = detached_logger("app", BedrockHandler(stream))
    threads_n, per_thread = 8, 200
    barrier = threading.Barrier(threads_n)

    def worker(i: int) -> None:
        barrier.wait()
        for j in range(per_thread):
            log.info("msg", extra={ATTRS_KEY: {"worker": i, "j": j, "pad": "x" * 64}})

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(previous)
    lines = stream.getvalue().splitlines()
    assert len(lines) == threads_n * per_thread
    seen = {(r["worker"], r["j"]) for r in map(json.loads, lines)}
    assert len(seen) == threads_n * per_thread


def test_handler_does_not_raise_on_unserializable_fields() -> None:
    cap = Capture()
    log = detached_logger("app", BedrockHandler(cap.stream))
    log.info("ok", extra={"obj": object(), ATTRS_KEY: {"set": {1, 2}}})
    assert cap.one()["msg"] == "ok"


# ---------------------------------------------------------------- filter


def test_filter_adds_ids_inside_a_span_and_returns_true() -> None:
    flt = CorrelationFilter()
    record = make_record()
    assert in_span(lambda: flt.filter(record)) is True
    assert record.trace_id == TRACE_ID  # type: ignore[attr-defined]
    assert record.span_id == SPAN_ID  # type: ignore[attr-defined]


def test_filter_sets_empty_ids_outside_a_span() -> None:
    record = make_record()
    assert CorrelationFilter().filter(record) is True
    assert record.trace_id == ""  # type: ignore[attr-defined]
    assert record.span_id == ""  # type: ignore[attr-defined]


def test_filter_adds_static_attrs() -> None:
    record = make_record()
    CorrelationFilter(static_attrs={"env": "prod", "region": "eu"}).filter(record)
    assert record.env == "prod"  # type: ignore[attr-defined]
    assert record.region == "eu"  # type: ignore[attr-defined]


def test_filter_on_app_owned_stdlib_handler() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(trace_id)s|%(span_id)s|%(message)s"))
    handler.addFilter(CorrelationFilter())
    log = detached_logger("owned", handler)
    in_span(lambda: log.info("correlated"))
    log.info("plain")
    assert stream.getvalue().splitlines() == [f"{TRACE_ID}|{SPAN_ID}|correlated", "||plain"]


def test_filter_with_json_formatter_on_plain_handler() -> None:
    cap = Capture()
    handler = logging.StreamHandler(cap.stream)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(CorrelationFilter(static_attrs={"env": "prod"}))
    log = detached_logger("owned", handler)
    in_span(lambda: log.info("hi"))
    record = cap.one()
    assert (record["trace_id"], record["span_id"], record["env"]) == (TRACE_ID, SPAN_ID, "prod")


def test_filter_never_drops_records() -> None:
    flt = CorrelationFilter()
    for level in (logging.DEBUG, logging.INFO, logging.CRITICAL):
        assert flt.filter(make_record(level=level)) is True
