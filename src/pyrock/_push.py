"""Background Prometheus metrics push (Go: ``startMetricsPush``).

A daemon thread POSTs the registry's exposition text every ``interval``
seconds with ``Content-Type`` set to the Prometheus text type. Pushes are
synchronous on that thread, so at most one is ever in flight: ticks that pass
while a push is still running are skipped (and counted), and an explicit
:meth:`MetricsPusher.push_once` during a push is skipped too. Every request is
bounded by ``timeout``. A failed push is counted and logged; the next tick
tries again.

:meth:`MetricsPusher.stop` makes one final push, waited for at most the
caller's ``timeout``; if that expires the push still completes in the
background, bounded by its own request timeout. Redirects are never followed
because the headers carry credentials.
"""

from __future__ import annotations

import http.client
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from pyrock._compat import ConfigError, is_positive_number
from pyrock._transport import header_problems, no_redirect_opener
from pyrock._version import __version__
from pyrock.metric import Registry, prometheus
from pyrock.privacy import sanitize_url

__all__ = ["MetricsPusher", "PushStats"]

_log = logging.getLogger("pyrock.cloud")

_MAX_RESPONSE_BYTES = 64 << 10
"""How much of a response body is read (and discarded) before closing it."""


@dataclass(frozen=True, slots=True)
class PushStats:
    """Cumulative push outcomes."""

    pushed: int = 0
    """Pushes answered with a 2xx status."""
    failed: int = 0
    """Pushes that could not be encoded or sent, or got a non-2xx status."""
    skipped: int = 0
    """Ticks and :meth:`MetricsPusher.push_once` calls skipped because a push was in flight."""


def _endpoint_problems(endpoint: object, *, allow_http: bool) -> list[str]:
    if not isinstance(endpoint, str):
        return ["metrics push endpoint must be an http(s) URL"]
    shown = sanitize_url(endpoint)
    try:
        parts = urlsplit(endpoint)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        return [f"metrics push endpoint must be an http(s) URL: {shown!r}"]
    if parts.scheme == "http" and not allow_http:
        return [f"metrics push endpoint {shown!r} is plaintext http; set insecure=True to allow it"]
    return []


class MetricsPusher:
    """Pushes ``registry`` to ``endpoint`` every ``interval`` seconds from a daemon thread.

    ``endpoint`` must be an absolute ``http(s)`` URL; a plain ``http://`` one
    is refused when ``allow_http`` is false (the cloud wiring passes its
    ``insecure`` flag, so the API key never travels in plaintext by
    accident). ``headers`` are added to every request (for example
    ``Authorization``) and validated like every pyrock header. ``timeout``
    bounds each request. Thread-safe; :meth:`stop` is idempotent and may be
    called concurrently.
    """

    def __init__(
        self,
        registry: Registry,
        /,
        *,
        endpoint: str,
        headers: Mapping[str, str] | None = None,
        interval: float = 15.0,
        timeout: float = 10.0,
        allow_http: bool = True,
    ) -> None:
        """``ConfigError`` (a ``ValueError``) listing every invalid setting."""
        extra = dict(headers or {})
        problems = [
            *_endpoint_problems(endpoint, allow_http=allow_http),
            *header_problems(extra, field="metrics push headers"),
        ]
        if not is_positive_number(interval) or not is_positive_number(timeout):
            problems.append("metrics push interval and timeout must be positive and finite")
        if problems:
            raise ConfigError(problems)
        self._registry = registry
        self._endpoint = endpoint
        self._headers = {
            "Content-Type": prometheus.CONTENT_TYPE,
            "User-Agent": f"pyrock/{__version__}",
            **extra,
        }
        self._interval = interval
        self._timeout = timeout
        self._opener = no_redirect_opener()
        self._reset()

    def _reset(self) -> None:
        """Fresh locks, events and counters, and no worker (construction and after fork)."""
        self._lock = threading.Lock()
        """Guards the lifecycle fields and the counters."""
        self._in_flight = threading.Lock()
        """Held while a push is being sent."""
        self._stop_event = threading.Event()
        self._done = threading.Event()
        """Set once the worker (final flush included) has finished."""
        self._thread: threading.Thread | None = None
        self._final_flush = True
        self._stats = PushStats()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the push thread (idempotent); ``RuntimeError`` after :meth:`stop`."""
        with self._lock:
            if self._stop_event.is_set():
                raise RuntimeError("metrics pusher is stopped")
            if self._thread is None:
                self._locked_start_thread()

    def stop(self, *, timeout: float | None = None, final_flush: bool = True) -> None:
        """Stop pushing, after one last push unless ``final_flush=False``.

        Waits up to ``timeout`` seconds (``None``: until done) for the worker
        and its final push. Later and concurrent calls wait for the same
        shutdown; the first call's ``final_flush`` wins.
        """
        with self._lock:
            if not self._stop_event.is_set():
                self._final_flush = final_flush
                self._stop_event.set()
                if self._thread is None:  # never started: run just the final push
                    if final_flush:
                        self._locked_start_thread()
                    else:
                        self._done.set()
        if threading.current_thread() is not self._thread:
            self._done.wait(timeout)

    def stats(self) -> PushStats:
        """A snapshot of the counters."""
        with self._lock:
            return self._stats

    def _after_fork_in_child(self) -> None:
        """Forget the parent's worker and counters; restart if it was pushing."""
        was_running = self._thread is not None and not self._stop_event.is_set()
        self._reset()
        if was_running:
            self.start()

    # -- pushing ------------------------------------------------------------

    def push_once(self, *, timeout: float | None = None) -> bool:
        """Push now; ``False`` on failure or when a push is already in flight."""
        return self._push(timeout=timeout, wait=False)

    def _push(self, *, timeout: float | None = None, wait: bool) -> bool:
        """One counted push; ``wait`` waits for a push in flight instead of skipping."""
        if not self._in_flight.acquire(blocking=wait):
            self._count(skipped=1)
            return False
        try:
            ok = self._send(self._timeout if timeout is None else timeout)
        finally:
            self._in_flight.release()
        self._count(pushed=int(ok), failed=int(not ok))
        return ok

    def _send(self, timeout: float) -> bool:
        """Encode the registry and POST it once; log and return ``False`` on failure."""
        try:
            body = prometheus.encode(self._registry)
        except prometheus.ExpositionError:
            _log.warning("metrics push skipped: the registry could not be encoded", exc_info=True)
            return False
        request = urllib.request.Request(
            self._endpoint, data=body, headers=self._headers, method="POST"
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                response.read(_MAX_RESPONSE_BYTES)
                status: int = response.status
        except urllib.error.HTTPError as exc:
            exc.close()
            status = exc.code
        except (OSError, http.client.HTTPException) as exc:
            _log.warning("metrics push failed: %s", type(exc).__name__)
            return False
        if 200 <= status < 300:
            return True
        _log.warning("metrics push failed: HTTP %d", status)
        return False

    def _count(self, *, pushed: int = 0, failed: int = 0, skipped: int = 0) -> None:
        with self._lock:
            stats = self._stats
            self._stats = PushStats(
                stats.pushed + pushed, stats.failed + failed, stats.skipped + skipped
            )

    # -- the worker ---------------------------------------------------------

    def _locked_start_thread(self) -> None:
        thread = threading.Thread(target=self._run, name="pyrock-metrics-push", daemon=True)
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        try:
            due = time.monotonic() + self._interval
            while not self._stop_event.wait(min(due - time.monotonic(), threading.TIMEOUT_MAX)):
                self._tick(wait=False)
                due = self._next_due(due)
            if self._final_flush:
                self._tick(wait=True)  # waits for a push_once caller's push first
        finally:
            self._done.set()

    def _tick(self, *, wait: bool) -> None:
        try:
            self._push(wait=wait)
        except Exception:  # the worker must outlive any unexpected failure
            self._count(failed=1)
            _log.exception("metrics push failed unexpectedly")

    def _next_due(self, due: float) -> float:
        """The next tick after ``due``; ticks that already passed are skipped and counted."""
        due += self._interval
        overdue = time.monotonic() - due
        if overdue < 0:
            return due
        missed = math.floor(overdue / self._interval) + 1
        self._count(skipped=missed)
        return due + missed * self._interval
