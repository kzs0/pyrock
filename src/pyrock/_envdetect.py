"""Resource auto-detection: process, git, Kubernetes, container, AWS and GCP.

Every detector runs on its own daemon thread under a 500 ms total budget;
exceptions are caught and counted, so ``init()`` never blocks or fails on
detection. A detector that misses the budget is abandoned (its thread is a
daemon and every probe it makes has its own short timeout) and counted.

The metadata base URLs can be overridden with :data:`AWS_ENDPOINT_ENV` and
:data:`GCP_ENDPOINT_ENV` (used by tests; an empty value disables that probe).
Metadata requests never go through an HTTP proxy.
"""

from __future__ import annotations

import functools
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyrock.attr import AttrValue

if TYPE_CHECKING:
    import urllib.request

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

Detector = Callable[[], dict[str, AttrValue]]

_REVISION_LENGTH = 12
_MAX_METADATA_BYTES = 4096
_CONTAINER_ID_PREFIXES = ("docker-", "containerd-", "cri-containerd-", "crio-", "libpod-")
_HEX = frozenset("0123456789abcdefABCDEF")
_K8S_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


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
    """Run every detector concurrently within ``budget`` seconds.

    Results are merged in a fixed order (process, git, Kubernetes, container,
    cloud), whatever order the detectors finish in. AWS wins over GCP when
    both answer. Container detection is skipped inside Kubernetes.
    """
    env = os.environ if environ is None else environ
    local: list[Detector] = [detect_process, detect_git, functools.partial(detect_kubernetes, env)]
    if not _in_kubernetes(env):
        local.append(detect_container)
    clouds: list[Detector] = [
        functools.partial(probe, base)
        for variable, default, probe in (
            (AWS_ENDPOINT_ENV, AWS_BASE, detect_aws),
            (GCP_ENDPOINT_ENV, GCP_BASE, detect_gcp),
        )
        if (base := env.get(variable, default))
    ]
    detectors = local + clouds

    done = threading.Condition()
    results: list[dict[str, AttrValue] | None] = [None] * len(detectors)
    errors = 0
    finished = 0

    def run(index: int, detector: Detector) -> None:
        nonlocal errors, finished
        try:
            found: dict[str, AttrValue] | None = detector()
        except Exception:  # a detector must never break init()
            found = None
        with done:
            results[index] = found
            errors += found is None
            finished += 1
            done.notify()

    for index, detector in enumerate(detectors):
        threading.Thread(
            target=run, args=(index, detector), name="pyrock-envdetect", daemon=True
        ).start()
    deadline = time.monotonic() + budget
    with done:
        done.wait_for(lambda: finished == len(detectors), max(0.0, deadline - time.monotonic()))
        snapshot = list(results)
        failed = errors + (len(detectors) - finished)
    attrs: dict[str, AttrValue] = {}
    for found in snapshot[: len(local)]:
        attrs.update(found or {})
    attrs.update(next((found for found in snapshot[len(local) :] if found), {}))
    return Detection(attrs, failed)


def _in_kubernetes(environ: Mapping[str, str]) -> bool:
    return bool(environ.get("KUBERNETES_SERVICE_HOST"))


def detect_process() -> dict[str, AttrValue]:
    """``process.pid``, ``process.executable.name``, ``process.runtime.*``, ``host.name``."""
    version = sys.implementation.version
    out: dict[str, AttrValue] = {
        "process.pid": os.getpid(),
        "process.executable.name": _executable_name(),
        "process.runtime.name": sys.implementation.name,
        "process.runtime.version": f"{version.major}.{version.minor}.{version.micro}",
    }
    host = socket.gethostname()
    if host:
        out["host.name"] = host
    return {key: value for key, value in out.items() if value != ""}


def _executable_name() -> str:
    """The ``-m`` module name, else the script's base name, else the interpreter's."""
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    module = getattr(spec, "name", None)
    if isinstance(module, str) and module:
        return module.removesuffix(".__main__")
    script = sys.argv[0] if sys.argv else ""
    if script and script not in ("-c", "-m", "-"):
        return os.path.basename(script)
    return os.path.basename(sys.executable)


def detect_git(*, timeout: float = GIT_TIMEOUT) -> dict[str, AttrValue]:
    """``service.version`` from ``git rev-parse`` (12 chars) or package metadata."""
    revision = _git_revision(timeout)
    if revision:
        return {"service.version": revision[:_REVISION_LENGTH]}
    version = _distribution_version()
    return {"service.version": version} if version else {}


def _source_dir() -> str:
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    return os.path.dirname(os.path.abspath(path)) if isinstance(path, str) else os.getcwd()


def _git_revision(timeout: float) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git from PATH, like Go's vcs stamping
            cwd=_source_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    revision = result.stdout.strip() if result.returncode == 0 else ""
    return revision if revision and set(revision) <= _HEX else ""


def _distribution_version() -> str:
    """The version of the distribution providing the ``-m`` package, if any."""
    import importlib.metadata

    main = sys.modules.get("__main__")
    module = getattr(getattr(main, "__spec__", None), "name", None)
    if not isinstance(module, str) or not module:
        return ""
    names = importlib.metadata.packages_distributions().get(module.split(".", 1)[0])
    if not names:
        return ""
    try:
        return importlib.metadata.version(names[0])
    except importlib.metadata.PackageNotFoundError:
        return ""


def detect_kubernetes(environ: Mapping[str, str], /) -> dict[str, AttrValue]:
    """``k8s.pod.name``, ``k8s.namespace``, ``k8s.node.name``, ``k8s.container.name``.

    Only inside Kubernetes (``KUBERNETES_SERVICE_HOST`` set). The variables and
    files are the ones Go reads.
    """
    if not _in_kubernetes(environ):
        return {}
    candidates: dict[str, str] = {
        "k8s.pod.name": environ.get("HOSTNAME", "") or _read_first_line("/etc/hostname"),
        "k8s.namespace": environ.get("KUBERNETES_NAMESPACE", "")
        or _read_first_line(_K8S_NAMESPACE_FILE),
        "k8s.node.name": environ.get("KUBERNETES_NODE_NAME", ""),
        "k8s.container.name": environ.get("KUBERNETES_CONTAINER_NAME", ""),
    }
    return {key: value for key, value in candidates.items() if value}


def _read_first_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as file:
            return file.readline().strip()
    except OSError:
        return ""


def detect_container() -> dict[str, AttrValue]:
    """``container.id`` from ``/proc/self/cgroup`` or ``/proc/self/mountinfo``."""
    for path in ("/proc/self/cgroup", "/proc/self/mountinfo"):
        try:
            with open(path, encoding="utf-8", errors="replace") as file:
                lines = file.read().splitlines()
        except OSError:
            continue
        for line in lines:
            container_id = _container_id(line)
            if container_id:
                return {"container.id": container_id}
    return {}


def _container_id(line: str) -> str:
    """The last 64-hex path segment of ``line`` (``docker-<id>.scope`` forms included)."""
    for segment in reversed(line.split("/")):
        segment = segment.split(" ", 1)[0]
        stem, dot, _ = segment.rpartition(".")
        if dot and stem:
            segment = stem
        for prefix in _CONTAINER_ID_PREFIXES:
            if segment.startswith(prefix):
                segment = segment[len(prefix) :]
                break
        if len(segment) == 64 and set(segment) <= _HEX:
            return segment
    return ""


def detect_aws(
    base: str = AWS_BASE, /, *, timeout: float = METADATA_TIMEOUT
) -> dict[str, AttrValue]:
    """IMDSv2: ``PUT /latest/api/token`` then GET instance-id and region."""
    opener = _direct_opener()
    base = base.rstrip("/")
    token = _fetch(
        opener,
        f"{base}/latest/api/token",
        {"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        timeout,
        method="PUT",
    )
    if not token:
        return {}
    headers = {"X-aws-ec2-metadata-token": token}
    instance_id = _fetch(opener, f"{base}/latest/meta-data/instance-id", headers, timeout)
    if not instance_id:
        return {}
    out: dict[str, AttrValue] = {"cloud.provider": "aws", "host.id": instance_id}
    region = _fetch(opener, f"{base}/latest/meta-data/placement/region", headers, timeout)
    if region:
        out["cloud.region"] = region
    return out


def detect_gcp(
    base: str = GCP_BASE, /, *, timeout: float = METADATA_TIMEOUT
) -> dict[str, AttrValue]:
    """GCP metadata (``Metadata-Flavor: Google``): instance id and region from the zone."""
    opener = _direct_opener()
    base = base.rstrip("/")
    headers = {"Metadata-Flavor": "Google"}
    instance_id = _fetch(opener, f"{base}/computeMetadata/v1/instance/id", headers, timeout)
    if not instance_id:
        return {}
    out: dict[str, AttrValue] = {"cloud.provider": "gcp", "host.id": instance_id}
    zone = _fetch(opener, f"{base}/computeMetadata/v1/instance/zone", headers, timeout)
    region = zone.rsplit("/", 1)[-1].rpartition("-")[0]
    if region:
        out["cloud.region"] = region
    return out


def _direct_opener() -> urllib.request.OpenerDirector:
    """A urllib opener that ignores ``*_proxy`` variables (metadata is link-local)."""
    import urllib.request

    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    *,
    method: str = "GET",
) -> str:
    """The stripped body of a 200 response, or ``""`` on any failure."""
    import urllib.request

    request = urllib.request.Request(url, headers=dict(headers), method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return ""
            body: bytes = response.read(_MAX_METADATA_BYTES)
    except (OSError, ValueError):
        return ""
    return body.decode("utf-8", errors="replace").strip()
