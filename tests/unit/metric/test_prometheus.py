"""Prometheus text exposition 0.0.4: golden output, escaping, numbers, validation.

DESIGN §10.3; Go ``metric/prometheus/exposition.go`` and its tests. Numbers use
Go's ``%g`` (PLAN decision 28).
"""

from __future__ import annotations

import math

import pytest

from pyrock.metric import Bucket, MetricFamily, MetricType, Registry, Sample
from pyrock.metric.prometheus import (
    CONTENT_TYPE,
    ExpositionError,
    encode,
    escape_help,
    escape_label_value,
    validate,
)
from tests.support.prom import parse


def _gauge(name: str, v: float, labels: dict[str, str] | None = None) -> MetricFamily:
    return MetricFamily(name, "", MetricType.GAUGE, (Sample(labels or {}, value=v),))


def _hist(
    buckets: tuple[Bucket, ...],
    count: int,
    total: float = 0.0,
    *,
    name: str = "h_ms",
    labels: dict[str, str] | None = None,
) -> MetricFamily:
    sample = Sample(labels or {}, buckets=buckets, count=count, sum=total)
    return MetricFamily(name, "H", MetricType.HISTOGRAM, (sample,))


# --------------------------------------------------------------- constants


def test_content_type() -> None:
    assert CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


def test_exposition_error_is_value_error() -> None:
    assert issubclass(ExpositionError, ValueError)


# ------------------------------------------------------------------ golden


def test_golden_registry_output() -> None:
    reg = Registry(const_labels={"env": "prod"})
    jobs = reg.counter("jobs_total", "Jobs processed", labels=["queue"])
    jobs.labels(queue="b").inc(2)
    jobs.labels(queue="a").inc(3)
    reg.gauge("depth", "Depth").set(1.5)
    lat = reg.histogram("lat_ms", "Latency", buckets=[1, 2.5], labels=["route"])
    lat.labels(route="/x").observe(0.5)
    lat.labels(route="/x").observe(3)
    out = encode(reg)
    assert isinstance(out, bytes)
    assert out.decode() == (
        "# HELP depth Depth\n"
        "# TYPE depth gauge\n"
        'depth{env="prod"} 1.5\n'
        "# HELP jobs_total Jobs processed\n"
        "# TYPE jobs_total counter\n"
        'jobs_total{env="prod",queue="a"} 3\n'
        'jobs_total{env="prod",queue="b"} 2\n'
        "# HELP lat_ms Latency\n"
        "# TYPE lat_ms histogram\n"
        'lat_ms_bucket{env="prod",route="/x",le="1"} 1\n'
        'lat_ms_bucket{env="prod",route="/x",le="2.5"} 1\n'
        'lat_ms_bucket{env="prod",route="/x",le="+Inf"} 2\n'
        'lat_ms_sum{env="prod",route="/x"} 3.5\n'
        'lat_ms_count{env="prod",route="/x"} 2\n'
    )
    parsed = parse(out)
    assert parsed.type("lat_ms") == "histogram"
    assert parsed.value("jobs_total", queue="a") == 3


def test_golden_counter_snapshot() -> None:
    fam = MetricFamily(
        "requests_total",
        "Total requests",
        MetricType.COUNTER,
        (Sample({"method": "GET"}, value=42),),
    )
    assert encode([fam]) == (
        b"# HELP requests_total Total requests\n"
        b"# TYPE requests_total counter\n"
        b'requests_total{method="GET"} 42\n'
    )


def test_golden_gauge_without_labels_or_help() -> None:
    assert encode([_gauge("queue_depth", 7)]) == b"# TYPE queue_depth gauge\nqueue_depth 7\n"


def test_golden_histogram_snapshot() -> None:
    fam = _hist(
        (Bucket(10, 3), Bucket(100, 7)), 10, 350.5, name="latency_ms", labels={"route": "/api"}
    )
    assert encode([fam]).decode() == (
        "# HELP latency_ms H\n"
        "# TYPE latency_ms histogram\n"
        'latency_ms_bucket{route="/api",le="10"} 3\n'
        'latency_ms_bucket{route="/api",le="100"} 7\n'
        'latency_ms_bucket{route="/api",le="+Inf"} 10\n'
        'latency_ms_sum{route="/api"} 350.5\n'
        'latency_ms_count{route="/api"} 10\n'
    )


def test_histogram_without_labels_has_only_le() -> None:
    text = encode([_hist((Bucket(1, 1),), 2, 4.5)]).decode().splitlines()
    assert text[2:] == [
        'h_ms_bucket{le="1"} 1',
        'h_ms_bucket{le="+Inf"} 2',
        "h_ms_sum 4.5",
        "h_ms_count 2",
    ]


def test_histogram_with_no_finite_buckets_still_has_inf_bucket() -> None:
    lines = encode([_hist((), 3, 1.0)]).decode().splitlines()
    assert 'h_ms_bucket{le="+Inf"} 3' in lines
    assert "h_ms_count 3" in lines


def test_default_bucket_bounds_are_formatted_like_go() -> None:
    reg = Registry()
    reg.histogram("d_ms").observe(1)
    bounds = [s.labels["le"] for s in parse(encode(reg)).find("d_ms_bucket")]
    assert bounds == ["0.5", "1", "2.5", "5", "10", "25", "50", "100", "250", "500", "1000", "+Inf"]


def test_empty_registry_encodes_to_empty_bytes() -> None:
    assert encode(Registry()) == b""
    assert encode([]) == b""


def test_families_without_samples_are_skipped() -> None:
    empty = MetricFamily("empty_total", "No data", MetricType.COUNTER, ())
    assert encode([empty]) == b""
    assert encode([empty, _gauge("g", 1)]) == b"# TYPE g gauge\ng 1\n"


def test_encode_registry_runs_collectors() -> None:
    reg = Registry()
    g = reg.gauge("fresh")

    class Collector:
        def collect(self) -> None:
            g.set(9)

    reg.register_collector(Collector())
    assert b"fresh 9\n" in encode(reg)


def test_encode_accepts_any_iterable_and_does_not_mutate_input() -> None:
    families = [_gauge("z_metric", 1), _gauge("a_metric", 2)]
    out = encode(iter(families)).decode()
    assert [f.name for f in families] == ["z_metric", "a_metric"]
    assert out.index("a_metric") < out.index("z_metric")


# ---------------------------------------------------------------- sorting


def test_families_are_sorted_by_name() -> None:
    out = encode([_gauge("zzz", 1), _gauge("aaa", 2), _gauge("mmm", 3)]).decode()
    types = [line.split()[2] for line in out.splitlines() if line.startswith("# TYPE")]
    assert types == ["aaa", "mmm", "zzz"]


def test_registry_series_are_sorted_by_label_values() -> None:
    reg = Registry()
    c = reg.counter("s_total", labels=["k"])
    for k in ("c", "a", "b"):
        c.labels(k=k).inc()
    lines = [ln for ln in encode(reg).decode().splitlines() if not ln.startswith("#")]
    assert lines == ['s_total{k="a"} 1', 's_total{k="b"} 1', 's_total{k="c"} 1']


def test_registry_labels_are_sorted_by_name() -> None:
    reg = Registry(const_labels={"middle": "m"})
    reg.counter("o_total", labels=["zeta", "alpha"]).labels(zeta="z", alpha="a").inc()
    assert b'o_total{alpha="a",middle="m",zeta="z"} 1\n' in encode(reg)


def test_output_is_deterministic() -> None:
    reg = Registry()
    c = reg.counter("d_total", labels=["a", "b"])
    for i in range(20):
        c.labels(a=str(i % 3), b=str(i)).inc()
    assert encode(reg) == encode(reg)


# --------------------------------------------------------------- escaping


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("plain", "plain"),
        ("back\\slash", "back\\\\slash"),
        ("line1\nline2", "line1\\nline2"),
        ('keeps "quotes"', 'keeps "quotes"'),
        ("tab\tstays", "tab\tstays"),
        ("café", "café"),
    ],
)
def test_escape_help(raw: str, escaped: str) -> None:
    assert escape_help(raw) == escaped


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("plain", "plain"),
        ("back\\slash", "back\\\\slash"),
        ("line1\nline2", "line1\\nline2"),
        ('a"b', 'a\\"b'),
        ('a"b\\c\nd', 'a\\"b\\\\c\\nd'),
        ("tab\tstays", "tab\tstays"),
        ("", ""),
    ],
)
def test_escape_label_value(raw: str, escaped: str) -> None:
    assert escape_label_value(raw) == escaped


def test_help_is_escaped_in_output() -> None:
    fam = MetricFamily("e_total", "line1\nline2 back\\slash", MetricType.COUNTER, (Sample({}, 1),))
    assert encode([fam]).decode().splitlines()[0] == "# HELP e_total line1\\nline2 back\\\\slash"


def test_label_values_are_escaped_and_round_trip() -> None:
    raw = 'a"b\\c\nd'
    reg = Registry()
    reg.counter("esc_total", labels=["v"]).labels(v=raw).inc()
    text = encode(reg).decode()
    assert 'esc_total{v="a\\"b\\\\c\\nd"} 1' in text.splitlines()
    assert parse(text).value("esc_total", v=raw) == 1


def test_utf8_label_values_are_encoded_as_utf8() -> None:
    out = encode([_gauge("u", 1, {"city": "Zürich"})])
    assert 'u{city="Zürich"} 1\n'.encode() in out


# ---------------------------------------------------------------- numbers


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (0, "0"),
        (1, "1"),
        (-1, "-1"),
        (0.5, "0.5"),
        (3.75, "3.75"),
        (100000, "100000"),
        (123456, "123456"),
        (1e6, "1e+06"),
        (1234567, "1.234567e+06"),
        (12345678, "1.2345678e+07"),
        (2.0**53, "9.007199254740992e+15"),
        (1e21, "1e+21"),
        (1e100, "1e+100"),
        (0.0001, "0.0001"),
        (0.00001, "1e-05"),
        (1.5e-7, "1.5e-07"),
        (-2.5e-10, "-2.5e-10"),
        (5e-324, "5e-324"),
        (0.1 + 0.2, "0.30000000000000004"),
        (math.inf, "+Inf"),
        (-math.inf, "-Inf"),
        (math.nan, "NaN"),
    ],
)
def test_sample_values_use_go_g_formatting(number: float, text: str) -> None:
    assert encode([_gauge("n", number)]).decode().splitlines()[-1] == f"n {text}"


@pytest.mark.parametrize(
    ("bound", "text"), [(1e6, "1e+06"), (0.00001, "1e-05"), (2.5, "2.5"), (250, "250")]
)
def test_bucket_bounds_use_go_g_formatting(bound: float, text: str) -> None:
    lines = encode([_hist((Bucket(bound, 1),), 1, 1.0)]).decode().splitlines()
    assert f'h_ms_bucket{{le="{text}"}} 1' in lines


def test_histogram_sum_and_count_formatting() -> None:
    lines = encode([_hist((Bucket(1, 0),), 2_000_000, 1e7)]).decode().splitlines()
    assert "h_ms_sum 1e+07" in lines
    assert "h_ms_count 2e+06" in lines
    assert 'h_ms_bucket{le="+Inf"} 2e+06' in lines


# -------------------------------------------------------------- validation


INVALID_FAMILIES = {
    "bounds decrease": _hist((Bucket(5, 1), Bucket(1, 1)), 2),
    "bounds repeat": _hist((Bucket(1, 1), Bucket(1, 1)), 2),
    "counts decrease": _hist((Bucket(1, 3), Bucket(2, 2)), 3),
    "bucket above count": _hist((Bucket(1, 5),), 4),
    "nan bound": _hist((Bucket(math.nan, 0),), 1),
    "inf bound": _hist((Bucket(math.inf, 1),), 1),
    "-inf bound": _hist((Bucket(-math.inf, 0), Bucket(1, 0)), 1),
    "histogram le label": _hist((Bucket(1, 0),), 0, labels={"le": "x"}),
    "metric leading digit": _gauge("1metric", 1),
    "metric dash": _gauge("bad-name", 1),
    "metric newline": _gauge("safe\nevil", 1),
    "metric empty": _gauge("", 1),
    "label leading digit": _gauge("g", 1, {"1x": "v"}),
    "label dash": _gauge("g", 1, {"a-b": "v"}),
    "label colon": _gauge("g", 1, {"a:b": "v"}),
    "label reserved": _gauge("g", 1, {"__name__": "v"}),
    "label newline": _gauge("g", 1, {"a\nb": "v"}),
    "label value surrogate": _gauge("g", 1, {"a": "\ud800"}),
    "help surrogate": MetricFamily("g", "bad \udc80", MetricType.GAUGE, (Sample({}, 1),)),
}


@pytest.mark.parametrize("case", sorted(INVALID_FAMILIES))
def test_invalid_snapshots_raise_exposition_error(case: str) -> None:
    fam = INVALID_FAMILIES[case]
    with pytest.raises(ExpositionError):
        validate([fam])
    with pytest.raises(ExpositionError):
        encode([fam])


def test_validation_happens_before_any_output() -> None:
    # A valid family sorted *before* the invalid one must not produce partial text.
    families = [_gauge("aaa_valid", 1), _hist((Bucket(5, 1), Bucket(1, 1)), 2, name="zzz_ms")]
    with pytest.raises(ExpositionError):
        encode(families)


def test_colliding_histogram_series_names_raise() -> None:
    hist = _hist((Bucket(1, 0),), 0, name="x")
    clash = MetricFamily("x_count", "", MetricType.COUNTER, (Sample({}, 1),))
    with pytest.raises(ExpositionError):
        encode([hist, clash])


def test_duplicate_family_names_raise() -> None:
    with pytest.raises(ExpositionError):
        encode([_gauge("dup", 1), _gauge("dup", 2)])


def test_valid_snapshot_validates() -> None:
    validate([_gauge("ok", 1, {"le": "allowed on gauges"}), _hist((Bucket(1, 1),), 1, 1.0)])
    validate([MetricFamily("empty_1x", "", MetricType.GAUGE, ())])


def test_empty_family_is_not_validated() -> None:
    # Go skips families with no samples entirely, including for validation.
    validate([MetricFamily("1bad", "", MetricType.GAUGE, ())])
    assert encode([MetricFamily("1bad", "", MetricType.GAUGE, ())]) == b""


def test_counter_and_gauge_ignore_histogram_fields() -> None:
    fam = MetricFamily(
        "c_total", "", MetricType.COUNTER, (Sample({}, value=2, buckets=(), count=99),)
    )
    assert encode([fam]) == b"# TYPE c_total counter\nc_total 2\n"
