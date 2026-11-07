"""Request instrumentation shared by the WSGI, ASGI and ``http.server`` integrations.

Private: :mod:`pyrock.wsgi`, :mod:`pyrock.asgi` and :mod:`pyrock.http` build
on :class:`Instrumentation`, which turns one incoming request into a
``SERVER`` operation with the HTTP attributes, labels and success rules all
three integrations share.

User callbacks (``route=`` and ``additional_attrs=``) are guarded: an
exception from one is logged once per integration on the ``pyrock`` logger
(type only, never the message, which may carry request data) and the request
falls back to the framework route or no extra attributes. The operation is
always ended, even when recording its outcome fails.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Container, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from pyrock._bedrock import current
from pyrock.logs import LOGGER_NAME
from pyrock.trace import SpanKind
from pyrock.trace.http import HeaderCarrier, HTTPPropagator

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock
    from pyrock._operation import Operation
    from pyrock.attr import AttrValue
    from pyrock.privacy import Policy

__all__ = [
    "DEFAULT_LABELS",
    "INTERNAL_ERROR",
    "PROPAGATOR",
    "ROUTE_KEY",
    "UNMATCHED_ROUTE",
    "Instrumentation",
    "RequestInfo",
    "parse_status",
]

ROUTE_KEY = "pyrock.route"
"""``environ``/``scope`` key a framework hook may set to the matched route template."""

UNMATCHED_ROUTE = "_"
"""``http.route`` when no template is known (shared by every unmatched path)."""

INTERNAL_ERROR = 500
"""``http.status_code`` recorded when the app fails before sending a response."""

DEFAULT_LABELS = ("http.method", "http.route", "http.status_code")
"""Metric labels of every server operation, before ``additional_labels``."""

PROPAGATOR = HTTPPropagator()
"""The W3C propagator used to extract remote parents from request headers."""

_USER_AGENT = "http.user_agent"

_log = logging.getLogger(LOGGER_NAME)

RequestT = TypeVar("RequestT")


@dataclass(frozen=True, slots=True)
class RequestInfo:
    """What a server integration extracted from one incoming request."""

    method: str | None
    """``None`` for requests without one (websocket connections)."""
    scheme: str
    host: str
    user_agent: str | None
    headers: HeaderCarrier


def parse_status(status: str) -> int | None:
    """The numeric code of a WSGI status line such as ``"404 Not Found"``."""
    code = status.partition(" ")[0]
    return int(code) if code.isdigit() else None


class Instrumentation(Generic[RequestT]):
    """Request instrumentation shared by the WSGI, ASGI and ``http.server`` integrations.

    ``RequestT`` is what user callbacks receive: the WSGI ``environ``, the
    ASGI ``scope`` or the ``BaseHTTPRequestHandler``. The configuration is
    immutable after construction, so one instance serves concurrent requests;
    the only mutable state is the "callback failure already logged" flags and
    a one-entry cache of the policy's ``http.user_agent`` verdict.
    """

    __slots__ = (
        "_additional_attrs",
        "_agent_verdict",
        "_bedrock",
        "_labels",
        "_logged",
        "_logged_lock",
        "_route",
        "_success_codes",
        "_trace_propagation",
    )

    def __init__(
        self,
        *,
        additional_labels: Iterable[str],
        additional_attrs: Callable[[RequestT], Mapping[str, AttrValue]] | None,
        success_codes: Container[int] | None,
        trace_propagation: bool,
        route: Callable[[RequestT], str | None] | None,
        bedrock: Bedrock | None,
    ) -> None:
        if bedrock is None:
            active = current()
            bedrock = None if active.is_noop else active
        self._bedrock = bedrock
        self._labels = tuple(dict.fromkeys((*DEFAULT_LABELS, *additional_labels)))
        self._additional_attrs = additional_attrs
        self._success_codes = success_codes
        self._trace_propagation = trace_propagation
        self._route = route
        self._agent_verdict: tuple[Policy, bool] | None = None
        self._logged: set[str] = set()
        self._logged_lock = threading.Lock()

    @property
    def labels(self) -> tuple[str, ...]:
        """Metric labels: the defaults plus ``additional_labels``."""
        return self._labels

    def instance(self) -> Bedrock:
        """The instance captured at construction, else the active one (maybe the noop)."""
        return current() if self._bedrock is None else self._bedrock

    def start(
        self,
        bedrock: Bedrock,
        name: str,
        request: RequestT,
        info: RequestInfo,
    ) -> Operation:
        """Start (without activating) the ``SERVER`` operation for one request.

        Requests without a method (websocket connections) record ``"_"`` for
        the method and status labels, so they share the HTTP metric schema.
        """
        attrs: dict[str, AttrValue] = {"http.scheme": info.scheme, "http.host": info.host}
        if info.method is not None:
            attrs["http.method"] = info.method
        agent = info.user_agent
        if agent and self._user_agent_allowed(bedrock.policy, agent):
            attrs[_USER_AGENT] = agent
        if self._additional_attrs is not None:
            attrs.update(self._extra_attrs(self._additional_attrs, request))
        remote = PROPAGATOR.extract(info.headers) if self._trace_propagation else None
        return bedrock.start_operation(
            name,
            attrs=attrs,
            metric_labels=self._labels,
            remote_parent=remote,
            kind=SpanKind.SERVER,
        )

    def finish(
        self,
        op: Operation,
        request: RequestT,
        *,
        framework_route: str | None,
        status: int | None,
        error: BaseException | None,
    ) -> None:
        """Record the route and outcome, then end ``op`` (always, even if recording fails).

        ``status=None`` means the response never started, recorded as ``500``
        whether or not an exception escaped.
        """
        try:
            recorded = INTERNAL_ERROR if status is None else status
            op.register(
                {
                    "http.route": self.route_of(request, framework_route),
                    "http.status_code": recorded,
                }
            )
            if error is None and not self.is_success(recorded):
                op.fail()
        finally:
            op.end(error=error)

    def finish_connection(
        self,
        op: Operation,
        request: RequestT,
        *,
        framework_route: str | None,
        error: BaseException | None,
    ) -> None:
        """End a status-less operation (a websocket connection): failure only on ``error``."""
        try:
            op.register({"http.route": self.route_of(request, framework_route)})
        finally:
            op.end(error=error)

    def route_of(self, request: RequestT, framework_route: str | None) -> str:
        """The ``route=`` callable's template, else the framework's, else ``"_"``.

        A ``route=`` callable that raises, or returns a non-string, is treated
        as returning ``None`` (and logged once).
        """
        if self._route is not None:
            try:
                template: object = self._route(request)
            except Exception as exc:
                self._callback_failed("route", exc)
            else:
                if isinstance(template, str):
                    return template
                if template is not None:
                    self._callback_failed("route", TypeError("route= must return str or None"))
        return UNMATCHED_ROUTE if framework_route is None else framework_route

    def is_success(self, status: int) -> bool:
        """Whether ``status`` counts as success (``success_codes``, else below 400)."""
        if self._success_codes is None:
            return status < 400
        return status in self._success_codes

    def _extra_attrs(
        self, callback: Callable[[RequestT], Mapping[str, AttrValue]], request: RequestT
    ) -> dict[str, AttrValue]:
        try:
            return dict(callback(request))
        except Exception as exc:
            self._callback_failed("additional_attrs", exc)
            return {}

    def _user_agent_allowed(self, policy: Policy, agent: str) -> bool:
        """Whether the policy admits ``http.user_agent`` (computed once per policy).

        Without a ``redact`` hook the verdict depends only on the key, so it is
        cached; with one, the hook sees every value, as before.
        """
        if policy.redact is not None:
            return policy.value(_USER_AGENT, agent)[1]
        cached = self._agent_verdict
        if cached is not None and cached[0] is policy:
            return cached[1]
        allowed = policy.value(_USER_AGENT, agent)[1]
        self._agent_verdict = (policy, allowed)
        return allowed

    def _callback_failed(self, which: str, exc: BaseException) -> None:
        """Log the first failure of callback ``which`` (its type only: messages may hold data)."""
        with self._logged_lock:
            if which in self._logged:
                return
            self._logged.add(which)
        _log.warning(
            "pyrock: the %s= callback raised %s; using the fallback "
            "(further failures are not logged)",
            which,
            type(exc).__qualname__,
        )
