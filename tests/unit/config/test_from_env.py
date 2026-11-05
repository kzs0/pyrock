"""``Config.from_env``: every ``BEDROCK_*`` variable, empty = unset, aggregation."""

from __future__ import annotations

import os
import types
from typing import Any

import pytest

from pyrock.config import ENV_VARS, Config, ConfigError

# (variable, raw value, field, parsed value)
CASES: list[tuple[str, str, str, Any]] = [
    ("BEDROCK_SERVICE", "users-api", "service", "users-api"),
    (
        "BEDROCK_TRACE_URL",
        "http://jaeger:4318/v1/traces",
        "trace_url",
        "http://jaeger:4318/v1/traces",
    ),
    ("BEDROCK_TRACE_PROTOCOL", "grpc", "trace_protocol", "grpc"),
    ("BEDROCK_TRACE_PROTOCOL", "http", "trace_protocol", "http"),
    ("BEDROCK_TRACE_INSECURE", "true", "trace_insecure", True),
    ("BEDROCK_TRACE_SAMPLE_RATE", "0.25", "trace_sample_rate", 0.25),
    ("BEDROCK_TRACE_SAMPLE_RATE", "0", "trace_sample_rate", 0.0),
    ("BEDROCK_LOG_FORMAT", "text", "log_format", "text"),
    ("BEDROCK_LOG_ADD_SOURCE", "1", "log_add_source", True),
    ("BEDROCK_LOG_CANONICAL", "yes", "log_canonical", True),
    ("BEDROCK_LOG_INSTALL", "pyrock", "log_install", "pyrock"),
    ("BEDROCK_LOG_INSTALL", "none", "log_install", "none"),
    ("BEDROCK_METRIC_PREFIX", "myapp", "metric_prefix", "myapp"),
    ("BEDROCK_METRIC_BUCKETS", "1,5,10", "metric_buckets", (1.0, 5.0, 10.0)),
    ("BEDROCK_METRIC_BUCKETS", " 0.5 , 2.5 ", "metric_buckets", (0.5, 2.5)),
    ("BEDROCK_METRIC_SERIES_LIMIT", "50", "metric_series_limit", 50),
    ("BEDROCK_METRIC_FAMILY_LIMIT", "60", "metric_family_limit", 60),
    ("BEDROCK_RUNTIME_METRICS", "false", "runtime_metrics", False),
    ("BEDROCK_SERVER_ENABLED", "off", "server_enabled", False),
    ("BEDROCK_SERVER_ADDR", "127.0.0.1:9191", "server_addr", "127.0.0.1:9191"),
    ("BEDROCK_SERVER_METRICS", "0", "server_metrics", False),
    ("BEDROCK_SERVER_PPROF", "on", "server_pprof", True),
    ("BEDROCK_SERVER_READ_TIMEOUT", "11s", "server_read_timeout", 11.0),
    ("BEDROCK_SERVER_READ_HEADER_TIMEOUT", "500ms", "server_read_header_timeout", 0.5),
    ("BEDROCK_SERVER_WRITE_TIMEOUT", "1m30s", "server_write_timeout", 90.0),
    ("BEDROCK_SERVER_IDLE_TIMEOUT", "2m", "server_idle_timeout", 120.0),
    ("BEDROCK_SERVER_IDLE_TIMEOUT", "1h2m3.5s", "server_idle_timeout", 3723.5),
    ("BEDROCK_SERVER_MAX_HEADER_BYTES", "4096", "server_max_header_bytes", 4096),
    ("BEDROCK_SERVER_MAX_CONNECTIONS", "8", "server_max_connections", 8),
    ("BEDROCK_DISABLE_ENVIRONMENT_DETECTION", "true", "disable_environment_detection", True),
    ("BEDROCK_SHUTDOWN_TIMEOUT", "45", "shutdown_timeout", 45.0),
    ("BEDROCK_SHUTDOWN_TIMEOUT", "2.5", "shutdown_timeout", 2.5),
]

BAD: list[tuple[str, str]] = [
    ("BEDROCK_TRACE_PROTOCOL", "carrier-pigeon"),
    ("BEDROCK_TRACE_INSECURE", "maybe"),
    ("BEDROCK_TRACE_SAMPLE_RATE", "lots"),
    ("BEDROCK_TRACE_SAMPLE_RATE", "1.5"),
    ("BEDROCK_TRACE_SAMPLE_RATE", "-0.1"),
    ("BEDROCK_LOG_LEVEL", "loud"),
    ("BEDROCK_LOG_FORMAT", "xml"),
    ("BEDROCK_LOG_ADD_SOURCE", "2"),
    ("BEDROCK_LOG_CANONICAL", "sometimes"),
    ("BEDROCK_LOG_INSTALL", "everything"),
    ("BEDROCK_METRIC_BUCKETS", "1,x,3"),
    ("BEDROCK_METRIC_BUCKETS", "5,1"),
    ("BEDROCK_METRIC_BUCKETS", "1,1"),
    ("BEDROCK_METRIC_SERIES_LIMIT", "0"),
    ("BEDROCK_METRIC_SERIES_LIMIT", "many"),
    ("BEDROCK_METRIC_FAMILY_LIMIT", "-1"),
    ("BEDROCK_RUNTIME_METRICS", "nah"),
    ("BEDROCK_SERVER_ENABLED", "maybe"),
    ("BEDROCK_SERVER_METRICS", "x"),
    ("BEDROCK_SERVER_PPROF", "x"),
    ("BEDROCK_SERVER_READ_TIMEOUT", "forever"),
    ("BEDROCK_SERVER_READ_TIMEOUT", "-1s"),
    ("BEDROCK_SERVER_READ_HEADER_TIMEOUT", "0"),
    ("BEDROCK_SERVER_WRITE_TIMEOUT", "0s"),
    ("BEDROCK_SERVER_IDLE_TIMEOUT", "a while"),
    ("BEDROCK_SERVER_MAX_HEADER_BYTES", "big"),
    ("BEDROCK_SERVER_MAX_HEADER_BYTES", "1.5"),
    ("BEDROCK_SERVER_MAX_CONNECTIONS", "0"),
    ("BEDROCK_SERVER_MAX_CONNECTIONS", "lots"),
    ("BEDROCK_DISABLE_ENVIRONMENT_DETECTION", "kinda"),
    ("BEDROCK_SHUTDOWN_TIMEOUT", "-5s"),
    ("BEDROCK_SHUTDOWN_TIMEOUT", "soon"),
]


@pytest.mark.parametrize(
    ("var", "raw", "name", "expected"), CASES, ids=[f"{c[0]}={c[1]}" for c in CASES]
)
def test_each_variable_sets_its_field(var: str, raw: str, name: str, expected: Any) -> None:
    cfg = Config.from_env({var: raw})
    value = getattr(cfg, name)
    assert value == expected
    if isinstance(expected, float):
        assert isinstance(value, float)
    assert cfg == Config.default().replace(**{name: value})


@pytest.mark.parametrize("raw", ["debug", "DEBUG", "info", "warn", "WARNING", "error"])
def test_log_level_is_case_insensitive(raw: str) -> None:
    assert Config.from_env({"BEDROCK_LOG_LEVEL": raw}).log_level.upper() == raw.upper()


def test_every_env_var_is_exercised() -> None:
    assert {c[0] for c in CASES} | {"BEDROCK_LOG_LEVEL"} == set(ENV_VARS.values())


@pytest.mark.parametrize(("var", "raw"), BAD, ids=[f"{b[0]}={b[1]}" for b in BAD])
def test_malformed_variable_is_reported_by_name(var: str, raw: str) -> None:
    """Syntax errors name the variable; range errors name the variable or the field."""
    field_name = {v: k for k, v in ENV_VARS.items()}[var]
    with pytest.raises(ConfigError) as info:
        Config.from_env({var: raw})
    problems = info.value.problems
    assert len(problems) == 1, problems
    assert var in problems[0] or field_name in problems[0], problems


def test_unset_and_unrelated_vars_keep_defaults() -> None:
    assert Config.from_env({}) == Config.default()
    assert Config.from_env({"PATH": "/bin", "BEDROCK_UNKNOWN": "x"}) == Config.default()


@pytest.mark.parametrize("var", sorted(set(ENV_VARS.values())))
def test_empty_value_is_unset(var: str) -> None:
    assert Config.from_env({var: ""}) == Config.default()


def test_all_empty_is_default() -> None:
    assert Config.from_env(dict.fromkeys(ENV_VARS.values(), "")) == Config.default()


def test_errors_are_aggregated_across_variables() -> None:
    environ = {
        "BEDROCK_TRACE_SAMPLE_RATE": "lots",
        "BEDROCK_SERVER_READ_TIMEOUT": "forever",
        "BEDROCK_SERVER_ENABLED": "maybe",
        "BEDROCK_SERVER_MAX_HEADER_BYTES": "big",
        "BEDROCK_LOG_ADD_SOURCE": "2",
        "BEDROCK_SERVICE": "fine",
    }
    with pytest.raises(ConfigError) as info:
        Config.from_env(environ)
    problems = info.value.problems
    joined = " ".join(problems)
    bad = [k for k in environ if k != "BEDROCK_SERVICE"]
    assert len(problems) >= len(bad)
    for var in bad:
        assert var in joined
    assert "BEDROCK_SERVICE" not in joined


def test_everything_at_once() -> None:
    environ = {var: raw for var, raw, _, _ in CASES}
    environ["BEDROCK_LOG_LEVEL"] = "warn"
    cfg = Config.from_env(environ)
    # Later duplicates in CASES win, as they would in a dict.
    expected = {name: value for var, raw, name, value in CASES if environ[var] == raw}
    for name, value in expected.items():
        assert getattr(cfg, name) == value


def test_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BEDROCK_SERVICE", "from-os-environ")
    monkeypatch.setenv("BEDROCK_SERVER_IDLE_TIMEOUT", "3s")
    cfg = Config.from_env()
    assert cfg.service == "from-os-environ"
    assert cfg.server_idle_timeout == 3.0


def test_explicit_mapping_ignores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_SERVICE", "from-os-environ")
    assert Config.from_env({}).service == "unknown"


def test_read_only_mapping_not_mutated() -> None:
    source = {"BEDROCK_SERVICE": "ro"}
    cfg = Config.from_env(types.MappingProxyType(source))
    assert cfg.service == "ro"
    assert source == {"BEDROCK_SERVICE": "ro"}


def test_os_environ_not_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_SERVICE", "x")
    before = dict(os.environ)
    Config.from_env()
    assert dict(os.environ) == before


def test_from_env_result_is_a_validated_config() -> None:
    cfg = Config.from_env({"BEDROCK_SERVER_WRITE_TIMEOUT": "250ms"})
    assert isinstance(cfg, Config)
    assert cfg.problems() == []
    assert cfg.server_write_timeout == 0.25
