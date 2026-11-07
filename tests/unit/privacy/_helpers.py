"""Small helpers shared by the privacy unit tests."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

SECRET = "hunter2-very-secret"


class Opaque:
    """An arbitrary object whose rendering would leak; it records any attempt."""

    def __init__(self) -> None:
        self.rendered = threading.Event()

    def __repr__(self) -> str:
        self.rendered.set()
        return f"Opaque({SECRET})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        self.rendered.set()
        return SECRET


def walk(value: Any) -> Iterator[Any]:
    """Yield every key and value in a cleaned (acyclic) output structure."""
    stack = [value]
    while stack:
        item = stack.pop()
        yield item
        if isinstance(item, dict):
            for key, child in item.items():
                yield key
                stack.append(child)
        elif isinstance(item, list | tuple):
            stack.extend(item)


def leaks(value: Any, needle: str = SECRET) -> bool:
    """Whether ``needle`` appears in any string or bytes inside ``value``."""
    for item in walk(value):
        if isinstance(item, str) and needle in item:
            return True
        if isinstance(item, bytes) and needle.encode() in item:
            return True
    return False


def contains_instance(value: Any, tp: type) -> bool:
    return any(isinstance(item, tp) for item in walk(value))


def count_nodes(value: Any) -> int:
    """Count values (not keys) in a cleaned output structure."""
    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        total += 1
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return total
