"""Continuous profiling built from stdlib pieces, exported as OTLP Profiles.

=========== ===============================================================
``CPU``     sampler thread (100 Hz) keeping threads whose CPU clock advanced
``WALL``    the same sampler without the CPU filter (Python extra)
``HEAP``    ``tracemalloc`` snapshot diffs per interval
``THREADS`` thread stacks plus pending asyncio tasks (alias ``GOROUTINE``)
``MUTEX``   wait times recorded by the instrumented :class:`Lock`/:class:`RLock`
``BLOCK``   wait times recorded by :class:`Condition`/:class:`Semaphore`
=========== ===============================================================

``HEAP`` starts tracemalloc only if it is not already tracing, and stopping the
exporter stops tracemalloc only if pyrock started it. Every upload carries an
``x-profile-type`` header with the :class:`ProfileType` value.

This package is never imported by ``import pyrock``; it loads on first use.
"""

from __future__ import annotations

import enum
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pyrock._compat import is_nonempty_str, seconds
from pyrock.attr import AttrValue
from pyrock.privacy import Policy
from pyrock.profile._locks import Condition, Lock, RLock, Semaphore

__all__ = [
    "DEFAULT_PROFILE_TYPES",
    "Condition",
    "Lock",
    "ProfileExportConfig",
    "ProfileExportStats",
    "ProfileExporter",
    "ProfileType",
    "RLock",
    "Semaphore",
    "start_export",
]


def _is_str(value: object) -> bool:
    return isinstance(value, str)


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
    max_bytes: int = 8 << 20
    retries: int = 3
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

        problems: list[str] = []
        endpoint: object = self.endpoint
        parts = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if parts is None or not parts.netloc or parts.scheme not in ("http", "https"):
            problems.append("endpoint must be an absolute http(s) URL")
        elif parts.scheme == "http" and not self.allow_http:
            problems.append("endpoint must use https unless allow_http=True")
        if self.interval <= 0:
            problems.append("interval must be positive")
        if not 0 < self.cpu_sample_duration < self.interval:
            problems.append("cpu_sample_duration must be > 0 and < interval")
        if self.sample_hz <= 0 or self.sample_hz > 1000:
            problems.append("sample_hz must be within 1..1000")
        if self.timeout <= 0:
            problems.append("timeout must be positive")
        if self.max_bytes <= 0:
            problems.append("max_bytes must be positive")
        if self.retries < 0:
            problems.append("retries must be >= 0")
        if self.mutex_sample_fraction < 0:
            problems.append("mutex_sample_fraction must be >= 0")
        if self.block_sample_rate < 0:
            problems.append("block_sample_rate must be >= 0")
        for key, value in self.headers.items():
            if not (is_nonempty_str(key) and _is_str(value)):
                problems.append("headers must map str to str")
            elif any(c in key + value for c in "\r\n\0"):
                problems.append(f"header {key!r} contains control characters")
        if problems:
            raise ValueError("invalid ProfileExportConfig: " + "; ".join(problems))


@dataclass(frozen=True, slots=True)
class ProfileExportStats:
    """Cumulative profiling outcomes (Go: ``ProfileExportStats``)."""

    exported: int = 0
    failed: int = 0
    skipped: int = 0


# --- scratch reference
import threading as _threading
import time as _time
import urllib.error as _ue
import urllib.request as _ur


class _NoRedirect(_ur.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class ProfileExporter:
    def __init__(self, config, service, resource, policy):
        from pyrock.profile import _heap, _locks, _sampler
        self._cfg, self._service, self._resource = config, service, dict(resource or {})
        self._stop = _threading.Event()
        self._lock = _threading.Lock()
        self._stats = [0, 0, 0]
        self._heap = _heap.HeapProfiler() if ProfileType.HEAP in config.types else None
        if self._heap: self._heap.start()
        _locks.configure(mutex_sample_fraction=config.mutex_sample_fraction, block_sample_rate=config.block_sample_rate)
        self._opener = _ur.build_opener(_NoRedirect)
        self._thread = _threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _bump(self, i):
        with self._lock: self._stats[i] += 1

    def stats(self):
        with self._lock: return ProfileExportStats(*self._stats)

    @property
    def running(self): return self._thread.is_alive() and not self._stop.is_set()

    def stop(self, *, timeout=None):
        self._stop.set()
        self._thread.join(timeout)
        if self._heap: self._heap.stop(); self._heap = None

    def _run(self):
        while not self._stop.is_set():
            for t in self._cfg.types:
                if self._stop.is_set(): return
                self._one(t)
            if self._stop.wait(self._cfg.interval): return

    def _collect(self, t):
        from pyrock.profile import _locks, _sampler, _stacks
        if t in (ProfileType.CPU, ProfileType.WALL):
            p = _sampler.SamplingProfiler(hz=self._cfg.sample_hz, cpu=t is ProfileType.CPU, sleep=lambda s: self._stop.wait(s))
            return p.collect(self._cfg.cpu_sample_duration)
        if t is ProfileType.HEAP: return self._heap.snapshot()
        if t is ProfileType.THREADS: return _stacks.thread_profile()
        if t is ProfileType.MUTEX:
            return None if self._cfg.mutex_sample_fraction == 0 else _locks.mutex_profile()
        return None if self._cfg.block_sample_rate == 0 else _locks.block_profile()

    def _one(self, t):
        from pyrock.profile import _otlp
        prof = self._collect(t)
        if self._stop.is_set(): return
        if prof is None: self._bump(2); return
        try:
            data = _otlp.encode_export_request(prof, service=self._service, profile_type=t.value, resource=self._resource, max_bytes=self._cfg.max_bytes)
        except ValueError:
            self._bump(1); return
        for attempt in range(self._cfg.retries + 1):
            if attempt and self._stop.wait(0.1 * attempt): return
            req = _ur.Request(self._cfg.endpoint, data=data, method="POST", headers={**self._cfg.headers, "Content-Type": "application/x-protobuf", "x-profile-type": t.value})
            try:
                with self._opener.open(req, timeout=self._cfg.timeout) as resp:
                    resp.read()
                self._bump(0); return
            except _ue.HTTPError as e:
                if (e.code == 429 or e.code >= 500) and attempt < self._cfg.retries: continue
                self._bump(1); return
            except OSError:
                if attempt < self._cfg.retries: continue
                self._bump(1); return


def start_export(config, /, *, service, resource=None, policy=None):
    return ProfileExporter(config, service, resource, policy)
