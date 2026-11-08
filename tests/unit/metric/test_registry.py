"""Registry contract: get-or-create, conflicts, limits, names, labels, collectors, reset.

DESIGN §10.1; Go ``metric.Registry`` (``registry.go``).
"""

from __future__ import annotations

import pytest

from pyrock.metric import (
    DEFAULT_BUCKETS,
    Counter,
    Gauge,
    Histogram,
    MetricConflictError,
    MetricFamily,
    MetricLimitError,
    MetricType,
    Registry,
    sanitize_name,
)
from tests.unit.metric._helpers import family, sample, samples, value

# ------------------------------------------------------------- data types / constants


def test_default_buckets_match_go() -> None:
    assert DEFAULT_BUCKETS == (0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000)


def test_error_hierarchy() -> None:
    assert issubclass(MetricConflictError, ValueError)
    assert issubclass(MetricLimitError, RuntimeError)
    assert not issubclass(MetricLimitError, NotImplementedError)


def test_metric_type_values() -> None:
    assert [t.value for t in MetricType] == ["counter", "gauge", "histogram"]
    assert str(MetricType.HISTOGRAM) == "histogram"


# ------------------------------------------------------------------ sanitize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http.request", "http_request"),
        ("a.b.c", "a_b_c"),
        ("already_ok", "already_ok"),
        ("ns:sub_total", "ns:sub_total"),
        ("with-dash and space", "with_dash_and_space"),
        ("café", "caf_"),
        ("slash/and|pipe", "slash_and_pipe"),
        ("Mixed09", "Mixed09"),
        ("", ""),
    ],
)
def test_sanitize_name(raw: str, expected: str) -> None:
    assert sanitize_name(raw) == expected


# ------------------------------------------------------------ registry properties


def test_registry_defaults() -> None:
    reg = Registry()
    assert reg.prefix == ""
    assert reg.default_buckets == DEFAULT_BUCKETS
    assert dict(reg.const_labels) == {}
    assert reg.dropped_series() == 0
    assert reg.collect() == []


def test_registry_custom_default_buckets() -> None:
    reg = Registry(buckets=[1, 2, 3])
    assert reg.default_buckets == (1.0, 2.0, 3.0)
    assert reg.histogram("h_ms").buckets == (1.0, 2.0, 3.0)


@pytest.mark.parametrize("bad", [[2, 1], [1, 1], [1, float("nan")], [1, float("inf")]])
def test_registry_rejects_invalid_default_buckets(bad: list[float]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        Registry(buckets=bad)


@pytest.mark.parametrize("kwargs", [{"series_limit": 0}, {"family_limit": 0}, {"series_limit": -1}])
def test_registry_rejects_non_positive_limits(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        Registry(**kwargs)


def test_const_labels_are_stringified_and_sanitized() -> None:
    reg = Registry(const_labels={"env": "prod", "deploy.zone": "a", "shard": 3, "canary": True})
    assert dict(reg.const_labels) == {
        "env": "prod",
        "deploy_zone": "a",
        "shard": "3",
        "canary": "true",
    }


# ------------------------------------------------------------- names and prefix


def test_metric_name_is_sanitized() -> None:
    reg = Registry()
    c = reg.counter("http.requests-total", "R")
    assert c.name == "http_requests_total"
    assert c.help == "R"


def test_prefix_is_prepended_with_underscore() -> None:
    reg = Registry("myapp")
    assert reg.prefix == "myapp"
    assert reg.counter("hits_total").name == "myapp_hits_total"
    assert reg.gauge("depth").name == "myapp_depth"
    assert reg.histogram("lat_ms").name == "myapp_lat_ms"


def test_prefix_with_dots_is_sanitized() -> None:
    reg = Registry("my.app")
    assert reg.counter("op.count").name == "my_app_op_count"


def test_get_by_full_or_unprefixed_name() -> None:
    reg = Registry("myapp")
    c = reg.counter("hits_total")
    assert reg.get("hits_total") is c
    assert reg.get("myapp_hits_total") is c
    assert reg.get("missing") is None


def test_get_returns_each_type() -> None:
    reg = Registry()
    c, g, h = reg.counter("c_total"), reg.gauge("g"), reg.histogram("h_ms")
    assert isinstance(reg.get("c_total"), Counter)
    assert isinstance(reg.get("g"), Gauge)
    assert isinstance(reg.get("h_ms"), Histogram)
    assert (reg.get("c_total"), reg.get("g"), reg.get("h_ms")) == (c, g, h)


# --------------------------------------------------------------- get-or-create


def test_same_schema_returns_existing_metric() -> None:
    reg = Registry()
    first = reg.counter("jobs_total", "Jobs", labels=["queue"])
    second = reg.counter("jobs_total", "Jobs", labels=["queue"])
    assert first is second


def test_same_labels_in_different_order_is_the_same_schema() -> None:
    reg = Registry()
    first = reg.gauge("g", "G", labels=["a", "b"])
    assert reg.gauge("g", "G", labels=["b", "a"]) is first


def test_dotted_and_underscored_names_are_the_same_family() -> None:
    reg = Registry()
    first = reg.counter("http.hits", "H")
    assert reg.counter("http_hits", "H") is first


def test_each_type_is_get_or_create() -> None:
    reg = Registry()
    assert reg.gauge("g") is reg.gauge("g")
    assert reg.histogram("h_ms", buckets=[1, 2]) is reg.histogram("h_ms", buckets=[1, 2])


# ------------------------------------------------------------------ conflicts


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("counter", "gauge"),
        ("counter", "histogram"),
        ("gauge", "counter"),
        ("gauge", "histogram"),
        ("histogram", "counter"),
        ("histogram", "gauge"),
    ],
)
def test_type_conflict_raises(first: str, second: str) -> None:
    reg = Registry()
    getattr(reg, first)("clash", "C")
    with pytest.raises(MetricConflictError):
        getattr(reg, second)("clash", "C")


def test_type_conflict_across_sanitized_names() -> None:
    reg = Registry()
    reg.counter("a.b", "C")
    with pytest.raises(MetricConflictError):
        reg.gauge("a_b", "C")


@pytest.mark.parametrize("labels", [["b"], ["a", "b"], []])
def test_label_conflict_raises(labels: list[str]) -> None:
    reg = Registry()
    reg.counter("conflict_total", "C", labels=["a"])
    with pytest.raises(MetricConflictError):
        reg.counter("conflict_total", "C", labels=labels)


def test_conflict_leaves_original_registration_usable() -> None:
    reg = Registry()
    c = reg.counter("kept_total", "K", labels=["a"])
    with pytest.raises(MetricConflictError):
        reg.gauge("kept_total", "K")
    c.labels(a="x").inc()
    assert reg.get("kept_total") is c
    assert value(reg, "kept_total", {"a": "x"}) == 1


# --------------------------------------------------------------- label names


def test_label_names_are_sanitized() -> None:
    reg = Registry()
    c = reg.counter("req_total", labels=["http.method", "status"])
    assert set(c.label_names) == {"http_method", "status"}


def test_label_names_exclude_const_labels() -> None:
    reg = Registry(const_labels={"env": "prod"})
    c = reg.counter("req_total", labels=["route"])
    assert c.label_names == ("route",)


@pytest.mark.parametrize("label", ["__name", "__reserved__"])
def test_double_underscore_label_names_are_rejected(label: str) -> None:
    reg = Registry()
    with pytest.raises(ValueError):  # noqa: PT011
        reg.counter("bad_total", labels=[label])


def test_le_label_is_rejected_on_histograms() -> None:
    reg = Registry()
    with pytest.raises(ValueError):  # noqa: PT011
        reg.histogram("bad_ms", labels=["le"])


def test_le_label_is_allowed_on_counters() -> None:
    reg = Registry()
    reg.counter("le_total", labels=["le"]).labels(le="x").inc()
    assert value(reg, "le_total", {"le": "x"}) == 1


# ------------------------------------------------------------- static labels


def test_const_labels_are_merged_into_every_series() -> None:
    reg = Registry(const_labels={"env": "prod", "region": "eu"})
    reg.counter("req_total", labels=["route"]).labels(route="/a").inc()
    reg.gauge("depth").set(3)
    assert sample(reg, "req_total").labels == {"env": "prod", "region": "eu", "route": "/a"}
    assert sample(reg, "depth").labels == {"env": "prod", "region": "eu"}


def test_const_labels_on_unlabelled_inc() -> None:
    reg = Registry(const_labels={"env": "prod"})
    reg.counter("req_total", labels=["route"]).inc()
    assert sample(reg, "req_total").labels == {"env": "prod", "route": "_"}


def test_const_label_keys_cannot_be_overridden_per_call() -> None:
    reg = Registry(const_labels={"env": "prod"})
    c = reg.counter("req_total", labels=["route"])
    with pytest.raises(ValueError, match="env"):
        c.labels(route="/a", env="dev")


def test_label_filter_applies_to_label_values() -> None:
    seen: list[tuple[str, str]] = []

    def label_filter(key: str, val: str) -> str:
        seen.append((key, val))
        return "_" if key == "user" else val.upper()

    reg = Registry(label_filter=label_filter)
    reg.counter("req_total", labels=["user", "method"]).labels(user="u1", method="get").inc()
    assert sample(reg, "req_total").labels == {"user": "_", "method": "GET"}
    assert ("user", "u1") in seen


# ------------------------------------------------------------------- limits


def test_series_limit_drops_and_counts_new_series() -> None:
    reg = Registry(series_limit=3)
    c = reg.counter("limited_total", labels=["user"])
    for i in range(5):
        c.labels(user=f"u{i}").inc()
    assert len(samples(reg, "limited_total")) == 3
    assert reg.dropped_series() == 2


def test_series_limit_keeps_existing_series_counting() -> None:
    reg = Registry(series_limit=1)
    c = reg.counter("kept_total", labels=["user"])
    c.labels(user="first").inc()
    c.labels(user="second").inc(5)
    c.labels(user="first").inc()
    assert [s.labels for s in samples(reg, "kept_total")] == [{"user": "first"}]
    assert value(reg, "kept_total") == 2


def test_series_limit_is_per_family() -> None:
    reg = Registry(series_limit=2)
    a = reg.counter("a_total", labels=["k"])
    b = reg.gauge("b", labels=["k"])
    for k in ("1", "2"):
        a.labels(k=k).inc()
        b.labels(k=k).set(1)
    assert reg.dropped_series() == 0
    assert len(samples(reg, "a_total")) == 2
    assert len(samples(reg, "b")) == 2


def test_dropped_series_children_are_silent_noops_for_every_type() -> None:
    reg = Registry(series_limit=1)
    c = reg.counter("c_total", labels=["k"])
    g = reg.gauge("g", labels=["k"])
    h = reg.histogram("h_ms", buckets=[1], labels=["k"])
    c.labels(k="a").inc()
    g.labels(k="a").set(1)
    h.labels(k="a").observe(1)
    c.labels(k="b").inc()
    g.labels(k="b").set(2)
    g.labels(k="c").inc()
    h.labels(k="b").observe(1)
    with h.labels(k="c").time():
        pass
    assert reg.dropped_series() >= 4
    for name in ("c_total", "g", "h_ms"):
        assert [s.labels["k"] for s in samples(reg, name)] == ["a"]


def test_family_limit_raises_on_new_registration() -> None:
    reg = Registry(family_limit=2)
    reg.counter("one_total")
    reg.gauge("two")
    with pytest.raises(MetricLimitError):
        reg.histogram("three_ms")
    with pytest.raises(MetricLimitError):
        reg.counter("four_total")


def test_family_limit_allows_reregistering_existing_names() -> None:
    reg = Registry(family_limit=1)
    first = reg.counter("one_total")
    assert reg.counter("one_total") is first


# --------------------------------------------------------------- collection


def test_collect_returns_families_sorted_by_name() -> None:
    reg = Registry()
    for name in ("zeta_total", "alpha_total", "mid_total"):
        reg.counter(name, "x").inc()
    fams = reg.collect()
    assert [f.name for f in fams] == ["alpha_total", "mid_total", "zeta_total"]
    assert all(isinstance(f, MetricFamily) for f in fams)


def test_collect_reports_type_and_help() -> None:
    reg = Registry()
    reg.counter("c_total", "Counter help").inc()
    reg.gauge("g", "Gauge help").set(1)
    reg.histogram("h_ms", "Histogram help").observe(1)
    by_name = {f.name: f for f in reg.collect()}
    assert (by_name["c_total"].type, by_name["c_total"].help) == (
        MetricType.COUNTER,
        "Counter help",
    )
    assert (by_name["g"].type, by_name["g"].help) == (MetricType.GAUGE, "Gauge help")
    assert (by_name["h_ms"].type, by_name["h_ms"].help) == (MetricType.HISTOGRAM, "Histogram help")


def test_collect_snapshot_is_not_live() -> None:
    reg = Registry()
    c = reg.counter("snap_total")
    c.inc()
    snapshot = reg.collect()
    c.inc(10)
    fam = next(f for f in snapshot if f.name == "snap_total")
    assert [s.value for s in fam.samples] == [1]


class _CountingCollector:
    def __init__(self, registry: Registry) -> None:
        self.calls = 0
        self.gauge = registry.gauge("collected_calls")

    def collect(self) -> None:
        self.calls += 1
        self.gauge.set(self.calls)


def test_collectors_run_before_each_snapshot() -> None:
    reg = Registry()
    collector = _CountingCollector(reg)
    reg.register_collector(collector)
    assert value(reg, "collected_calls") == 1
    assert value(reg, "collected_calls") == 2
    assert collector.calls == 2


def test_unregistered_collector_stops_running() -> None:
    reg = Registry()
    collector = _CountingCollector(reg)
    reg.register_collector(collector)
    reg.collect()
    reg.unregister_collector(collector)
    reg.collect()
    assert collector.calls == 1


def test_family_with_no_series_has_no_samples() -> None:
    reg = Registry()
    reg.counter("unused_total", labels=["a"])
    fam = family(reg, "unused_total")
    assert fam is None or fam.samples == ()


# ------------------------------------------------------------------- reset


def test_reset_zeroes_series_and_keeps_registrations() -> None:
    reg = Registry()
    c = reg.counter("c_total", labels=["k"])
    g = reg.gauge("g")
    h = reg.histogram("h_ms", buckets=[1])
    child = c.labels(k="a")
    child.inc(5)
    g.set(7)
    h.observe(0.5)
    reg.reset()
    assert reg.get("c_total") is c
    assert reg.get("g") is g
    assert reg.get("h_ms") is h
    for s in samples(reg, "c_total") + samples(reg, "g"):
        assert s.value == 0
    for s in samples(reg, "h_ms"):
        assert (s.count, s.sum) == (0, 0)
        assert all(b.count == 0 for b in s.buckets)
    # A child bound before the reset keeps working and counts from zero.
    child.inc()
    assert value(reg, "c_total", {"k": "a"}) == 1
