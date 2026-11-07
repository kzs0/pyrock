"""Environment auto-detection end to end (DESIGN §13, Go ``detectEnvironment``).

Detected values are observed as exported resource attributes. Infrastructure
keys are dropped by the default privacy policy, so each test allowlists the
keys it looks for with ``resource_keys``.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

from tests.e2e._export_helpers import prom, running
from tests.support.collectors import FakeMetadataServer, OTLPHTTPCollector

pytestmark = pytest.mark.usefixtures("clean_env")

PROCESS_KEYS = [
    "process.pid",
    "process.executable.name",
    "process.runtime.name",
    "process.runtime.version",
    "host.name",
]
K8S_KEYS = ["k8s.pod.name", "k8s.namespace", "k8s.node.name", "k8s.container.name"]
CLOUD_KEYS = ["cloud.provider", "cloud.region", "host.id"]


def _resource(collector: OTLPHTTPCollector, keys: list[str], **overrides: Any) -> dict[str, Any]:
    from pyrock import privacy

    options: dict[str, Any] = {
        "trace_url": collector.traces_url,
        "disable_environment_detection": False,
        "data_policy": privacy.Policy(resource_keys=keys),
    }
    options.update(overrides)
    with running(**options) as b, b.operation("detect"):
        pass
    resource: dict[str, Any] = collector.span("detect")["resource"]
    return resource


@pytest.fixture
def aws(clean_env: pytest.MonkeyPatch) -> Iterator[FakeMetadataServer]:
    with FakeMetadataServer(provider="aws") as server:
        for key, value in server.environ().items():
            clean_env.setenv(key, value)
        yield server


@pytest.fixture
def gcp(clean_env: pytest.MonkeyPatch) -> Iterator[FakeMetadataServer]:
    with FakeMetadataServer(provider="gcp") as server:
        for key, value in server.environ().items():
            clean_env.setenv(key, value)
        yield server


# ---------------------------------------------------------------- process


def test_process_pid_detected(otlp_collector: OTLPHTTPCollector) -> None:
    assert _resource(otlp_collector, PROCESS_KEYS)["process.pid"] == os.getpid()


def test_host_name_detected(otlp_collector: OTLPHTTPCollector) -> None:
    assert _resource(otlp_collector, PROCESS_KEYS)["host.name"] == socket.gethostname()


def test_runtime_attrs_detected(otlp_collector: OTLPHTTPCollector) -> None:
    resource = _resource(otlp_collector, PROCESS_KEYS)
    assert resource["process.runtime.name"] == sys.implementation.name
    major, minor = sys.version_info[:2]
    assert resource["process.runtime.version"].startswith(f"{major}.{minor}")


def test_executable_name_detected(otlp_collector: OTLPHTTPCollector) -> None:
    name = _resource(otlp_collector, PROCESS_KEYS)["process.executable.name"]
    assert name
    assert "/" not in name


def test_static_attrs_include_detected_values() -> None:
    with running(disable_environment_detection=False) as b:
        attrs = dict(b.static_attrs)
    assert attrs["process.pid"] == os.getpid()


# ---------------------------------------------------------------- kubernetes


def test_kubernetes_attrs_from_env(
    otlp_collector: OTLPHTTPCollector, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    clean_env.setenv("HOSTNAME", "users-api-7d9f8-abcde")
    clean_env.setenv("KUBERNETES_NAMESPACE", "prod")
    clean_env.setenv("KUBERNETES_NODE_NAME", "node-1")
    clean_env.setenv("KUBERNETES_CONTAINER_NAME", "app")
    resource = _resource(otlp_collector, K8S_KEYS)
    assert resource["k8s.pod.name"] == "users-api-7d9f8-abcde"
    assert resource["k8s.namespace"] == "prod"
    assert resource["k8s.node.name"] == "node-1"
    assert resource["k8s.container.name"] == "app"


def test_kubernetes_attrs_absent_outside_kubernetes(
    otlp_collector: OTLPHTTPCollector, clean_env: pytest.MonkeyPatch
) -> None:
    for key in [k for k in os.environ if k.startswith("KUBERNETES_")]:
        clean_env.delenv(key)
    resource = _resource(otlp_collector, ["k8s.namespace", "k8s.node.name", "k8s.container.name"])
    assert not [k for k in resource if k.startswith("k8s.")]


# ---------------------------------------------------------------- clouds


def test_aws_attrs_via_imdsv2(otlp_collector: OTLPHTTPCollector, aws: FakeMetadataServer) -> None:
    resource = _resource(otlp_collector, CLOUD_KEYS)
    assert resource["cloud.provider"] == "aws"
    assert resource["cloud.region"] == "us-west-2"
    assert resource["host.id"] == "i-0123456789abcdef0"


def test_aws_detection_fetches_token_first(
    otlp_collector: OTLPHTTPCollector, aws: FakeMetadataServer
) -> None:
    _resource(otlp_collector, CLOUD_KEYS)
    requests = aws.requests
    assert requests[0].method == "PUT"
    assert requests[0].path == "/latest/api/token"
    gets = [r for r in requests if r.path.startswith("/latest/meta-data/")]
    assert gets
    assert all(r.header("x-aws-ec2-metadata-token") == aws.token for r in gets)


def test_gcp_attrs_via_metadata_server(
    otlp_collector: OTLPHTTPCollector, gcp: FakeMetadataServer
) -> None:
    resource = _resource(otlp_collector, CLOUD_KEYS)
    assert resource["cloud.provider"] == "gcp"
    assert resource["cloud.region"] == "us-central1"
    assert resource["host.id"] == "1234567890123456789"


def test_gcp_detection_sends_metadata_flavor(
    otlp_collector: OTLPHTTPCollector, gcp: FakeMetadataServer
) -> None:
    _resource(otlp_collector, CLOUD_KEYS)
    gets = [r for r in gcp.requests if r.path.startswith("/computeMetadata/v1/")]
    assert gets
    assert all(r.header("metadata-flavor") == "Google" for r in gets)


def test_no_cloud_attrs_when_probes_disabled(otlp_collector: OTLPHTTPCollector) -> None:
    resource = _resource(otlp_collector, CLOUD_KEYS)
    assert "cloud.provider" not in resource


def test_hanging_metadata_server_does_not_delay_init(aws: FakeMetadataServer) -> None:
    import pyrock

    aws.hang()
    started = time.monotonic()
    b = pyrock.init(
        activate=False,
        server_enabled=False,
        log_install="none",
        runtime_metrics=False,
        disable_environment_detection=False,
    )
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 1.5  # 500 ms budget plus generous slack
    finally:
        aws.release()
        b.shutdown(timeout=5)


def test_failing_metadata_server_does_not_fail_init(
    otlp_collector: OTLPHTTPCollector, aws: FakeMetadataServer
) -> None:
    aws.set_default_status(500)
    resource = _resource(otlp_collector, CLOUD_KEYS)
    assert "cloud.provider" not in resource


# ---------------------------------------------------------------- switches


def test_disable_environment_detection(
    otlp_collector: OTLPHTTPCollector, aws: FakeMetadataServer
) -> None:
    resource = _resource(
        otlp_collector, PROCESS_KEYS + CLOUD_KEYS, disable_environment_detection=True
    )
    assert not [k for k in resource if k in PROCESS_KEYS + CLOUD_KEYS]
    assert aws.requests == []


def test_user_static_attrs_override_detected(otlp_collector: OTLPHTTPCollector) -> None:
    resource = _resource(otlp_collector, PROCESS_KEYS, static_attrs={"host.name": "custom-host"})
    assert resource["host.name"] == "custom-host"


def test_static_attrs_order_puts_detected_first() -> None:
    with running(disable_environment_detection=False, static_attrs={"env": "prod"}) as b:
        keys = list(b.static_attrs)
    assert keys.index("process.pid") < keys.index("env")


def test_detected_resource_keys_are_not_metric_labels() -> None:
    from pyrock import privacy

    policy = privacy.Policy(resource_keys=PROCESS_KEYS)
    with running(disable_environment_detection=False, data_policy=policy) as b:
        b.counter("plain_total", "P").inc()
        with b.operation("labelled"):
            pass
        text = prom(b)
    for name in ("plain_total", "labelled_count"):
        for labels in text.label_sets(name):
            assert not [k for k in labels if k.startswith(("process_", "host_"))], labels
