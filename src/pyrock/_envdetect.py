"""Resource auto-detection: process, git, Kubernetes, container, AWS and GCP.

Every detector runs in a thread pool under a 500 ms total budget; exceptions
are caught and counted, so ``init()`` never blocks or fails on detection.

The metadata base URLs can be overridden with :data:`AWS_ENDPOINT_ENV` and
:data:`GCP_ENDPOINT_ENV` (used by tests; an empty value disables that probe).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyrock.attr import AttrValue

__all__ = [
    "AWS_BASE",
    "AWS_ENDPOINT_ENV",
    "DEFAULT_BUDGET",
    "GCP_BASE",
    "GCP_ENDPOINT_ENV",
    "Detection",
    "detect",
    "detect_aws",
    "detect_container",
    "detect_gcp",
    "detect_git",
    "detect_kubernetes",
    "detect_process",
]

AWS_BASE = "http://169.254.169.254"
GCP_BASE = "http://metadata.google.internal"
AWS_ENDPOINT_ENV = "PYROCK_AWS_METADATA_ENDPOINT"
GCP_ENDPOINT_ENV = "PYROCK_GCP_METADATA_ENDPOINT"
DEFAULT_BUDGET = 0.5
METADATA_TIMEOUT = 0.5
GIT_TIMEOUT = 0.2


@dataclass(frozen=True, slots=True)
class Detection:
    """Detected resource attributes plus the number of detectors that failed."""

    attrs: Mapping[str, AttrValue] = field(default_factory=dict)
    errors: int = 0


def detect(
    *,
    environ: Mapping[str, str] | None = None,
    budget: float = DEFAULT_BUDGET,
) -> Detection:
    """Run every detector concurrently within ``budget`` seconds."""
    raise NotImplementedError


def detect_process() -> dict[str, AttrValue]:
    """``process.pid``, ``process.executable.name``, ``process.runtime.*``, ``host.name``."""
    raise NotImplementedError


def detect_git(*, timeout: float = GIT_TIMEOUT) -> dict[str, AttrValue]:
    """``service.version`` from ``git rev-parse`` (12 chars) or package metadata."""
    raise NotImplementedError


def detect_kubernetes(environ: Mapping[str, str], /) -> dict[str, AttrValue]:
    """``k8s.pod.name``, ``k8s.namespace``, ``k8s.node.name``, ``k8s.container.name``."""
    raise NotImplementedError


def detect_container() -> dict[str, AttrValue]:
    """``container.id`` from ``/proc/self/cgroup`` / ``mountinfo`` (skipped in k8s)."""
    raise NotImplementedError


def detect_aws(
    base: str = AWS_BASE, /, *, timeout: float = METADATA_TIMEOUT
) -> dict[str, AttrValue]:
    """IMDSv2: ``PUT /latest/api/token`` then GET instance-id and region."""
    raise NotImplementedError


def detect_gcp(
    base: str = GCP_BASE, /, *, timeout: float = METADATA_TIMEOUT
) -> dict[str, AttrValue]:
    """GCP metadata (``Metadata-Flavor: Google``): instance id and region from the zone."""
    raise NotImplementedError
