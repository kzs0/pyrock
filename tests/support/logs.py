"""Capture pyrock JSON logs written to a stream and parse them.

.. code-block:: python

    cap = LogCapture()
    with pyrock.init(service="t", log_output=cap.stream, server_enabled=False):
        pyrock.info("hello", user_id="1")
    rec = cap.find(msg="hello")
    assert rec["user_id"] == "1"

``LogCapture.stream`` is a thread-safe ``io.StringIO``. Non-JSON lines raise
``AssertionError`` from :meth:`LogCapture.records` (use ``strict=False`` to
skip them).
"""

from __future__ import annotations

import io
import json
import threading
from typing import Any

__all__ = ["LockedStringIO", "LogCapture", "parse_json_lines"]


class LockedStringIO(io.StringIO):
    """A ``StringIO`` whose writes and reads are serialized with a lock."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            return super().write(s)

    def getvalue(self) -> str:
        with self._lock:
            return super().getvalue()

    def reset(self) -> None:
        with self._lock:
            self.seek(0)
            self.truncate(0)


def parse_json_lines(text: str, *, strict: bool = True) -> list[dict[str, Any]]:
    """Parse one JSON object per non-empty line."""
    out: list[dict[str, Any]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if strict:
                raise AssertionError(f"log line {n} is not JSON: {line!r}") from exc
            continue
        if not isinstance(value, dict):
            if strict:
                raise AssertionError(f"log line {n} is not a JSON object: {line!r}")
            continue
        out.append(value)
    return out


def _matches(record: dict[str, Any], filters: dict[str, Any]) -> bool:
    return all(record.get(k, _MISSING) == v for k, v in filters.items())


_MISSING = object()


class LogCapture:
    """Owns a stream to pass as ``log_output=``; parses what was written to it."""

    def __init__(self) -> None:
        self.stream = LockedStringIO()

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def lines(self) -> list[str]:
        return [ln for ln in self.text.splitlines() if ln.strip()]

    def records(self, *, strict: bool = True) -> list[dict[str, Any]]:
        return parse_json_lines(self.text, strict=strict)

    def find_all(self, **filters: Any) -> list[dict[str, Any]]:
        """Records whose keys equal every filter (``msg="x"``; use a dict for dotted keys)."""
        return [r for r in self.records() if _matches(r, filters)]

    def find(self, _filters: dict[str, Any] | None = None, /, **filters: Any) -> dict[str, Any]:
        """The first matching record; ``AssertionError`` listing messages if none."""
        merged = {**(_filters or {}), **filters}
        for record in self.records():
            if _matches(record, merged):
                return record
        msgs = [r.get("msg") for r in self.records()]
        raise AssertionError(f"no log record matches {merged!r}; messages: {msgs!r}")

    def messages(self) -> list[str]:
        return [str(r.get("msg", "")) for r in self.records(strict=False)]

    def clear(self) -> None:
        self.stream.reset()
