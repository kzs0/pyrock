"""Profile export settings and outcomes (re-exported by :mod:`pyrock.profile`)."""

from __future__ import annotations

import enum
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pyrock._compat import is_nonempty_str, seconds

__all__ = [
    "DEFAULT_PROFILE_TYPES",
    "MAX_BYTES_LIMIT",
    "MAX_RETRIES",
    "ProfileExportConfig",
    "ProfileExportStats",
    "ProfileType",
]

MAX_RETRIES = 3
"""Upper bound for :attr:`ProfileExportConfig.retries` (as in Go)."""
MAX_BYTES_LIMIT = 32 << 20
"""Upper bound for :attr:`ProfileExportConfig.max_bytes` (32 MiB, as in Go)."""


class ProfileType(enum.StrEnum):
    """Profile kinds. Values match Go's ``ProfileType`` strings (``x-profile-type``)."""

    CPU = "cpu"
    WALL = "wall"
    HEAP = "heap"
    THREADS = "goroutine"
    GOROUTINE = "goroutine"  # alias of THREADS, for parity with Go
    MUTEX = "mutex"
    BLOCK = "block"


DEFAULT_PROFILE_TYPES: tuple[ProfileType, ...] = (
    ProfileType.CPU,
    ProfileType.HEAP,
    ProfileType.THREADS,
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileExportConfig:
    """OTLP profile export settings (Go: ``ProfileExportConfig``).

    ``headers`` are excluded from ``repr`` because they usually carry
    credentials. ``types=()`` disables collection. Redirects are never
    followed. Construction validates and raises ``ValueError`` listing every
    problem.
    """

    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    types: tuple[ProfileType, ...] = DEFAULT_PROFILE_TYPES
    interval: float = 60.0
    cpu_sample_duration: float = 10.0
    """Must be ``> 0`` and ``< interval``."""
    sample_hz: int = 100
    timeout: float = 10.0
    """Budget in seconds for one upload, retries included."""
    max_bytes: int = 8 << 20
    """Largest encoded upload; at most 32 MiB."""
    retries: int = 3
    """Extra attempts after HTTP 429, 5xx or a connection error (0 to 3)."""
    ssl_context: ssl.SSLContext | None = field(default=None, compare=False)
    allow_http: bool = False
    mutex_sample_fraction: int = 0
    """Record 1/N lock contentions (0 disables, like Go)."""
    block_sample_rate: float = 0.0
    """Record waits longer than this many seconds on average (0 disables)."""

    def __post_init__(self) -> None:
        for name in ("interval", "cpu_sample_duration", "timeout", "block_sample_rate"):
            object.__setattr__(self, name, seconds(getattr(self, name)))
        object.__setattr__(self, "headers", dict(self.headers))
        raw_types: Iterable[Any] = self.types
        if isinstance(raw_types, str):
            raise ValueError("types must be a sequence of ProfileType")
        try:
            types = tuple(dict.fromkeys(ProfileType(t) for t in raw_types))
        except ValueError as exc:
            raise ValueError(f"unknown profile type: {exc}") from None
        object.__setattr__(self, "types", types)
        problems = [*self._endpoint_problems(), *self._limit_problems(), *self._header_problems()]
        if problems:
            raise ValueError("invalid ProfileExportConfig: " + "; ".join(problems))

    def _endpoint_problems(self) -> list[str]:
        endpoint: object = self.endpoint
        parts = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if parts is None or not parts.hostname or parts.scheme not in ("http", "https"):
            return ["endpoint must be an absolute http(s) URL"]
        if parts.username is not None or parts.query or parts.fragment:
            return ["endpoint must not carry credentials, a query or a fragment"]
        if parts.scheme == "http" and not self.allow_http:
            return ["endpoint must use https unless allow_http=True"]
        return []

    def _limit_problems(self) -> list[str]:
        problems: list[str] = []
        if self.interval <= 0:
            problems.append("interval must be positive")
        if not 0 < self.cpu_sample_duration < self.interval:
            problems.append("cpu_sample_duration must be > 0 and < interval")
        if not _is_int(self.sample_hz) or not 1 <= self.sample_hz <= 1000:
            problems.append("sample_hz must be within 1..1000")
        if self.timeout <= 0:
            problems.append("timeout must be positive")
        if not _is_int(self.max_bytes) or not 0 < self.max_bytes <= MAX_BYTES_LIMIT:
            problems.append(f"max_bytes must be within 1..{MAX_BYTES_LIMIT}")
        if not _is_int(self.retries) or not 0 <= self.retries <= MAX_RETRIES:
            problems.append(f"retries must be within 0..{MAX_RETRIES}")
        if not _is_int(self.mutex_sample_fraction) or self.mutex_sample_fraction < 0:
            problems.append("mutex_sample_fraction must be >= 0")
        if self.block_sample_rate < 0:
            problems.append("block_sample_rate must be >= 0")
        return problems

    def _header_problems(self) -> list[str]:
        problems: list[str] = []
        for key, value in self.headers.items():
            if not (is_nonempty_str(key) and isinstance(value, str)):
                problems.append("headers must map str to str")
            elif any(c in key + value for c in "\r\n\0"):
                problems.append(f"header {key!r} contains control characters")
        return problems


@dataclass(frozen=True, slots=True)
class ProfileExportStats:
    """Cumulative profiling outcomes (Go: ``ProfileExportStats``)."""

    exported: int = 0
    failed: int = 0
    skipped: int = 0

    def __add__(self, other: ProfileExportStats) -> ProfileExportStats:
        """Sum two stats (``Bedrock.profile_stats`` adds up every exporter)."""
        return ProfileExportStats(
            self.exported + other.exported,
            self.failed + other.failed,
            self.skipped + other.skipped,
        )
