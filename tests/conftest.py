"""Shared fixtures. See ``tests/support/README.md``."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.collectors import (
    FakeMetadataServer,
    GRPCCollector,
    MetricsPushSink,
    OTLPHTTPCollector,
)
from tests.support.logs import LogCapture

_DIR_MARKERS = {"e2e": "e2e", "unit": "unit", "structure": "unit", "support": "unit"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark tests by directory: ``tests/e2e`` -> ``e2e``; unit/structure/support -> ``unit``."""
    root = Path(__file__).parent
    for item in items:
        try:
            first = item.path.relative_to(root).parts[0]
        except (ValueError, IndexError):
            continue
        marker = _DIR_MARKERS.get(first)
        if marker and not item.get_closest_marker(marker):
            item.add_marker(getattr(pytest.mark, marker))


THREAD_LEAK_GRACE = 2.0
"""Seconds a test's non-daemon threads get to finish after the test (and its fixtures) end."""


@pytest.fixture
def otlp_collector() -> Iterator[OTLPHTTPCollector]:
    with OTLPHTTPCollector() as collector:
        yield collector


@pytest.fixture
def push_sink() -> Iterator[MetricsPushSink]:
    with MetricsPushSink() as sink:
        yield sink


@pytest.fixture
def metadata_server() -> Iterator[FakeMetadataServer]:
    """An AWS-flavoured metadata server; build ``FakeMetadataServer(provider=...)`` for others."""
    with FakeMetadataServer() as server:
        yield server


@pytest.fixture
def grpc_collector() -> Iterator[GRPCCollector]:
    """Skips when grpcio / opentelemetry-proto are not installed."""
    with GRPCCollector() as collector:
        yield collector


@pytest.fixture
def log_capture() -> LogCapture:
    return LogCapture()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every ``BEDROCK_*`` / ``PYROCK_*`` variable and disable cloud metadata probes.

    Returns the ``monkeypatch`` so tests can ``setenv`` more.
    """
    for key in list(os.environ):
        if key.startswith(("BEDROCK_", "PYROCK_")):
            monkeypatch.delenv(key, raising=False)
    # Empty endpoints disable the AWS/GCP probes so tests never touch 169.254.169.254.
    monkeypatch.setenv("PYROCK_AWS_METADATA_ENDPOINT", "")
    monkeypatch.setenv("PYROCK_GCP_METADATA_ENDPOINT", "")
    return monkeypatch


def _non_daemon_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if not t.daemon and t.is_alive()}


@pytest.fixture(autouse=True)
def _no_thread_leaks() -> Iterator[None]:
    """Fail a test that leaves non-daemon threads running (they would block exit)."""
    before = _non_daemon_threads()
    yield
    deadline = time.monotonic() + THREAD_LEAK_GRACE
    leaked = _non_daemon_threads() - before
    for thread in leaked:
        thread.join(max(0.0, deadline - time.monotonic()))
    leaked = {t for t in leaked if t.is_alive()}
    if leaked:
        names = sorted(t.name for t in leaked)
        pytest.fail(f"test leaked non-daemon threads: {names}", pytrace=False)
