"""Fixtures shared by the ``pyrock.profile`` unit tests."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from tests.unit.profile._helpers import (
    _busy_spin_for_unit_profile,
    _parked_worker_for_unit_profile,
    wait_in_function,
)


@pytest.fixture
def parked_thread() -> Iterator[threading.Thread]:
    release = threading.Event()
    thread = threading.Thread(
        target=_parked_worker_for_unit_profile,
        args=(release,),
        name="parked-for-unit-profile",
        daemon=True,
    )
    thread.start()
    wait_in_function(thread, "_parked_worker_for_unit_profile")
    yield thread
    release.set()
    thread.join(5)


@pytest.fixture
def busy_thread() -> Iterator[threading.Thread]:
    stop = threading.Event()
    thread = threading.Thread(
        target=_busy_spin_for_unit_profile, args=(stop,), name="busy-for-unit-profile", daemon=True
    )
    thread.start()
    wait_in_function(thread, "_busy_spin_for_unit_profile")
    yield thread
    stop.set()
    thread.join(5)


@pytest.fixture
def no_owner_marker(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No ``PYROCK_PROFILER_OWNER`` marker from the outside environment."""
    monkeypatch.delenv("PYROCK_PROFILER_OWNER", raising=False)
    return monkeypatch
