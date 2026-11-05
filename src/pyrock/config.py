"""Configuration: the frozen :class:`Config` dataclass and :class:`ConfigError`.

Durations are ``float`` seconds and also accept :class:`datetime.timedelta`
(normalized to seconds). Environment variables keep Go's names and syntax
(``BEDROCK_SERVER_READ_TIMEOUT=10s``), so one ``.env`` configures both
languages.

Environment variables read by :meth:`Config.from_env`:

========================================= ==============================
``BEDROCK_SERVICE``                       ``service``
``BEDROCK_TRACE_URL``                     ``trace_url``
``BEDROCK_TRACE_PROTOCOL``                ``trace_protocol`` (grpc/http)
``BEDROCK_TRACE_INSECURE``                ``trace_insecure``
``BEDROCK_TRACE_SAMPLE_RATE``             ``trace_sample_rate``
``BEDROCK_LOG_LEVEL``                     ``log_level``
``BEDROCK_LOG_FORMAT``                    ``log_format``
``BEDROCK_LOG_ADD_SOURCE``                ``log_add_source``
``BEDROCK_LOG_CANONICAL``                 ``log_canonical``
``BEDROCK_LOG_INSTALL``                   ``log_install`` (Python only)
``BEDROCK_METRIC_PREFIX``                 ``metric_prefix``
``BEDROCK_METRIC_BUCKETS``                ``metric_buckets`` (comma-separated)
``BEDROCK_METRIC_SERIES_LIMIT``           ``metric_series_limit``
``BEDROCK_METRIC_FAMILY_LIMIT``           ``metric_family_limit``
``BEDROCK_RUNTIME_METRICS``               ``runtime_metrics``
``BEDROCK_SERVER_ENABLED``                ``server_enabled``
``BEDROCK_SERVER_ADDR``                   ``server_addr``
``BEDROCK_SERVER_METRICS``                ``server_metrics``
``BEDROCK_SERVER_PPROF``                  ``server_pprof``
``BEDROCK_SERVER_READ_TIMEOUT``           ``server_read_timeout``
``BEDROCK_SERVER_READ_HEADER_TIMEOUT``    ``server_read_header_timeout``
``BEDROCK_SERVER_WRITE_TIMEOUT``          ``server_write_timeout``
``BEDROCK_SERVER_IDLE_TIMEOUT``           ``server_idle_timeout``
``BEDROCK_SERVER_MAX_HEADER_BYTES``       ``server_max_header_bytes``
``BEDROCK_SERVER_MAX_CONNECTIONS``        ``server_max_connections`` (Python only)
``BEDROCK_DISABLE_ENVIRONMENT_DETECTION`` ``disable_environment_detection``
``BEDROCK_SHUTDOWN_TIMEOUT``              ``shutdown_timeout``
========================================= ==============================
"""

from __future__ import annotations

import dataclasses
import itertools
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TextIO

from pyrock._compat import (
    is_nonempty_str,
    is_number,
    is_positive_int,
    is_positive_number,
    seconds,
)
from pyrock._types import WSGIApp
from pyrock.env import _parse_float, _parse_int, parse_bool, parse_duration
from pyrock.privacy import Policy

if TYPE_CHECKING:
    from pyrock.profile import ProfileExportConfig
    from pyrock.trace.sampler import Sampler

__all__ = ["ENV_PREFIX", "ENV_VARS", "Config", "ConfigError"]

ENV_PREFIX = "BEDROCK_"

ENV_VARS: Mapping[str, str] = {
    "service": "BEDROCK_SERVICE",
    "trace_url": "BEDROCK_TRACE_URL",
    "trace_protocol": "BEDROCK_TRACE_PROTOCOL",
    "trace_insecure": "BEDROCK_TRACE_INSECURE",
    "trace_sample_rate": "BEDROCK_TRACE_SAMPLE_RATE",
    "log_level": "BEDROCK_LOG_LEVEL",
    "log_format": "BEDROCK_LOG_FORMAT",
    "log_add_source": "BEDROCK_LOG_ADD_SOURCE",
    "log_canonical": "BEDROCK_LOG_CANONICAL",
    "log_install": "BEDROCK_LOG_INSTALL",
    "metric_prefix": "BEDROCK_METRIC_PREFIX",
    "metric_buckets": "BEDROCK_METRIC_BUCKETS",
    "metric_series_limit": "BEDROCK_METRIC_SERIES_LIMIT",
    "metric_family_limit": "BEDROCK_METRIC_FAMILY_LIMIT",
    "runtime_metrics": "BEDROCK_RUNTIME_METRICS",
    "server_enabled": "BEDROCK_SERVER_ENABLED",
    "server_addr": "BEDROCK_SERVER_ADDR",
    "server_metrics": "BEDROCK_SERVER_METRICS",
    "server_pprof": "BEDROCK_SERVER_PPROF",
    "server_read_timeout": "BEDROCK_SERVER_READ_TIMEOUT",
    "server_read_header_timeout": "BEDROCK_SERVER_READ_HEADER_TIMEOUT",
    "server_write_timeout": "BEDROCK_SERVER_WRITE_TIMEOUT",
    "server_idle_timeout": "BEDROCK_SERVER_IDLE_TIMEOUT",
    "server_max_header_bytes": "BEDROCK_SERVER_MAX_HEADER_BYTES",
    "server_max_connections": "BEDROCK_SERVER_MAX_CONNECTIONS",
    "disable_environment_detection": "BEDROCK_DISABLE_ENVIRONMENT_DETECTION",
    "shutdown_timeout": "BEDROCK_SHUTDOWN_TIMEOUT",
}
"""Config field -> environment variable."""

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL"})
_DURATION_FIELDS = (
    "server_read_timeout",
    "server_read_header_timeout",
    "server_write_timeout",
    "server_idle_timeout",
    "shutdown_timeout",
)


def _parse_buckets(raw: str) -> tuple[float, ...]:
    buckets: list[float] = []
    for index, item in enumerate(raw.split(","), start=1):
        try:
            buckets.append(_parse_float(item.strip()))
        except ValueError as exc:
            raise ValueError(f"item {index}: {exc}") from None
    return tuple(buckets)


_BOOL_FIELDS = (
    "trace_insecure",
    "log_add_source",
    "log_canonical",
    "runtime_metrics",
    "server_enabled",
    "server_metrics",
    "server_pprof",
    "disable_environment_detection",
)
_INT_FIELDS = (
    "metric_series_limit",
    "metric_family_limit",
    "server_max_header_bytes",
    "server_max_connections",
)

_ENV_PARSERS: Mapping[str, Callable[[str], Any]] = {
    **dict.fromkeys(_BOOL_FIELDS, parse_bool),
    **dict.fromkeys(_INT_FIELDS, _parse_int),
    **dict.fromkeys(_DURATION_FIELDS, parse_duration),
    "trace_sample_rate": _parse_float,
    "metric_buckets": _parse_buckets,
}
"""Field -> parser for non-string variables; the rest are taken verbatim."""


class ConfigError(ValueError):
    """Invalid configuration. :attr:`problems` lists every problem, not just the first."""

    problems: list[str]

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = list(problems)
        super().__init__("invalid pyrock configuration: " + "; ".join(self.problems))


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """pyrock configuration (Go: ``bedrock.Config``). Construction validates."""

    service: str = "unknown"

    # tracing
    trace_url: str | None = None
    trace_protocol: Literal["grpc", "http"] | None = None
    """``None`` auto-detects: a URL with a scheme or path is HTTP, bare ``host:port`` gRPC."""
    trace_insecure: bool = False
    trace_sample_rate: float = 1.0
    trace_sampler: Sampler | None = field(default=None, compare=False)

    # logging
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_output: TextIO | None = field(default=None, compare=False)
    """Defaults to ``sys.stderr`` (resolved when the handler is built)."""
    log_add_source: bool = False
    log_canonical: bool = False
    log_install: Literal["root", "pyrock", "none"] = "root"

    # metrics
    metric_prefix: str = ""
    metric_buckets: tuple[float, ...] | None = None
    metric_series_limit: int = 1000
    metric_family_limit: int = 1000
    runtime_metrics: bool = True

    # server
    server_enabled: bool = True
    server_addr: str = "127.0.0.1:9090"
    server_metrics: bool = True
    server_pprof: bool = False
    server_profile_handler: WSGIApp | None = field(default=None, compare=False)
    server_read_timeout: float = 10.0
    server_read_header_timeout: float = 5.0
    server_write_timeout: float = 30.0
    server_idle_timeout: float = 120.0
    server_max_header_bytes: int = 1 << 20
    server_max_connections: int = 64
    """Concurrent connection cap (maps to ``server.Config.max_connections``).

    A connection over the cap gets ``503`` and is then closed.
    """

    # privacy / environment / profiles
    data_policy: Policy | None = None
    disable_environment_detection: bool = False
    profiles: ProfileExportConfig | None = None

    shutdown_timeout: float = 30.0

    def __post_init__(self) -> None:
        for name in _DURATION_FIELDS:
            object.__setattr__(self, name, seconds(getattr(self, name)))
        if self.metric_buckets is not None:
            object.__setattr__(self, "metric_buckets", tuple(self.metric_buckets))
        problems = self.problems()
        if problems:
            raise ConfigError(problems)

    def problems(self) -> list[str]:
        """Return every validation problem (empty when valid)."""
        out: list[str] = []
        if not is_nonempty_str(self.service):
            out.append("service must be a non-empty string")
        if self.trace_protocol not in (None, "grpc", "http"):
            out.append("trace_protocol must be 'grpc', 'http' or None")
        if not (is_number(self.trace_sample_rate) and 0.0 <= self.trace_sample_rate <= 1.0):
            out.append("trace_sample_rate must be within [0, 1]")
        level: object = self.log_level
        if not (isinstance(level, str) and level.upper() in _LOG_LEVELS):
            out.append("log_level must be one of debug, info, warn, error")
        if self.log_format not in ("json", "text"):
            out.append("log_format must be 'json' or 'text'")
        if self.log_install not in ("root", "pyrock", "none"):
            out.append("log_install must be 'root', 'pyrock' or 'none'")
        if self.metric_buckets is not None:
            buckets = self.metric_buckets
            if not buckets:
                out.append("metric_buckets must not be empty")
            elif not all(is_number(b) for b in buckets):
                out.append("metric_buckets must be finite numbers")
            elif any(b >= c for b, c in itertools.pairwise(buckets)):
                out.append("metric_buckets must be strictly increasing")
        for name in (
            "metric_series_limit",
            "metric_family_limit",
            "server_max_header_bytes",
            "server_max_connections",
        ):
            if not is_positive_int(getattr(self, name)):
                out.append(f"{name} must be a positive integer")
        for name in _DURATION_FIELDS:
            if not is_positive_number(getattr(self, name)):
                out.append(f"{name} must be a positive duration")
        if not is_nonempty_str(self.server_addr):
            out.append("server_addr must be a non-empty 'host:port' string")
        policy: object = self.data_policy
        if policy is not None and not isinstance(policy, Policy):
            out.append("data_policy must be a pyrock.privacy.Policy")
        return out

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Config:
        """Build a config from ``BEDROCK_*`` variables (unset ones keep defaults).

        Empty variables count as unset. Raises :class:`ConfigError` listing every
        malformed variable (by name, never echoing its value) together with
        every validation problem of the result.
        """
        problems: list[str] = []
        values: dict[str, Any] = {}
        for name, var in ENV_VARS.items():
            raw = environ.get(var, "")
            if not raw:
                continue
            parser = _ENV_PARSERS.get(name)
            if parser is None:
                values[name] = raw
                continue
            try:
                values[name] = parser(raw)
            except ValueError as exc:
                problems.append(f"{var}: {exc}")
        try:
            config = cls(**values)
        except ConfigError as exc:
            problems.extend(exc.problems)
        if problems:
            raise ConfigError(problems)
        return config

    @classmethod
    def default(cls) -> Config:
        """The defaults, ignoring the environment (Go: ``DefaultConfig``)."""
        return cls()

    def replace(self, **changes: Any) -> Config:
        """Return a validated copy with ``changes`` applied (unknown names: ``TypeError``)."""
        return dataclasses.replace(self, **changes)
