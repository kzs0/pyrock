"""The managed cloud backend (Go: ``WithCloud`` / ``wireCloud``).

.. code-block:: python

    pyrock.init(cloud=pyrock.cloud("brk_live_abc123", push_interval=15.0))

* Traces: OTLP/gRPC with ``x-bedrock-key`` metadata; a larger batch processor.
* :class:`RetryExporter`: 3 attempts with 1 s / 2 s / 4 s back-off, waiting on
  an event so shutdown deadlines are honoured.
* :class:`FanOutExporter`: with a local ``trace_url`` too, every batch goes to
  both; every exporter is always called, the first error is raised.
* Metrics push: Prometheus text to ``{endpoint}/v1/metrics`` with
  ``Authorization: Bearer``; overlapping pushes are skipped; final flush on
  shutdown.
* Profile push: OTLP profiles to ``{endpoint}/v1/profiles``.

``pyrock.cloud`` and ``pyrock.CloudConfig`` are re-exported (lazily) from this
private module. It is never imported by ``import pyrock``; the gRPC stack loads
only when wiring starts.
"""

from __future__ import annotations

import datetime
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrock._compat import is_nonempty_str, seconds
from pyrock.profile import DEFAULT_PROFILE_TYPES, ProfileType
from pyrock.trace import Span, SpanExporter

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock

__all__ = [
    "DEFAULT_ENDPOINT",
    "CloudConfig",
    "CloudWiring",
    "FanOutExporter",
    "RetryExporter",
    "cloud",
    "grpc_endpoint",
    "wire",
]

DEFAULT_ENDPOINT = "https://ingest.bedrock.dev"
API_KEY_HEADER = "x-bedrock-key"


@dataclass(frozen=True, slots=True, kw_only=True)
class CloudConfig:
    """Cloud settings. ``api_key`` is excluded from ``repr`` and equality."""

    api_key: str = field(repr=False, compare=False)
    endpoint: str = DEFAULT_ENDPOINT
    insecure: bool = False
    push_interval: float = 15.0
    profile_interval: float = 60.0
    profile_cpu_sample_duration: float = 10.0
    profiles: tuple[ProfileType, ...] = DEFAULT_PROFILE_TYPES
    """``()`` disables cloud profiling."""

    def __post_init__(self) -> None:
        for name in ("push_interval", "profile_interval", "profile_cpu_sample_duration"):
            object.__setattr__(self, name, seconds(getattr(self, name)))
        raw: Iterable[Any] = self.profiles
        object.__setattr__(self, "profiles", tuple(dict.fromkeys(ProfileType(p) for p in raw)))
        problems: list[str] = []
        if not is_nonempty_str(self.api_key):
            problems.append("api_key must be a non-empty string")
        elif any(c in self.api_key for c in "\r\n\0"):
            problems.append("api_key contains control characters")
        if not self.endpoint:
            problems.append("endpoint must be non-empty")
        if self.push_interval <= 0:
            problems.append("push_interval must be positive")
        if self.profile_interval <= 0:
            problems.append("profile_interval must be positive")
        if not 0 < self.profile_cpu_sample_duration < self.profile_interval:
            problems.append("profile_cpu_sample_duration must be > 0 and < profile_interval")
        if problems:
            raise ValueError("invalid CloudConfig: " + "; ".join(problems))


def cloud(
    api_key: str,
    /,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    insecure: bool = False,
    push_interval: float | datetime.timedelta = 15.0,
    profile_interval: float | datetime.timedelta = 60.0,
    profile_cpu_sample_duration: float | datetime.timedelta = 10.0,
    profiles: Iterable[ProfileType] = DEFAULT_PROFILE_TYPES,
) -> CloudConfig:
    """Build a :class:`CloudConfig` for ``init(cloud=...)``."""
    return CloudConfig(
        api_key=api_key,
        endpoint=endpoint,
        insecure=insecure,
        push_interval=seconds(push_interval),
        profile_interval=seconds(profile_interval),
        profile_cpu_sample_duration=seconds(profile_cpu_sample_duration),
        profiles=tuple(profiles),
    )


def grpc_endpoint(endpoint: str, /) -> str:
    """Convert ``https://ingest.bedrock.dev`` into ``ingest.bedrock.dev:443`` (Go parity)."""
    raise NotImplementedError


class RetryExporter:
    """Retries a failing exporter: ``attempts`` tries with ``backoff`` waits between them."""

    def __init__(
        self,
        exporter: SpanExporter,
        /,
        *,
        attempts: int = 3,
        backoff: Sequence[float] = (1.0, 2.0, 4.0),
        stop: threading.Event | None = None,
    ) -> None:
        raise NotImplementedError

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Interrupt any back-off wait, then shut the wrapped exporter down."""
        raise NotImplementedError


class FanOutExporter:
    """Sends each batch to every exporter; raises the first error after calling all."""

    def __init__(self, *exporters: SpanExporter) -> None:
        raise NotImplementedError

    def export(self, spans: Sequence[Span], /, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    def shutdown(self, *, timeout: float | None = None) -> None:
        raise NotImplementedError


class CloudWiring:
    """Handles to the cloud workers started by :func:`wire`."""

    def stop(self, *, timeout: float | None = None) -> None:
        """Final metrics push and stop all cloud workers, bounded by ``timeout``."""
        raise NotImplementedError


def wire(bedrock: Bedrock, config: CloudConfig, /) -> CloudWiring:
    """Replace the batch processor and start metrics and profile push for ``bedrock``."""
    raise NotImplementedError
