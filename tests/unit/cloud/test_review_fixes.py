"""Review fixes in the cloud wiring, metrics push and environment detection."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from pyrock import _envdetect
from pyrock._cloud import CloudConfig, CloudWiring, cloud, grpc_endpoint
from pyrock._compat import ConfigError
from pyrock._push import MetricsPusher
from pyrock.metric import Registry

pytestmark = pytest.mark.unit

KEY = "brk_test_key"


# ------------------------------------------------------------------ CloudConfig


def test_cloud_config_raises_config_error_with_every_problem() -> None:
    with pytest.raises(ConfigError) as info:
        CloudConfig(api_key="", push_interval=0)
    problems = info.value.problems
    assert any("api_key" in p for p in problems)
    assert any("push_interval" in p for p in problems)
    assert isinstance(info.value, ValueError)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"endpoint": "http://ingest.local"},
        {"traces_endpoint": "http://collector:4317"},
    ],
)
def test_plaintext_endpoints_require_insecure(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="insecure=True") as info:
        cloud(KEY, **kwargs)
    assert KEY not in str(info.value)
    cloud(KEY, insecure=True, **kwargs)  # explicitly allowed


@pytest.mark.parametrize(
    "kwargs",
    [
        {"endpoint": "ingest.bedrock.dev:443"},  # pushes need a URL
        {"endpoint": "ftp://ingest.bedrock.dev"},
        {"endpoint": "https://user:pw@ingest.bedrock.dev"},
        {"traces_endpoint": "collector:notaport"},
        {"traces_endpoint": "https://user:pw@collector"},
    ],
)
def test_malformed_endpoints_are_config_errors(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigError) as info:
        cloud(KEY, **kwargs)
    assert "pw@" not in str(info.value)


def test_bare_traces_endpoint_is_tls_and_allowed() -> None:
    cfg = cloud(KEY, traces_endpoint="collector:4317")
    assert cfg.traces_endpoint == "collector:4317"


def test_grpc_endpoint_rejects_malformed_targets() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        grpc_endpoint("collector:notaport")


# ---------------------------------------------------------------- metrics push


def test_pusher_validates_headers_without_echoing_values() -> None:
    with pytest.raises(ConfigError, match="metrics push headers") as info:
        MetricsPusher(
            Registry(""),
            endpoint="https://h/v1/metrics",
            headers={"Authorization": "Bearer s3cr3t\r\nX-Injected: 1"},
        )
    assert "s3cr3t" not in str(info.value)


def test_pusher_refuses_http_unless_allowed() -> None:
    with pytest.raises(ConfigError, match="plaintext"):
        MetricsPusher(Registry(""), endpoint="http://h/v1/metrics", allow_http=False)
    MetricsPusher(Registry(""), endpoint="https://h/v1/metrics", allow_http=False)
    MetricsPusher(Registry(""), endpoint="http://h/v1/metrics", allow_http=True)


def test_pusher_endpoint_errors_are_sanitized() -> None:
    with pytest.raises(ConfigError) as info:
        MetricsPusher(Registry(""), endpoint="ftp://user:s3cr3t@h/x?k=s3cr3t")
    assert "s3cr3t" not in str(info.value)


# ---------------------------------------------------------------- fork refresh


class _Pusher:
    def __init__(self) -> None:
        self.forks = 0

    def _after_fork_in_child(self) -> None:
        self.forks += 1

    def stop(self, *, timeout: float | None = None) -> None:
        pass


class _Exporter:
    def __init__(self) -> None:
        self.resources: list[dict[str, Any]] = []

    def _replace_resource(self, resource: dict[str, Any]) -> None:
        self.resources.append(dict(resource))


def test_wiring_fork_refreshes_exporter_resources() -> None:
    pusher, exporter, plain = _Pusher(), _Exporter(), object()
    bedrock = types.SimpleNamespace(tracer=types.SimpleNamespace(resource={"process.pid": 42}))
    wiring = CloudWiring(pusher, bedrock=bedrock, exporters=[exporter, plain])  # type: ignore[arg-type]
    wiring._after_fork_in_child()
    assert pusher.forks == 1
    assert exporter.resources == [{"process.pid": 42}]


def test_wiring_without_bedrock_still_forwards_fork() -> None:
    pusher = _Pusher()
    CloudWiring(pusher)._after_fork_in_child()  # type: ignore[arg-type]
    assert pusher.forks == 1


# ------------------------------------------------------------ cloud detection


@pytest.fixture
def no_dmi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(_envdetect, "_DMI_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, set()),
        ({"AWS_EXECUTION_ENV": "AWS_ECS_FARGATE"}, {"aws"}),
        ({"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4"}, {"aws"}),
        ({"AWS_REGION": "us-west-2"}, {"aws"}),
        ({"K_SERVICE": "svc"}, {"gcp"}),
        ({"GOOGLE_CLOUD_PROJECT": "p", "AWS_REGION": "x"}, {"aws", "gcp"}),
        ({"GCE_METADATA_HOST": "metadata:8080"}, {"gcp"}),
        ({"AWS_REGION": ""}, set()),
    ],
)
def test_cloud_hints_from_environment(
    no_dmi: Path, environ: dict[str, str], expected: set[str]
) -> None:
    assert _envdetect.cloud_hints(environ) == expected


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"sys_vendor": "Amazon EC2\n"}, {"aws"}),
        ({"bios_vendor": "Amazon EC2"}, {"aws"}),
        ({"product_name": "Google Compute Engine"}, {"gcp"}),
        ({"sys_vendor": "Google"}, {"gcp"}),
        ({"sys_vendor": "LENOVO", "product_name": "ThinkPad"}, set()),
    ],
)
def test_cloud_hints_from_dmi(no_dmi: Path, files: dict[str, str], expected: set[str]) -> None:
    for name, text in files.items():
        (no_dmi / name).write_text(text)
    assert _envdetect.cloud_hints({}) == expected


def _record_probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake(provider: str) -> Any:
        def probe(base: str, /, **kwargs: Any) -> dict[str, Any]:
            calls.append((provider, base))
            return {}

        return probe

    monkeypatch.setattr(_envdetect, "detect_aws", fake("aws"))
    monkeypatch.setattr(_envdetect, "detect_gcp", fake("gcp"))
    return calls


def test_detect_skips_metadata_probes_without_hints(
    no_dmi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_probes(monkeypatch)
    _envdetect.detect(environ={}, budget=2.0)
    assert calls == []


def test_detect_probes_only_the_hinted_cloud(no_dmi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_probes(monkeypatch)
    _envdetect.detect(environ={"AWS_EXECUTION_ENV": "AWS_Lambda_python3.12"}, budget=2.0)
    assert calls == [("aws", _envdetect.AWS_BASE)]


def test_overrides_force_or_disable_probes(no_dmi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_probes(monkeypatch)
    environ = {
        _envdetect.AWS_ENDPOINT_ENV: "http://127.0.0.1:9",
        _envdetect.GCP_ENDPOINT_ENV: "",  # disabled even with a GCP hint
        "K_SERVICE": "svc",
    }
    _envdetect.detect(environ=environ, budget=2.0)
    assert calls == [("aws", "http://127.0.0.1:9")]


def test_gce_metadata_host_sets_the_gcp_base(no_dmi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_probes(monkeypatch)
    _envdetect.detect(environ={"GCE_METADATA_HOST": "127.0.0.1:8080"}, budget=2.0)
    assert calls == [("gcp", "http://127.0.0.1:8080")]
