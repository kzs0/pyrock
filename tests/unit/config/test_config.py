"""``pyrock.config``: ``Config`` defaults, validation, ``replace`` and ``ENV_VARS``."""

from __future__ import annotations

import dataclasses
import datetime
import io
import math
import threading
from typing import Any

import pytest

from pyrock import privacy
from pyrock.config import ENV_PREFIX, ENV_VARS, Config, ConfigError

DURATION_FIELDS = (
    "server_read_timeout",
    "server_read_header_timeout",
    "server_write_timeout",
    "server_idle_timeout",
    "shutdown_timeout",
)

DEFAULTS: dict[str, Any] = {
    "service": "unknown",
    "trace_url": None,
    "trace_protocol": None,
    "trace_insecure": False,
    "trace_sample_rate": 1.0,
    "trace_sampler": None,
    "log_level": "INFO",
    "log_format": "json",
    "log_output": None,
    "log_add_source": False,
    "log_canonical": False,
    "log_install": "root",
    "metric_prefix": "",
    "metric_buckets": None,
    "metric_series_limit": 1000,
    "metric_family_limit": 1000,
    "runtime_metrics": True,
    "server_enabled": True,
    "server_addr": "127.0.0.1:9090",
    "server_metrics": True,
    "server_pprof": False,
    "server_profile_handler": None,
    "server_read_timeout": 10.0,
    "server_read_header_timeout": 5.0,
    "server_write_timeout": 30.0,
    "server_idle_timeout": 120.0,
    "server_max_header_bytes": 1 << 20,
    "server_max_connections": 64,
    "data_policy": None,
    "disable_environment_detection": False,
    "profiles": None,
    "shutdown_timeout": 30.0,
}

# The Go ``bedrock.Config`` env table (config.go), plus the Python additions.
GO_ENV_VARS = {
    "BEDROCK_SERVICE",
    "BEDROCK_TRACE_URL",
    "BEDROCK_TRACE_PROTOCOL",
    "BEDROCK_TRACE_INSECURE",
    "BEDROCK_TRACE_SAMPLE_RATE",
    "BEDROCK_LOG_LEVEL",
    "BEDROCK_LOG_FORMAT",
    "BEDROCK_LOG_ADD_SOURCE",
    "BEDROCK_LOG_CANONICAL",
    "BEDROCK_METRIC_PREFIX",
    "BEDROCK_METRIC_BUCKETS",
    "BEDROCK_METRIC_SERIES_LIMIT",
    "BEDROCK_METRIC_FAMILY_LIMIT",
    "BEDROCK_RUNTIME_METRICS",
    "BEDROCK_SERVER_ENABLED",
    "BEDROCK_SERVER_ADDR",
    "BEDROCK_SERVER_METRICS",
    "BEDROCK_SERVER_PPROF",
    "BEDROCK_SERVER_READ_TIMEOUT",
    "BEDROCK_SERVER_READ_HEADER_TIMEOUT",
    "BEDROCK_SERVER_WRITE_TIMEOUT",
    "BEDROCK_SERVER_IDLE_TIMEOUT",
    "BEDROCK_SERVER_MAX_HEADER_BYTES",
    "BEDROCK_DISABLE_ENVIRONMENT_DETECTION",
    "BEDROCK_SHUTDOWN_TIMEOUT",
}
PYTHON_ONLY_ENV_VARS = {"BEDROCK_SERVER_MAX_CONNECTIONS", "BEDROCK_LOG_INSTALL"}
# Fields that cannot come from a string variable.
NON_ENV_FIELDS = {
    "trace_sampler",
    "log_output",
    "server_profile_handler",
    "data_policy",
    "profiles",
}


def _problems(**kw: Any) -> list[str]:
    with pytest.raises(ConfigError) as info:
        Config(**kw)
    return info.value.problems


# ----------------------------------------------------------------- defaults


@pytest.mark.parametrize(("name", "expected"), DEFAULTS.items(), ids=list(DEFAULTS))
def test_field_default(name: str, expected: Any) -> None:
    value = getattr(Config(), name)
    assert value == expected
    assert type(value) is type(expected)


def test_defaults_cover_every_field() -> None:
    assert {f.name for f in dataclasses.fields(Config)} == set(DEFAULTS)


def test_default_classmethod_equals_constructor() -> None:
    assert Config.default() == Config()


def test_default_ignores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_SERVICE", "from-env")
    assert Config.default().service == "unknown"


def test_server_addr_default_is_loopback() -> None:
    assert Config().server_addr.startswith("127.0.0.1:")


# ---------------------------------------------------------- frozen / slots


def test_config_is_frozen() -> None:
    cfg = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.service = "x"  # type: ignore[misc]


def test_config_uses_slots() -> None:
    assert "__slots__" in Config.__dict__
    assert not hasattr(Config(), "__dict__")


def test_config_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        Config("svc")  # type: ignore[misc]


def test_config_is_hashable_and_comparable() -> None:
    assert Config(service="a") == Config(service="a")
    assert Config(service="a") != Config(service="b")
    assert hash(Config(service="a")) == hash(Config(service="a"))


def test_non_comparable_fields_do_not_affect_equality() -> None:
    assert Config(log_output=io.StringIO()) == Config()


# ----------------------------------------------------------- durations


@pytest.mark.parametrize("name", DURATION_FIELDS)
def test_timedelta_is_coerced_to_seconds(name: str) -> None:
    cfg = Config(**{name: datetime.timedelta(milliseconds=1500)})
    value = getattr(cfg, name)
    assert type(value) is float
    assert value == 1.5


@pytest.mark.parametrize("name", DURATION_FIELDS)
def test_int_duration_is_accepted(name: str) -> None:
    assert getattr(Config(**{name: 7}), name) == 7


@pytest.mark.parametrize("name", DURATION_FIELDS)
@pytest.mark.parametrize(
    "bad",
    [0, -1, -0.5, datetime.timedelta(0), datetime.timedelta(seconds=-1), math.inf, math.nan],
    ids=["zero", "neg", "negfloat", "td0", "tdneg", "inf", "nan"],
)
def test_duration_must_be_positive_and_finite(name: str, bad: Any) -> None:
    problems = _problems(**{name: bad})
    assert len(problems) == 1
    assert name in problems[0]


@pytest.mark.parametrize("name", DURATION_FIELDS)
def test_duration_rejects_strings_and_bools(name: str) -> None:
    assert _problems(**{name: "10s"})
    assert _problems(**{name: True})


# --------------------------------------------------------------- validation


@pytest.mark.parametrize("rate", [0, 0.0, 0.25, 1, 1.0])
def test_sample_rate_range_accepted(rate: float) -> None:
    assert Config(trace_sample_rate=rate).trace_sample_rate == rate


@pytest.mark.parametrize("rate", [-0.01, 1.01, 7, math.nan, math.inf, True, "0.5", None])
def test_sample_rate_out_of_range(rate: Any) -> None:
    problems = _problems(trace_sample_rate=rate)
    assert len(problems) == 1
    assert "trace_sample_rate" in problems[0]


@pytest.mark.parametrize(
    "level", ["DEBUG", "debug", "INFO", "info", "WARN", "warning", "ERROR", "Error", "CRITICAL"]
)
def test_log_levels_accepted(level: str) -> None:
    assert Config(log_level=level).log_level == level


@pytest.mark.parametrize("level", ["loud", "", "TRACE", 10, None])
def test_log_level_rejected(level: Any) -> None:
    assert any("log_level" in p for p in _problems(log_level=level))


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("log_format", "xml"),
        ("log_install", "everything"),
        ("trace_protocol", "carrier-pigeon"),
        ("service", ""),
        ("service", None),
        ("server_addr", ""),
    ],
)
def test_enumerated_and_string_fields_rejected(name: str, bad: Any) -> None:
    problems = _problems(**{name: bad})
    assert len(problems) == 1
    assert name in problems[0]


@pytest.mark.parametrize("proto", ["grpc", "http", None])
def test_trace_protocol_accepted(proto: Any) -> None:
    assert Config(trace_protocol=proto).trace_protocol == proto


@pytest.mark.parametrize("install", ["root", "pyrock", "none"])
def test_log_install_accepted(install: str) -> None:
    assert Config(log_install=install).log_install == install  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        "metric_series_limit",
        "metric_family_limit",
        "server_max_header_bytes",
        "server_max_connections",
    ],
)
@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10"])
def test_positive_int_fields(name: str, bad: Any) -> None:
    problems = _problems(**{name: bad})
    assert len(problems) == 1
    assert name in problems[0]


def test_buckets_are_normalized_to_tuple() -> None:
    cfg = Config(metric_buckets=[1, 2.5, 10])  # type: ignore[arg-type]
    assert cfg.metric_buckets == (1, 2.5, 10)
    assert isinstance(cfg.metric_buckets, tuple)


@pytest.mark.parametrize(
    "buckets",
    [(), (1.0, 1.0), (2.0, 1.0), (1.0, math.inf), (math.nan,), (1.0, "2")],
    ids=["empty", "dup", "decreasing", "inf", "nan", "str"],
)
def test_bad_buckets(buckets: Any) -> None:
    problems = _problems(metric_buckets=buckets)
    assert len(problems) == 1
    assert "metric_buckets" in problems[0]


def test_data_policy_must_be_a_policy() -> None:
    assert Config(data_policy=privacy.Policy()).data_policy == privacy.Policy()
    assert any("data_policy" in p for p in _problems(data_policy={"attribute_keys": []}))


def test_every_problem_is_listed() -> None:
    problems = _problems(
        service="",
        trace_sample_rate=2,
        log_level="loud",
        log_format="xml",
        metric_series_limit=0,
        server_read_timeout=0,
        shutdown_timeout=-1,
    )
    assert len(problems) == 7


def test_config_error_message_and_type() -> None:
    with pytest.raises(ConfigError) as info:
        Config(trace_sample_rate=2, log_format="xml")
    err = info.value
    assert isinstance(err, ValueError)
    assert "trace_sample_rate" in str(err)
    assert "log_format" in str(err)


def test_problems_method_on_valid_config_is_empty() -> None:
    assert Config().problems() == []


# ------------------------------------------------------------------ replace


def test_replace_returns_new_copy() -> None:
    base = Config()
    out = base.replace(service="svc", metric_prefix="app")
    assert out.service == "svc"
    assert out.metric_prefix == "app"
    assert base == Config()
    assert out is not base


def test_replace_validates() -> None:
    with pytest.raises(ConfigError):
        Config().replace(trace_sample_rate=2.0)


def test_replace_coerces_timedelta() -> None:
    assert Config().replace(shutdown_timeout=datetime.timedelta(seconds=3)).shutdown_timeout == 3.0


def test_replace_rejects_unknown_field() -> None:
    with pytest.raises(TypeError):
        Config().replace(bogus=1)


def test_replace_without_changes_is_equal() -> None:
    assert Config(service="x").replace() == Config(service="x")


# ------------------------------------------------------------------ ENV_VARS


def test_env_vars_cover_go_table_and_python_additions() -> None:
    assert set(ENV_VARS.values()) == GO_ENV_VARS | PYTHON_ONLY_ENV_VARS


def test_env_vars_map_real_fields() -> None:
    field_names = {f.name for f in dataclasses.fields(Config)}
    assert set(ENV_VARS) <= field_names
    assert set(ENV_VARS) == field_names - NON_ENV_FIELDS


def test_env_var_names_follow_the_prefix_convention() -> None:
    assert ENV_PREFIX == "BEDROCK_"
    for name, var in ENV_VARS.items():
        assert var.startswith(ENV_PREFIX)
        if name != "disable_environment_detection":
            assert var == ENV_PREFIX + name.upper()
    assert ENV_VARS["disable_environment_detection"] == "BEDROCK_DISABLE_ENVIRONMENT_DETECTION"


def test_env_var_names_are_unique() -> None:
    assert len(set(ENV_VARS.values())) == len(ENV_VARS)


# ------------------------------------------------------------- concurrency


def test_config_shared_across_threads() -> None:
    cfg = Config(service="shared")
    results: list[Config] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def work() -> None:
        barrier.wait()
        out = cfg.replace(metric_prefix="p")
        with lock:
            results.append(out)

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 16
    assert all(r == Config(service="shared", metric_prefix="p") for r in results)
    assert cfg.metric_prefix == ""
