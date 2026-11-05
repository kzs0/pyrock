"""Environment-variable configuration end to end (DESIGN §6).

``BEDROCK_*`` variables keep Go's names and syntax so one ``.env`` file
configures a Go and a Python service; ``pyrock.env.parse`` reads application
dataclasses annotated with ``Env(...)``.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import pytest

import pyrock
from pyrock.env import Env

pytestmark = pytest.mark.usefixtures("clean_env")


def _from_env(environ: dict[str, str]) -> Any:
    import pyrock

    return pyrock.Config.from_env(environ)


# ------------------------------------------------------------ BEDROCK_* table

ENV_CASES: list[tuple[str, str, str, Any]] = [
    ("BEDROCK_SERVICE", "users-api", "service", "users-api"),
    (
        "BEDROCK_TRACE_URL",
        "http://jaeger:4318/v1/traces",
        "trace_url",
        "http://jaeger:4318/v1/traces",
    ),
    ("BEDROCK_TRACE_PROTOCOL", "grpc", "trace_protocol", "grpc"),
    ("BEDROCK_TRACE_INSECURE", "true", "trace_insecure", True),
    ("BEDROCK_TRACE_SAMPLE_RATE", "0.25", "trace_sample_rate", 0.25),
    ("BEDROCK_LOG_LEVEL", "debug", "log_level", "debug"),
    ("BEDROCK_LOG_FORMAT", "text", "log_format", "text"),
    ("BEDROCK_LOG_ADD_SOURCE", "true", "log_add_source", True),
    ("BEDROCK_LOG_CANONICAL", "true", "log_canonical", True),
    ("BEDROCK_LOG_INSTALL", "pyrock", "log_install", "pyrock"),
    ("BEDROCK_METRIC_PREFIX", "myapp", "metric_prefix", "myapp"),
    ("BEDROCK_METRIC_BUCKETS", "1,5,10", "metric_buckets", (1.0, 5.0, 10.0)),
    ("BEDROCK_METRIC_SERIES_LIMIT", "50", "metric_series_limit", 50),
    ("BEDROCK_METRIC_FAMILY_LIMIT", "60", "metric_family_limit", 60),
    ("BEDROCK_RUNTIME_METRICS", "false", "runtime_metrics", False),
    ("BEDROCK_SERVER_ENABLED", "false", "server_enabled", False),
    ("BEDROCK_SERVER_ADDR", "127.0.0.1:9191", "server_addr", "127.0.0.1:9191"),
    ("BEDROCK_SERVER_METRICS", "false", "server_metrics", False),
    ("BEDROCK_SERVER_PPROF", "true", "server_pprof", True),
    ("BEDROCK_SERVER_READ_TIMEOUT", "11s", "server_read_timeout", 11.0),
    ("BEDROCK_SERVER_READ_HEADER_TIMEOUT", "500ms", "server_read_header_timeout", 0.5),
    ("BEDROCK_SERVER_WRITE_TIMEOUT", "1m30s", "server_write_timeout", 90.0),
    ("BEDROCK_SERVER_IDLE_TIMEOUT", "2m", "server_idle_timeout", 120.0),
    ("BEDROCK_SERVER_MAX_HEADER_BYTES", "4096", "server_max_header_bytes", 4096),
    ("BEDROCK_DISABLE_ENVIRONMENT_DETECTION", "true", "disable_environment_detection", True),
    ("BEDROCK_SHUTDOWN_TIMEOUT", "45s", "shutdown_timeout", 45.0),
]


@pytest.mark.parametrize(
    ("var", "raw", "field_name", "expected"), ENV_CASES, ids=[c[0] for c in ENV_CASES]
)
def test_env_var_is_parsed(var: str, raw: str, field_name: str, expected: Any) -> None:
    cfg = _from_env({var: raw})
    value = getattr(cfg, field_name)
    if field_name == "log_level":
        assert value.upper() == expected.upper()
    else:
        assert value == expected


def test_every_go_variable_is_covered() -> None:
    go_vars = {
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
    assert go_vars <= {case[0] for case in ENV_CASES}


def test_unset_vars_keep_defaults() -> None:
    import pyrock

    assert _from_env({}) == pyrock.Config.default()


def test_empty_var_is_treated_as_unset() -> None:
    import pyrock

    assert _from_env({"BEDROCK_SERVICE": "", "BEDROCK_SERVER_READ_TIMEOUT": ""}) == (
        pyrock.Config.default()
    )


def test_unrelated_vars_are_ignored() -> None:
    import pyrock

    assert _from_env({"BEDROCK_UNKNOWN_THING": "x", "PATH": "/bin"}) == pyrock.Config.default()


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("10s", 10.0),
        ("500ms", 0.5),
        ("1m30s", 90.0),
        ("1h", 3600.0),
        ("1.5s", 1.5),
        ("250us", 0.00025),
        ("2", 2.0),
        ("0.75", 0.75),
    ],
)
def test_duration_syntax(raw: str, seconds: float) -> None:
    assert _from_env({"BEDROCK_SHUTDOWN_TIMEOUT": raw}).shutdown_timeout == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_bool_syntax(raw: str, expected: bool) -> None:
    assert _from_env({"BEDROCK_SERVER_ENABLED": raw}).server_enabled is expected


def test_invalid_values_are_aggregated() -> None:
    import pyrock

    environ = {
        "BEDROCK_TRACE_SAMPLE_RATE": "lots",
        "BEDROCK_SERVER_READ_TIMEOUT": "forever",
        "BEDROCK_SERVER_ENABLED": "maybe",
        "BEDROCK_SERVER_MAX_HEADER_BYTES": "big",
    }
    with pytest.raises(pyrock.ConfigError) as info:
        _from_env(environ)
    problems = info.value.problems
    assert len(problems) >= 4
    joined = " ".join(problems)
    for var in environ:
        assert var in joined


def test_out_of_range_sample_rate_is_rejected() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError):
        _from_env({"BEDROCK_TRACE_SAMPLE_RATE": "1.5"})


def test_negative_duration_is_rejected() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError):
        _from_env({"BEDROCK_SHUTDOWN_TIMEOUT": "-5s"})


def test_unknown_log_level_is_rejected() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError):
        _from_env({"BEDROCK_LOG_LEVEL": "loud"})


def test_unknown_trace_protocol_is_rejected() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError):
        _from_env({"BEDROCK_TRACE_PROTOCOL": "carrier-pigeon"})


def test_config_error_is_value_error() -> None:
    import pyrock

    assert issubclass(pyrock.ConfigError, ValueError)


def test_from_env_reads_os_environ_by_default(clean_env: pytest.MonkeyPatch) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_SERVICE", "from-os-environ")
    assert pyrock.Config.from_env().service == "from-os-environ"


# ------------------------------------------------------------ init() and env


def test_init_reads_environment(clean_env: pytest.MonkeyPatch) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_SERVICE", "env-service")
    clean_env.setenv("BEDROCK_SERVER_ENABLED", "false")
    b = pyrock.init(activate=False, log_install="none", disable_environment_detection=True)
    try:
        assert b.config.service == "env-service"
        assert b.config.server_enabled is False
    finally:
        b.shutdown(timeout=5)


def test_init_overrides_win_over_environment(clean_env: pytest.MonkeyPatch) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_SERVICE", "env-service")
    b = pyrock.init(
        activate=False,
        service="override",
        server_enabled=False,
        log_install="none",
        disable_environment_detection=True,
    )
    try:
        assert b.config.service == "override"
    finally:
        b.shutdown(timeout=5)


def test_init_with_explicit_config_ignores_environment(clean_env: pytest.MonkeyPatch) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_SERVICE", "env-service")
    cfg = pyrock.Config(
        service="explicit",
        server_enabled=False,
        log_install="none",
        disable_environment_detection=True,
    )
    b = pyrock.init(cfg, activate=False)
    try:
        assert b.config.service == "explicit"
    finally:
        b.shutdown(timeout=5)


def test_init_rejects_unknown_override() -> None:
    import pyrock

    with pytest.raises(TypeError):
        pyrock.init(activate=False, server_enabled=False, no_such_option=True)


def test_init_rejects_invalid_override() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError):
        pyrock.init(activate=False, server_enabled=False, trace_sample_rate=7)


def test_init_rejects_malformed_environment(clean_env: pytest.MonkeyPatch) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_SERVER_IDLE_TIMEOUT", "a while")
    with pytest.raises(pyrock.ConfigError):
        pyrock.init(activate=False, server_enabled=False, log_install="none")


def test_disable_detection_env_var_skips_probes(clean_env: pytest.MonkeyPatch) -> None:
    import pyrock

    clean_env.setenv("BEDROCK_DISABLE_ENVIRONMENT_DETECTION", "true")
    b = pyrock.init(activate=False, server_enabled=False, log_install="none")
    try:
        assert "process.pid" not in b.static_attrs
    finally:
        b.shutdown(timeout=5)


# ------------------------------------------------------------ Config.replace


def test_replace_returns_new_validated_copy() -> None:
    import pyrock

    base = pyrock.Config.default()
    changed = base.replace(service="other", metric_prefix="app")
    assert changed.service == "other"
    assert changed.metric_prefix == "app"
    assert base.service == "unknown"


def test_replace_validates() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError):
        pyrock.Config.default().replace(trace_sample_rate=2.0)


def test_replace_rejects_unknown_field() -> None:
    import pyrock

    with pytest.raises(TypeError):
        pyrock.Config.default().replace(bogus=1)


def test_config_lists_every_problem() -> None:
    import pyrock

    with pytest.raises(pyrock.ConfigError) as info:
        pyrock.Config(trace_sample_rate=-1, server_read_timeout=0, log_format="xml")
    assert len(info.value.problems) == 3


def test_config_accepts_timedelta_durations() -> None:
    import pyrock

    cfg = pyrock.Config(server_write_timeout=datetime.timedelta(milliseconds=1500))
    assert cfg.server_write_timeout == 1.5


def test_config_is_frozen() -> None:
    import pyrock

    with pytest.raises(dataclasses.FrozenInstanceError):
        pyrock.Config.default().service = "x"  # type: ignore[misc]


# ------------------------------------------------------------ env.parse


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Database:
    url: Annotated[str, Env("DATABASE_URL", required=True)] = ""
    pool: Annotated[int, Env("DATABASE_POOL")] = 5


@dataclass
class AppConfig:
    port: Annotated[int, Env("PORT")] = 8080
    ratio: Annotated[float, Env("RATIO")] = 0.5
    debug: Annotated[bool, Env("DEBUG")] = False
    name: Annotated[str, Env("APP_NAME")] = "app"
    timeout: Annotated[datetime.timedelta, Env("TIMEOUT")] = datetime.timedelta(seconds=5)
    hosts: Annotated[list[str], Env("ALLOWED_HOSTS", sep=",")] = field(default_factory=list)
    ports: Annotated[tuple[int, ...], Env("PORTS", sep=";")] = ()
    mode: Annotated[Literal["dev", "prod"], Env("MODE")] = "dev"
    color: Annotated[Color, Env("COLOR")] = Color.RED
    region: Annotated[str | None, Env("REGION")] = None
    retries: Annotated[int | None, Env("RETRIES")] = None
    db: Database = field(default_factory=Database)


@dataclass
class Required:
    token: Annotated[str, Env("TOKEN", required=True)] = ""
    secret: Annotated[str, Env("SECRET", required=True)] = ""


@dataclass
class WithBedrock:
    port: Annotated[int, Env("PORT")] = 8080
    bedrock: pyrock.Config = field(default_factory=pyrock.Config.default)


def _parse(cls: type, environ: dict[str, str]) -> Any:
    from pyrock import env

    return env.parse(cls, environ=environ)


def _app(environ: dict[str, str]) -> AppConfig:
    environ = {"DATABASE_URL": "postgres://db", **environ}
    result: AppConfig = _parse(AppConfig, environ)
    return result


def test_parse_defaults_when_unset() -> None:
    cfg = _app({})
    assert cfg.port == 8080
    assert cfg.hosts == []
    assert cfg.region is None
    assert cfg.db.pool == 5


def test_parse_str() -> None:
    assert _app({"APP_NAME": "billing"}).name == "billing"


def test_parse_int() -> None:
    assert _app({"PORT": "9000"}).port == 9000


def test_parse_float() -> None:
    assert _app({"RATIO": "0.125"}).ratio == 0.125


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("Yes", True), ("on", True), ("0", False), ("off", False)],
)
def test_parse_bool(raw: str, expected: bool) -> None:
    assert _app({"DEBUG": raw}).debug is expected


def test_parse_timedelta_go_syntax() -> None:
    assert _app({"TIMEOUT": "1m30s"}).timeout == datetime.timedelta(seconds=90)


def test_parse_list_with_separator_strips_whitespace() -> None:
    assert _app({"ALLOWED_HOSTS": "a.example, b.example ,c"}).hosts == [
        "a.example",
        "b.example",
        "c",
    ]


def test_parse_tuple_of_int_with_custom_separator() -> None:
    assert _app({"PORTS": "80;443"}).ports == (80, 443)


def test_parse_literal() -> None:
    assert _app({"MODE": "prod"}).mode == "prod"


def test_parse_enum_by_value() -> None:
    assert _app({"COLOR": "blue"}).color is Color.BLUE


def test_parse_optional() -> None:
    cfg = _app({"REGION": "eu-west-1", "RETRIES": "3"})
    assert cfg.region == "eu-west-1"
    assert cfg.retries == 3


def test_parse_nested_dataclass() -> None:
    cfg = _app({"DATABASE_URL": "postgres://primary", "DATABASE_POOL": "20"})
    assert cfg.db == Database(url="postgres://primary", pool=20)


def test_parse_nested_bedrock_config() -> None:
    cfg = _parse(WithBedrock, {"BEDROCK_SERVICE": "nested-svc", "PORT": "81"})
    assert cfg.bedrock.service == "nested-svc"
    assert cfg.port == 81


def test_parse_invalid_literal_is_error() -> None:
    from pyrock import env

    with pytest.raises(env.ParseError) as info:
        _app({"MODE": "staging"})
    assert any("MODE" in p for p in info.value.problems)


def test_parse_invalid_enum_is_error() -> None:
    from pyrock import env

    with pytest.raises(env.ParseError):
        _app({"COLOR": "green"})


def test_parse_errors_are_aggregated() -> None:
    from pyrock import env

    with pytest.raises(env.ParseError) as info:
        _app({"PORT": "eighty", "RATIO": "half", "DEBUG": "perhaps"})
    joined = " ".join(info.value.problems)
    assert len(info.value.problems) >= 3
    for var in ("PORT", "RATIO", "DEBUG"):
        assert var in joined


def test_parse_required_missing_lists_all() -> None:
    from pyrock import env

    with pytest.raises(env.ParseError) as info:
        _parse(Required, {})
    joined = " ".join(info.value.problems)
    assert "TOKEN" in joined
    assert "SECRET" in joined


def test_parse_required_empty_is_missing() -> None:
    from pyrock import env

    with pytest.raises(env.ParseError) as info:
        _parse(Required, {"TOKEN": "", "SECRET": "s"})
    assert any("TOKEN" in p for p in info.value.problems)


def test_parse_required_nested_missing() -> None:
    from pyrock import env

    with pytest.raises(env.ParseError) as info:
        _parse(AppConfig, {})
    assert any("DATABASE_URL" in p for p in info.value.problems)


def test_parse_uses_os_environ_by_default(clean_env: pytest.MonkeyPatch) -> None:
    from pyrock import env

    clean_env.setenv("TOKEN", "t")
    clean_env.setenv("SECRET", "s")
    assert env.parse(Required) == Required(token="t", secret="s")


def test_parse_error_is_value_error() -> None:
    from pyrock import env

    assert issubclass(env.ParseError, ValueError)


def test_parse_duration_helper() -> None:
    from pyrock import env

    assert env.parse_duration("1h2m3s") == 3723.0
    assert env.parse_duration("1.5") == 1.5
    with pytest.raises(ValueError):  # noqa: PT011 - any ValueError message
        env.parse_duration("ten seconds")


def test_parse_bool_helper() -> None:
    from pyrock import env

    assert env.parse_bool("ON") is True
    assert env.parse_bool("no") is False
    with pytest.raises(ValueError):  # noqa: PT011 - any ValueError message
        env.parse_bool("perhaps")


def test_env_marker_requires_name() -> None:
    with pytest.raises(ValueError, match="name"):
        Env("")
