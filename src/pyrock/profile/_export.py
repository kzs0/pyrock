"""The background profile exporter (Go: ``StartProfileExport``).

One daemon thread collects every configured type right away and then once per
``interval`` (a collection that overruns the interval drops the missed ticks,
like Go's ticker), encodes each profile as OTLP and POSTs it. Waits are on a
stop event, so :meth:`ProfileExporter.stop` interrupts a CPU window or a retry
back-off immediately; an HTTP request in flight is bounded by ``timeout``.

Outcomes per collected type: ``exported`` after a 2xx; ``failed`` after a
non-retryable status, exhausted retries, a timeout or an oversized profile;
``skipped`` when the type cannot be collected (another sampler owns the
process, or MUTEX/BLOCK sampling is disabled). Work interrupted by ``stop`` is
dropped without being counted, and nothing is sent after ``stop``.
"""

from __future__ import annotations

import http.client
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from pyrock._transport import no_redirect_opener
from pyrock._version import __version__
from pyrock.attr import AttrValue
from pyrock.privacy import Policy
from pyrock.profile import _locks
from pyrock.profile._config import ProfileExportConfig, ProfileExportStats, ProfileType
from pyrock.profile._heap import HeapProfiler
from pyrock.profile._otlp import CONTENT_TYPE, LabelFilter, encode_export_request
from pyrock.profile._pprof import Profile
from pyrock.profile._sampler import SamplingProfiler
from pyrock.profile._stacks import thread_profile

if TYPE_CHECKING:
    import asyncio

__all__ = ["LoopsSource", "ProfileExporter", "start_export"]

LoopsSource = Callable[[], Iterable["asyncio.AbstractEventLoop"]]
"""Returns the event loops whose pending tasks belong in THREADS profiles."""

_log = logging.getLogger("pyrock.profile")

_RETRY_STEP = 0.1
"""Back-off before retry ``n`` is ``n * 0.1`` seconds, as in Go."""


def _retryable(status: int | None) -> bool:
    """Connection errors (``None``), 429 and 5xx may succeed on a later attempt."""
    return status is None or status == 429 or status >= 500


class _Stopped(Exception):
    """Raised inside the worker when :meth:`ProfileExporter.stop` interrupts it."""


class ProfileExporter:
    """A running exporter: collects on ``interval`` and POSTs OTLP profiles."""

    def __init__(
        self,
        config: ProfileExportConfig,
        *,
        service: str,
        resource: Mapping[str, AttrValue] | None,
        policy: Policy,
        loops: LoopsSource | None,
    ) -> None:
        self._config = config
        self._service = service
        self._resource = policy.resource(resource) if resource else {}
        self._label_filter: LabelFilter | None = (
            policy.profile_labels if policy.allow_profile_labels else None
        )
        self._source_paths = policy.allow_source_paths
        self._loops = loops
        self._opener = no_redirect_opener(config.ssl_context)
        self._headers = {
            "Content-Type": CONTENT_TYPE,
            "User-Agent": f"pyrock/{__version__}",
            **config.headers,
        }
        self._heap = HeapProfiler()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._stats = ProfileExportStats()
        self._lock_settings: tuple[tuple[int, float], tuple[int, float]] | None = None
        self._cleaned_up = False
        self._thread = threading.Thread(target=self._run, name="pyrock-profile-export", daemon=True)

    # -- lifecycle ----------------------------------------------------------

    def _start(self) -> None:
        self._configure_contention()
        if ProfileType.HEAP in self._config.types:
            self._heap.start()
        self._thread.start()

    def stats(self) -> ProfileExportStats:
        """Cumulative ``exported``/``failed``/``skipped`` counts."""
        with self._lock:
            return self._stats

    def stop(self, *, timeout: float | None = None) -> None:
        """Stop collection (idempotent); never stops a profiler someone else owns.

        Waits up to ``timeout`` seconds (``None``: until done) for the worker.
        Stops tracemalloc only if this exporter started it, and restores the
        contention sampling settings it changed.
        """
        self._stop_event.set()
        if self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        with self._lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
        self._heap.stop()
        self._restore_contention()

    @property
    def running(self) -> bool:
        """Whether the worker is collecting (``False`` once :meth:`stop` was called)."""
        return not self._stop_event.is_set() and self._thread.is_alive()

    def _configure_contention(self) -> None:
        cfg = self._config
        if cfg.mutex_sample_fraction <= 0 and cfg.block_sample_rate <= 0:
            return
        before = _locks.settings()
        after = (
            cfg.mutex_sample_fraction or before[0],
            cfg.block_sample_rate or before[1],
        )
        _locks.configure(mutex_sample_fraction=after[0], block_sample_rate=after[1])
        self._lock_settings = (before, _locks.settings())

    def _restore_contention(self) -> None:
        if self._lock_settings is None:
            return
        before, after = self._lock_settings
        if _locks.settings() == after:  # nobody changed them since
            _locks.configure(mutex_sample_fraction=before[0], block_sample_rate=before[1])

    # -- the worker ---------------------------------------------------------

    def _run(self) -> None:
        interval = self._config.interval
        next_at = time.monotonic()
        while not self._stop_event.is_set():
            for profile_type in self._config.types:
                try:
                    self._export(profile_type)
                except _Stopped:
                    return
                except Exception:  # a broken collector must not end profiling
                    self._count(failed=1)
                    _log.exception("profile collection failed: type=%s", profile_type.value)
            next_at += interval
            now = time.monotonic()
            if next_at <= now:
                next_at = now + interval
            self._stop_event.wait(next_at - now)

    def _count(self, *, exported: int = 0, failed: int = 0, skipped: int = 0) -> None:
        with self._lock:
            self._stats += ProfileExportStats(exported, failed, skipped)

    def _export(self, profile_type: ProfileType) -> None:
        if self._stop_event.is_set():
            raise _Stopped
        profile = self._collect(profile_type)
        if self._stop_event.is_set():
            raise _Stopped
        if profile is None:
            self._count(skipped=1)
            return
        try:
            data = encode_export_request(
                profile,
                service=self._service,
                profile_type=profile_type.value,
                resource=self._resource,
                max_bytes=self._config.max_bytes,
                label_filter=self._label_filter,
                source_paths=self._source_paths,
            )
        except ValueError as exc:
            self._fail(profile_type, str(exc))
            return
        error = self._upload(data, profile_type)
        if error is None:
            self._count(exported=1)
        else:
            self._fail(profile_type, error)

    def _fail(self, profile_type: ProfileType, reason: str) -> None:
        self._count(failed=1)
        _log.warning("profile push failed: type=%s error=%s", profile_type.value, reason)

    def _collect(self, profile_type: ProfileType) -> Profile | None:
        """The profile for ``profile_type``; ``None`` when it must be skipped."""
        cfg = self._config
        if profile_type is ProfileType.CPU or profile_type is ProfileType.WALL:
            sampler = SamplingProfiler(hz=cfg.sample_hz, cpu=profile_type is ProfileType.CPU)
            return sampler.collect(cfg.cpu_sample_duration, cancel=self._stop_event)
        if profile_type is ProfileType.HEAP:
            return self._heap.snapshot()
        if profile_type is ProfileType.THREADS:
            return thread_profile(loops=self._loops() if self._loops else ())
        if profile_type is ProfileType.MUTEX:
            return _locks.mutex_profile() if cfg.mutex_sample_fraction > 0 else None
        return _locks.block_profile() if cfg.block_sample_rate > 0 else None

    def _upload(self, data: bytes, profile_type: ProfileType) -> str | None:
        """POST with retries within ``timeout``; ``None`` on success, else the reason."""
        cfg = self._config
        deadline = time.monotonic() + cfg.timeout
        reason = "timed out"
        for attempt in range(cfg.retries + 1):
            if attempt and self._stop_event.wait(attempt * _RETRY_STEP):
                raise _Stopped
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            status, reason = self._post(data, profile_type, remaining)
            if status is not None and 200 <= status < 300:
                return None
            if not _retryable(status):
                break
        return reason

    def _post(
        self, data: bytes, profile_type: ProfileType, timeout: float
    ) -> tuple[int | None, str]:
        """One attempt: the HTTP status (``None`` for a connection error) and a reason."""
        request = urllib.request.Request(
            self._config.endpoint,
            data=data,
            headers={**self._headers, "x-profile-type": profile_type.value},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                response.read()
                return response.status, f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, f"HTTP {exc.code}"
        except (OSError, http.client.HTTPException) as exc:
            return None, type(exc).__name__


def start_export(
    config: ProfileExportConfig,
    /,
    *,
    service: str,
    resource: Mapping[str, AttrValue] | None = None,
    policy: Policy | None = None,
    loops: LoopsSource | None = None,
) -> ProfileExporter:
    """Start a background exporter thread for ``config``.

    ``resource`` is filtered with ``policy.resource``; sample labels are sent
    only with ``policy.allow_profile_labels`` and file paths are reduced to
    base names unless ``policy.allow_source_paths``. ``loops`` supplies the
    event loops whose pending tasks appear in THREADS profiles.
    """
    exporter = ProfileExporter(
        config, service=service, resource=resource, policy=policy or Policy(), loops=loops
    )
    exporter._start()
    return exporter
