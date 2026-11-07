"""Metrics end to end: Prometheus exposition text, registry rules, limits, runtime metrics.

Covers DESIGN §10 and the Go exposition format (``%g`` numbers, ``le`` bounds,
``+Inf``). Values are read back from the exposition text, never from internals.
"""

from __future__ import annotations

import sys
import threading

import pytest

from tests.e2e._export_helpers import exposition, family_block, prom, running, scrape_http
from tests.support.prom import parse

pytestmark = pytest.mark.usefixtures("clean_env")


# ------------------------------------------------------------- exposition text


def test_counter_family_text_is_exact() -> None:
    with running() as b:
        b.counter("jobs_total", "Jobs processed", labels=["queue"]).labels(queue="a").inc(3)
        block = family_block(exposition(b), "jobs_total")
    assert block == [
        "# HELP jobs_total Jobs processed",
        "# TYPE jobs_total counter",
        'jobs_total{queue="a"} 3',
    ]


def test_gauge_family_text_is_exact() -> None:
    with running() as b:
        g = b.gauge("queue_depth", "Current depth")
        g.set(42)
        g.inc()
        g.dec()
        g.add(5)
        g.sub(2.5)
        block = family_block(exposition(b), "queue_depth")
    assert block == [
        "# HELP queue_depth Current depth",
        "# TYPE queue_depth gauge",
        "queue_depth 44.5",
    ]


def test_histogram_family_text_is_exact() -> None:
    with running() as b:
        h = b.histogram("latency_ms", "Latency", buckets=[1, 5, 10])
        for value in (0.5, 3, 7, 100):
            h.observe(value)
        block = family_block(exposition(b), "latency_ms")
    assert block == [
        "# HELP latency_ms Latency",
        "# TYPE latency_ms histogram",
        'latency_ms_bucket{le="1"} 1',
        'latency_ms_bucket{le="5"} 2',
        'latency_ms_bucket{le="10"} 3',
        'latency_ms_bucket{le="+Inf"} 4',
        "latency_ms_sum 110.5",
        "latency_ms_count 4",
    ]


def test_histogram_bucket_label_comes_after_declared_labels() -> None:
    with running() as b:
        b.histogram("q_ms", "Q", buckets=[1], labels=["db"]).labels(db="pg").observe(0.5)
        block = family_block(exposition(b), "q_ms")
    assert 'q_ms_bucket{db="pg",le="1"} 1' in block
    assert 'q_ms_bucket{db="pg",le="+Inf"} 1' in block
    assert 'q_ms_sum{db="pg"} 0.5' in block
    assert 'q_ms_count{db="pg"} 1' in block


def test_default_buckets_match_go_formatting() -> None:
    with running() as b:
        b.histogram("default_ms", "Defaults").observe(1)
        text = prom(b)
    bounds = [s.labels["le"] for s in text.find("default_ms_bucket")]
    assert bounds == ["0.5", "1", "2.5", "5", "10", "25", "50", "100", "250", "500", "1000", "+Inf"]


def test_help_text_is_escaped() -> None:
    with running() as b:
        b.counter("escaped_total", "line1\nline2 back\\slash").inc()
        block = family_block(exposition(b), "escaped_total")
    assert block[0] == "# HELP escaped_total line1\\nline2 back\\\\slash"


def test_label_values_are_escaped() -> None:
    raw = 'a"b\\c\nd'
    with running() as b:
        b.counter("esc_label_total", "E", labels=["v"]).labels(v=raw).inc()
        text = exposition(b)
    assert 'esc_label_total{v="a\\"b\\\\c\\nd"} 1' in text.splitlines()
    assert parse(text).value("esc_label_total", v=raw) == 1


def test_families_are_sorted_by_name() -> None:
    with running() as b:
        for name in ("zeta_total", "alpha_total", "mid_total"):
            b.counter(name, "x").inc()
        types = [
            line.split()[2] for line in exposition(b).splitlines() if line.startswith("# TYPE")
        ]
    assert types == sorted(types)


def test_metric_name_dots_become_underscores() -> None:
    with running() as b:
        b.counter("http.requests", "Requests").inc()
        assert prom(b).value("http_requests") == 1


def test_metric_prefix_is_prepended() -> None:
    with running(metric_prefix="myapp") as b:
        b.counter("hits_total", "Hits").inc()
        text = prom(b)
    assert text.value("myapp_hits_total") == 1
    assert not text.has("hits_total")


def test_operation_metrics_use_prefix_and_labels() -> None:
    with running(metric_prefix="myapp") as b:
        with b.operation("process_user", metric_labels=["status"]) as op:
            op.register(status="active")
        text = prom(b)
    assert text.value("myapp_process_user_count", status="active") == 1
    assert text.value("myapp_process_user_successes", status="active") == 1
    assert text.type("myapp_process_user_duration_ms") == "histogram"


def test_metrics_endpoint_serves_exposition() -> None:
    with running(server_enabled=True, server_addr="127.0.0.1:0") as b:
        b.counter("served_total", "Served").inc(2)
        status, headers, data = scrape_http(b)
    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in headers["content-type"]
    assert parse(data).value("served_total") == 2


# ---------------------------------------------------------------- label rules


def test_static_labels_are_added_to_every_series() -> None:
    with running(static_attrs={"env": "prod"}) as b:
        b.counter("static_total", "S", labels=["k"]).labels(k="v").inc()
        with b.operation("op_with_static"):
            pass
        text = prom(b)
    assert text.value("static_total", env="prod", k="v") == 1
    assert text.value("op_with_static_count", env="prod") == 1


def test_dotted_static_label_is_sanitized() -> None:
    with running(static_attrs={"deploy.region": "eu"}) as b:
        b.counter("region_total", "R").inc()
        assert prom(b).label_sets("region_total") == [{"deploy_region": "eu"}]


def test_labels_kwargs_bind_series() -> None:
    with running() as b:
        c = b.counter("api_requests_total", "Total", labels=["endpoint", "method"])
        c.labels(endpoint="/users", method="GET").inc()
        c.labels(endpoint="/users", method="GET").inc()
        c.labels(endpoint="/users", method="POST").inc()
        text = prom(b)
    assert text.value("api_requests_total", endpoint="/users", method="GET") == 2
    assert text.value("api_requests_total", endpoint="/users", method="POST") == 1


def test_labels_mapping_accepts_dotted_keys() -> None:
    with running() as b:
        c = b.counter("dotted_total", "D", labels=["http.method"])
        c.labels({"http.method": "GET"}).inc()
        assert prom(b).label_sets("dotted_total") == [{"http_method": "GET"}]


def test_missing_declared_label_renders_underscore() -> None:
    with running() as b:
        c = b.counter("partial_total", "P", labels=["a", "b"])
        c.labels(a="x").inc()
        c.inc()
        text = prom(b)
    assert text.value("partial_total", a="x", b="_") == 1
    assert text.value("partial_total", a="_", b="_") == 1


def test_non_string_label_values_are_stringified() -> None:
    with running() as b:
        c = b.counter("typed_labels_total", "T", labels=["ok", "code"])
        c.labels(ok=True, code=200).inc()
        assert prom(b).label_sets("typed_labels_total") == [{"ok": "true", "code": "200"}]


def test_undeclared_label_raises_value_error() -> None:
    with running() as b:
        c = b.counter("strict_total", "S", labels=["a"])
        with pytest.raises(ValueError, match="nope"):
            c.labels(nope="x")


def test_counter_rejects_negative_increment() -> None:
    with running() as b:
        c = b.counter("mono_total", "M")
        with pytest.raises(ValueError):  # noqa: PT011 - any ValueError message
            c.inc(-1)


def test_same_schema_returns_existing_metric() -> None:
    with running() as b:
        first = b.counter("shared_total", "Shared", labels=["a"])
        second = b.counter("shared_total", "Shared", labels=["a"])
        first.labels(a="1").inc()
        second.labels(a="1").inc()
        assert prom(b).value("shared_total", a="1") == 2
    assert first is second


def test_label_conflict_raises_metric_conflict_error() -> None:
    from pyrock.metric import MetricConflictError

    with running() as b:
        b.counter("conflict_total", "C", labels=["a"])
        with pytest.raises(MetricConflictError):
            b.counter("conflict_total", "C", labels=["b"])


def test_type_conflict_raises_metric_conflict_error() -> None:
    from pyrock.metric import MetricConflictError

    with running() as b:
        b.counter("kind_clash", "K")
        with pytest.raises(MetricConflictError):
            b.gauge("kind_clash", "K")


def test_metric_conflict_error_is_value_error() -> None:
    from pyrock.metric import MetricConflictError

    assert issubclass(MetricConflictError, ValueError)


# --------------------------------------------------------------------- limits


def test_series_limit_drops_new_series() -> None:
    with running(metric_series_limit=3) as b:
        c = b.counter("limited_total", "L", labels=["user"])
        for i in range(5):
            c.labels(user=f"u{i}").inc()
        text = prom(b)
        dropped = b.metrics.dropped_series()
    assert len(text.label_sets("limited_total")) == 3
    assert dropped == 2


def test_series_limit_keeps_existing_series_counting() -> None:
    with running(metric_series_limit=1) as b:
        c = b.counter("kept_total", "K", labels=["user"])
        c.labels(user="first").inc()
        c.labels(user="second").inc()
        c.labels(user="first").inc()
        assert prom(b).label_sets("kept_total") == [{"user": "first"}]
        assert prom(b).value("kept_total", user="first") == 2


def test_family_limit_raises_on_registration() -> None:
    from pyrock.metric import MetricLimitError

    with running(metric_family_limit=2) as b:
        b.counter("fam_one_total", "1")
        b.counter("fam_two_total", "2")
        with pytest.raises(MetricLimitError):
            b.counter("fam_three_total", "3")


# ---------------------------------------------------------------- histograms


def test_histogram_timer_context_manager_observes_milliseconds() -> None:
    with running() as b:
        h = b.histogram("timed_ms", "T", labels=["db"])
        with h.labels(db="pg").time():
            threading.Event().wait(0.03)
        text = prom(b)
    assert text.value("timed_ms_count", db="pg") == 1
    elapsed_ms = text.value("timed_ms_sum", db="pg")
    assert elapsed_ms is not None
    assert 25 <= elapsed_ms < 5000


def test_histogram_timer_decorator_observes_each_call() -> None:
    with running() as b:
        h = b.histogram("decorated_ms", "D")

        @h.time()
        def work(x: int) -> int:
            return x * 2

        assert work(2) == 4
        assert work(3) == 6
        text = prom(b)
    assert text.value("decorated_ms_count") == 2
    total = text.value("decorated_ms_sum")
    assert total is not None
    assert 0 <= total < 1000


def test_histogram_timer_records_when_block_raises() -> None:
    with running() as b:
        h = b.histogram("raising_ms", "R")
        with pytest.raises(KeyError), h.time():
            raise KeyError("x")
        assert prom(b).value("raising_ms_count") == 1


def test_custom_buckets_are_used() -> None:
    with running() as b:
        b.histogram("custom_ms", "C", buckets=[1, 2]).observe(1.5)
        bounds = [s.labels["le"] for s in prom(b).find("custom_ms_bucket")]
    assert bounds == ["1", "2", "+Inf"]


def test_metric_buckets_config_sets_default_buckets() -> None:
    with running(metric_buckets=(10, 20)) as b:
        b.histogram("cfg_ms", "C").observe(15)
        bounds = [s.labels["le"] for s in prom(b).find("cfg_ms_bucket")]
    assert bounds == ["10", "20", "+Inf"]


def test_metric_buckets_env_sets_default_buckets(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("BEDROCK_METRIC_BUCKETS", "1, 2.5,3")
    with running() as b:
        b.histogram("env_ms", "E").observe(2)
        bounds = [s.labels["le"] for s in prom(b).find("env_ms_bucket")]
    assert bounds == ["1", "2.5", "3", "+Inf"]


def test_metric_buckets_apply_to_operation_duration(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("BEDROCK_METRIC_BUCKETS", "100,200")
    with running() as b:
        with b.operation("bucketed"):
            pass
        bounds = [s.labels["le"] for s in prom(b).find("bucketed_duration_ms_bucket")]
    assert bounds == ["100", "200", "+Inf"]


@pytest.mark.parametrize("raw", ["3,2,1", "1,1", "1,abc", "1,inf"])
def test_invalid_bucket_env_raises_config_error(clean_env: pytest.MonkeyPatch, raw: str) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_METRIC_BUCKETS", raw)
    with pytest.raises(pyrock.ConfigError) as info:
        pyrock.Config.from_env()
    assert any("BEDROCK_METRIC_BUCKETS" in p or "metric_buckets" in p for p in info.value.problems)


def test_invalid_bucket_env_fails_init(clean_env: pytest.MonkeyPatch) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_METRIC_BUCKETS", "5,1")
    with pytest.raises(pyrock.ConfigError):
        pyrock.init(activate=False, server_enabled=False, log_install="none")


def test_empty_bucket_env_keeps_default_buckets(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("BEDROCK_METRIC_BUCKETS", "")
    with running() as b:
        b.histogram("unset_ms", "U").observe(1)
        assert len(prom(b).find("unset_ms_bucket")) == 12


def test_non_increasing_histogram_buckets_raise() -> None:
    with running() as b, pytest.raises(ValueError):  # noqa: PT011 - any ValueError message
        b.histogram("bad_ms", "B", buckets=[2, 1])


def test_invalid_metric_buckets_config_raises() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError):
        pyrock.Config(metric_buckets=(1.0, float("nan")))


# -------------------------------------------------------------- runtime metrics


def test_runtime_metrics_include_python_families() -> None:
    with running(runtime_metrics=True) as b:
        text = prom(b)
    assert text.find("python_gc_collections_total", generation="0")
    assert text.type("python_gc_collections_total") == "counter"
    assert text.has("python_gc_collected_total")
    assert text.has("python_gc_uncollectable_total")
    threads = text.value("python_threads")
    assert threads is not None
    assert threads >= 1


def test_runtime_python_info_labels() -> None:
    with running(runtime_metrics=True) as b:
        info = prom(b).find("python_info")
    assert len(info) == 1
    labels = info[0].labels
    assert labels["implementation"] == sys.implementation.name
    assert labels["version"].startswith(f"{sys.version_info.major}.{sys.version_info.minor}")
    assert labels["gil"] in {"true", "false"}
    assert info[0].value == 1


@pytest.mark.linux_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc metrics")
def test_runtime_process_metrics_on_linux() -> None:
    with running(runtime_metrics=True) as b:
        text = prom(b)
    for name in (
        "process_cpu_seconds_total",
        "process_resident_memory_bytes",
        "process_virtual_memory_bytes",
        "process_open_fds",
        "process_max_fds",
        "process_start_time_seconds",
    ):
        value = text.value(name)
        assert value is not None, name
        assert value > 0, name


def test_runtime_metrics_absent_when_disabled() -> None:
    with running(runtime_metrics=False) as b:
        b.counter("only_total", "O").inc()
        names = prom(b).names()
    assert not any(n.startswith(("python_", "process_")) for n in names)


def test_runtime_metrics_carry_static_labels() -> None:
    with running(runtime_metrics=True, static_attrs={"env": "prod"}) as b:
        assert prom(b).value("python_threads", env="prod") is not None


# --------------------------------------------------------------- concurrency


def test_concurrent_increments_are_exact() -> None:
    threads_n, per_thread = 16, 5000
    with running() as b:
        c = b.counter("racy_total", "R")
        barrier = threading.Barrier(threads_n)

        def hammer() -> None:
            barrier.wait()
            for _ in range(per_thread):
                c.inc()

        workers = [threading.Thread(target=hammer) for _ in range(threads_n)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        assert prom(b).value("racy_total") == threads_n * per_thread


def test_concurrent_labelled_children_are_exact() -> None:
    threads_n, per_thread, shards = 12, 2000, 4
    with running() as b:
        c = b.counter("sharded_total", "S", labels=["shard"])
        h = b.histogram("sharded_ms", "S", buckets=[1], labels=["shard"])
        barrier = threading.Barrier(threads_n)

        def hammer(i: int) -> None:
            barrier.wait()
            for n in range(per_thread):
                shard = str((i + n) % shards)
                c.labels(shard=shard).inc()
                h.labels(shard=shard).observe(0.5)

        workers = [threading.Thread(target=hammer, args=(i,)) for i in range(threads_n)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        text = prom(b)
    expected = threads_n * per_thread / shards
    for shard in map(str, range(shards)):
        assert text.value("sharded_total", shard=shard) == expected
        assert text.value("sharded_ms_count", shard=shard) == expected
        assert text.value("sharded_ms_bucket", shard=shard, le="1") == expected


def test_concurrent_gauge_updates_are_exact() -> None:
    threads_n, per_thread = 8, 3000
    with running() as b:
        g = b.gauge("level", "L")
        barrier = threading.Barrier(threads_n)

        def hammer(i: int) -> None:
            barrier.wait()
            for _ in range(per_thread):
                if i % 2:
                    g.inc()
                else:
                    g.add(2)

        workers = [threading.Thread(target=hammer, args=(i,)) for i in range(threads_n)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        assert prom(b).value("level") == (threads_n // 2) * per_thread * 3


def test_concurrent_operations_count_exactly() -> None:
    threads_n, per_thread = 8, 250
    with running() as b:
        barrier = threading.Barrier(threads_n)

        def hammer() -> None:
            barrier.wait()
            for _ in range(per_thread):
                with b.operation("parallel_op"):
                    pass

        workers = [threading.Thread(target=hammer) for _ in range(threads_n)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        text = prom(b)
    assert text.value("parallel_op_count") == threads_n * per_thread
    assert text.value("parallel_op_duration_ms_count") == threads_n * per_thread
