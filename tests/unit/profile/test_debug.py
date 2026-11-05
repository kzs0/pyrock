"""The optional ``/debug/pprof/*`` WSGI handler (``pyrock.profile.debug``)."""

from __future__ import annotations

import gzip
import sys
import threading
import time
from typing import Any

import pytest

from pyrock.profile import debug
from pyrock.profile._sampler import acquire_ownership, release_ownership
from tests.support.wsgi_driver import WSGIResponse, call_wsgi
from tests.unit.profile._proto import Pprof, decode_pprof


def get(path: str, method: str = "GET", **app_kw: Any) -> WSGIResponse:
    return call_wsgi(debug.app(**app_kw), method, path)


def pprof_body(resp: WSGIResponse) -> Pprof:
    assert resp.status_code == 200, resp.body[:200]
    assert resp.body[:2] == b"\x1f\x8b", "debug profiles are gzipped"
    return decode_pprof(gzip.decompress(resp.body))


# ---------------------------------------------------------------- isolation


def test_importing_pyrock_does_not_import_debug() -> None:
    import subprocess

    code = "import pyrock, sys; print('pyrock.profile.debug' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "False"


def test_app_returns_a_new_wsgi_callable() -> None:
    first, second = debug.app(), debug.app()
    assert callable(first)
    assert callable(second)


# ---------------------------------------------------------------- routes


def test_heap_is_gzipped_pprof_with_heap_sample_types() -> None:
    profile = pprof_body(get("/debug/pprof/heap"))
    assert {t for t, _ in profile.sample_types} >= {"alloc_space", "inuse_space"}


def test_cpu_profile_honors_short_seconds(busy_thread: threading.Thread) -> None:
    started = time.monotonic()
    profile = pprof_body(get("/debug/pprof/profile?seconds=0.3"))
    elapsed = time.monotonic() - started
    assert profile.sample_types == [("samples", "count"), ("cpu", "nanoseconds")]
    assert profile.samples_with("_busy_spin_for_unit_profile")
    assert 0.2 <= elapsed < 5.0


def test_goroutine_is_gzipped_pprof_with_thread_stacks(parked_thread: threading.Thread) -> None:
    profile = pprof_body(get("/debug/pprof/goroutine"))
    assert profile.sample_types == [("threads", "count"), ("tasks", "count")]
    assert profile.samples_with("_parked_worker_for_unit_profile")


def test_goroutine_debug_1_is_text(parked_thread: threading.Thread) -> None:
    resp = get("/debug/pprof/goroutine?debug=1")
    assert resp.status_code == 200
    assert (resp.header("Content-Type") or "").startswith("text/plain")
    assert resp.body[:2] != b"\x1f\x8b"
    text = resp.body.decode("utf-8")
    assert "_parked_worker_for_unit_profile" in text
    assert "parked-for-unit-profile" in text


@pytest.mark.parametrize("kind", ["mutex", "block"])
def test_contention_profiles_are_gzipped_pprof(kind: str) -> None:
    profile = pprof_body(get(f"/debug/pprof/{kind}"))
    assert profile.sample_types == [("contentions", "count"), ("delay", "nanoseconds")]


@pytest.mark.parametrize("kind", ["heap", "goroutine", "mutex", "block"])
def test_binary_profiles_are_octet_streams(kind: str) -> None:
    resp = get(f"/debug/pprof/{kind}")
    assert resp.status_code == 200
    assert (resp.header("Content-Type") or "").startswith("application/octet-stream")


@pytest.mark.parametrize(
    "path", ["/debug/pprof/nope", "/debug/pprof/heap/extra", "/elsewhere", "/debug/pprofheap"]
)
def test_unknown_paths_are_404(path: str) -> None:
    assert get(path).status_code == 404


def test_custom_prefix() -> None:
    assert get("/_internal/pprof/heap", prefix="/_internal/pprof").status_code == 200
    assert get("/debug/pprof/heap", prefix="/_internal/pprof").status_code == 404


# ---------------------------------------------------------------- methods


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_get_is_allowed(method: str) -> None:
    resp = get("/debug/pprof/heap", method=method)
    assert resp.status_code == 405
    assert "GET" in (resp.header("Allow") or "")


def test_method_check_happens_before_collection() -> None:
    started = time.monotonic()
    resp = get("/debug/pprof/profile?seconds=5", method="POST")
    assert resp.status_code == 405
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------- seconds


@pytest.mark.parametrize("seconds", ["31", "30.5", "0", "-1", "abc", "nan", "inf", "1e9", "5s"])
def test_invalid_seconds_are_400(seconds: str) -> None:
    started = time.monotonic()
    resp = get(f"/debug/pprof/profile?seconds={seconds}")
    assert resp.status_code == 400
    assert time.monotonic() - started < 2.0, "rejected requests must not collect"


def test_max_seconds_is_configurable() -> None:
    started = time.monotonic()
    assert get("/debug/pprof/profile?seconds=2", max_seconds=1.0).status_code == 400
    assert time.monotonic() - started < 2.0


def test_seconds_at_the_cap_are_accepted() -> None:
    resp = get("/debug/pprof/profile?seconds=0.2", max_seconds=0.2)
    assert resp.status_code == 200


@pytest.mark.usefixtures("no_owner_marker")
def test_cpu_profile_busy_while_another_sampler_owns_the_process() -> None:
    assert acquire_ownership()
    try:
        started = time.monotonic()
        resp = get("/debug/pprof/profile?seconds=0.2")
        assert time.monotonic() - started < 2.0
    finally:
        release_ownership()
    assert resp.status_code == 503
