"""Context variables and helpers that carry context across threads.

asyncio needs nothing extra (tasks copy the context). Threads start with an
empty context, so pyrock provides explicit helpers: :func:`bind`,
:class:`ContextThreadPoolExecutor` and :class:`Thread`. ``asyncio.to_thread``
already copies context.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, overload

if TYPE_CHECKING:
    from pyrock._bedrock import Bedrock
    from pyrock._operation import Operation, Source, Step

__all__ = [
    "ContextThreadPoolExecutor",
    "Thread",
    "bedrock_var",
    "bind",
    "no_trace_var",
    "operation_var",
    "source_var",
    "step_var",
]

P = ParamSpec("P")
R = TypeVar("R")

bedrock_var: contextvars.ContextVar[Bedrock | None] = contextvars.ContextVar(
    "pyrock.bedrock", default=None
)
operation_var: contextvars.ContextVar[Operation | None] = contextvars.ContextVar(
    "pyrock.operation", default=None
)
step_var: contextvars.ContextVar[Step | None] = contextvars.ContextVar("pyrock.step", default=None)
source_var: contextvars.ContextVar[Source | None] = contextvars.ContextVar(
    "pyrock.source", default=None
)
no_trace_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pyrock.no_trace", default=False
)


@overload
def bind(fn: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def bind(fn: Callable[..., R], /, *args: Any, **kwargs: Any) -> Callable[..., R]: ...


def bind(fn: Callable[..., R], /, *args: Any, **kwargs: Any) -> Callable[..., R]:
    """Capture the current context now; run ``fn`` inside it later.

    ``bind(fn)`` returns a wrapper with ``fn``'s signature. ``bind(fn, *args,
    **kwargs)`` pre-binds arguments like :func:`functools.partial`. Each call
    runs in a fresh copy of the captured context, so concurrent calls do not
    interfere.
    """
    raise NotImplementedError


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """A :class:`ThreadPoolExecutor` that copies the caller's context on every submit."""

    def submit(self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        raise NotImplementedError


class Thread(threading.Thread):
    """A :class:`threading.Thread` that runs ``target`` in a copy of the creator's context."""

    def __init__(
        self,
        group: None = None,
        target: Callable[..., object] | None = None,
        name: str | None = None,
        args: Iterable[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        daemon: bool | None = None,
        context: contextvars.Context | None = None,
    ) -> None:
        """Same as :class:`threading.Thread` plus ``context`` (as on 3.14's ``Thread``).

        ``context=None`` copies the creator's context at construction time.
        An explicit :class:`contextvars.Context` is used as given: ``run()``
        executes inside it, as on 3.14 (pass ``contextvars.Context()`` for an
        empty context).
        """
        raise NotImplementedError

    def run(self) -> None:
        raise NotImplementedError
