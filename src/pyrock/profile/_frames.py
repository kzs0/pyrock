"""Interning of live Python stacks into compact keys, and back into :class:`Frame` values.

Collectors aggregate samples by stack. Hashing tuples of :class:`Frame`
dataclasses on every tick would be slow, so a :class:`StackTable` keys stacks
as flat ``(id(code), line, id(code), line, ...)`` integer tuples and keeps a
strong reference to each code object it has seen, so an ``id`` can never be
reused while a key that contains it is alive. :meth:`StackTable.frames` turns a
key back into frames once, when a profile is built.

A table is not thread-safe; each owner guards it with its own lock.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import CodeType, FrameType

from pyrock.profile._pprof import Frame

__all__ = ["MAX_DEPTH", "StackKey", "StackTable"]

MAX_DEPTH = 128
"""Frames kept per stack (leaf first); deeper callers are cut off."""

StackKey = tuple[int, ...]
"""Alternating ``id(code)`` and line numbers, leaf first."""


class StackTable:
    """Maps live frames to hashable stack keys and keys to :class:`Frame` tuples."""

    __slots__ = ("_codes", "_frames")

    def __init__(self) -> None:
        self._codes: dict[int, tuple[CodeType, str]] = {}
        self._frames: dict[tuple[int, int], Frame] = {}

    def key(self, frame: FrameType | None, /) -> StackKey:
        """The key of ``frame`` and its callers (leaf first, at most :data:`MAX_DEPTH`)."""
        codes = self._codes
        out: list[int] = []
        depth = 0
        while frame is not None and depth < MAX_DEPTH:
            code = frame.f_code
            code_id = id(code)
            if code_id not in codes:
                codes[code_id] = (code, _module_name(frame))
            out.append(code_id)
            out.append(frame.f_lineno or 0)
            frame = frame.f_back
            depth += 1
        return tuple(out)

    def key_of(self, frames: Iterable[FrameType], /) -> StackKey:
        """The key of exactly ``frames`` (already leaf first), ignoring their callers."""
        codes = self._codes
        out: list[int] = []
        for frame in frames:
            code = frame.f_code
            code_id = id(code)
            if code_id not in codes:
                codes[code_id] = (code, _module_name(frame))
            out.append(code_id)
            out.append(frame.f_lineno or 0)
            if len(out) >= 2 * MAX_DEPTH:
                break
        return tuple(out)

    def frames(self, key: StackKey, /) -> tuple[Frame, ...]:
        """The frames of a key returned by this table."""
        cache = self._frames
        out: list[Frame] = []
        for i in range(0, len(key), 2):
            ident = (key[i], key[i + 1])
            frame = cache.get(ident)
            if frame is None:
                code, module = self._codes[key[i]]
                frame = Frame(code.co_qualname, code.co_filename, key[i + 1], module)
                cache[ident] = frame
            out.append(frame)
        return tuple(out)

    def clear(self) -> None:
        """Forget every code object (invalidates all keys handed out so far)."""
        self._codes.clear()
        self._frames.clear()


def _module_name(frame: FrameType) -> str:
    name = frame.f_globals.get("__name__")
    return name if isinstance(name, str) else ""
