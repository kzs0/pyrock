"""Context variables and helpers that carry context across threads.

pyrock keeps no process-wide state: the active instance, operation, step,
source and the no-trace flag live in :mod:`contextvars`. asyncio needs nothing
extra (tasks copy the context, and ``asyncio.to_thread`` does too). Threads
start with an empty context unless the interpreter inherits it
(``sys.flags.thread_inherit_context``, the default on free-threaded 3.14), so
pyrock provides explicit helpers: :func:`bind`, :class:`ContextThreadPoolExecutor`
and :class:`Thread`.
"""

from __future__ import annotations

import contextvars
import functools
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
"""The active live instance (never the noop: deactivated means ``None``)."""

operation_var: contextvars.ContextVar[Operation | None] = contextvars.ContextVar(
    "pyrock.operation", default=None
)
"""The innermost operation."""

step_var: contextvars.ContextVar[Step | None] = contextvars.ContextVar("pyrock.step", default=None)
"""The innermost step *inside* the innermost operation.

Entering an operation clears it, so a non-``None`` value always means the step
is the innermost scope (what ``pyrock.register()`` targets).
"""

source_var: contextvars.ContextVar[Source | None] = contextvars.ContextVar(
    "pyrock.source", default=None
)
"""The innermost source; it prefixes every operation opened beneath it."""

no_trace_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pyrock.no_trace", default=False
)
"""Set by ``no_trace=True`` scopes and inherited by everything below them."""


@overload
def bind(fn: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def bind(fn: Callable[..., R], /, *args: Any, **kwargs: Any) -> Callable[..., R]: ...


def bind(fn: Callable[..., R], /, *args: Any, **kwargs: Any) -> Callable[..., R]:
    """Capture the current context now; run ``fn`` inside it later.

    ``bind(fn)`` returns a wrapper with ``fn``'s signature. ``bind(fn, *args,
    **kwargs)`` pre-binds arguments like :func:`functools.partial`. Each call
    runs in a fresh copy of the captured context, so concurrent calls do not
    interfere and nothing a call activates leaks into the next one.
    """
    captured = contextvars.copy_context()
    if not args and not kwargs:

        @functools.wraps(fn)
        def bound(*call_args: Any, **call_kwargs: Any) -> R:
            return captured.copy().run(fn, *call_args, **call_kwargs)

        return bound

    def partial(*call_args: Any, **call_kwargs: Any) -> R:
        return captured.copy().run(fn, *args, *call_args, **{**kwargs, **call_kwargs})

    return partial


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """A :class:`ThreadPoolExecutor` that copies the caller's context on every submit.

    :meth:`map` goes through :meth:`submit`, so it copies context too.
    """

    def submit(self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        context = contextvars.copy_context()
        return super().submit(context.run, fn, *args, **kwargs)


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
        super().__init__(group, target, name, args, kwargs, daemon=daemon)
        self._pyrock_context = contextvars.copy_context() if context is None else context

    def run(self) -> None:
        self._pyrock_context.run(super().run)
