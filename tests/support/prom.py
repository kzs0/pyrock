"""A tiny Prometheus text-format (0.0.4) parser for assertions.

.. code-block:: python

    metrics = parse(text)
    assert metrics.value("process_user_count", status="active") == 1
    assert metrics.value("http_request_count", {"http_method": "GET"}) == 3
    assert metrics.type("process_user_duration_ms") == "histogram"
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PromSample", "PromText", "parse"]

_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_SAMPLE_RE = re.compile(rf"^(?P<name>{_NAME})(?:\{{(?P<labels>.*)\}})?\s+(?P<rest>.+)$")
_LABEL_RE = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(,|$)')


@dataclass(frozen=True)
class PromSample:
    name: str
    labels: dict[str, str]
    value: float
    timestamp: int | None = None


def _unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_value(text: str) -> float:
    lowered = text.strip()
    if lowered in ("+Inf", "Inf"):
        return math.inf
    if lowered == "-Inf":
        return -math.inf
    if lowered == "NaN":
        return math.nan
    return float(lowered)


def _parse_labels(raw: str, line: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    pos = 0
    raw = raw.strip()
    while pos < len(raw):
        m = _LABEL_RE.match(raw, pos)
        if not m:
            raise ValueError(f"malformed labels in line: {line!r}")
        key, value = m.group(1), _unescape(m.group(2))
        if key in labels:
            raise ValueError(f"duplicate label {key!r} in line: {line!r}")
        labels[key] = value
        pos = m.end()
    return labels


@dataclass
class PromText:
    """Parsed exposition text."""

    samples: list[PromSample] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    helps: dict[str, str] = field(default_factory=dict)

    def names(self) -> set[str]:
        return {s.name for s in self.samples}

    def has(self, name: str) -> bool:
        return name in self.names() or name in self.types

    def type(self, family: str) -> str | None:
        return self.types.get(family)

    def help(self, family: str) -> str | None:
        return self.helps.get(family)

    def find(
        self, name: str, labels: dict[str, str] | None = None, /, **kw: str
    ) -> list[PromSample]:
        """Samples named ``name`` whose labels include every given label (subset match)."""
        want = {**(labels or {}), **kw}
        return [
            s
            for s in self.samples
            if s.name == name and all(s.labels.get(k) == v for k, v in want.items())
        ]

    def value(self, name: str, labels: dict[str, str] | None = None, /, **kw: str) -> float | None:
        """The value of the single matching sample, ``None`` if none.

        Raises ``AssertionError`` when several samples match (add labels).
        """
        found = self.find(name, labels, **kw)
        if not found:
            return None
        if len(found) > 1:
            raise AssertionError(
                f"{len(found)} samples match {name} {labels or {}} {kw}: "
                f"{[s.labels for s in found]}"
            )
        return found[0].value

    def label_sets(self, name: str) -> list[dict[str, str]]:
        return [s.labels for s in self.samples if s.name == name]

    def __contains__(self, name: Any) -> bool:
        return isinstance(name, str) and self.has(name)


def parse(text: str | bytes) -> PromText:
    """Parse exposition text. Raises ``ValueError`` on malformed lines."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    result = PromText()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[1] == "TYPE":
                result.types[parts[2]] = parts[3].strip() if len(parts) > 3 else ""
            elif len(parts) >= 3 and parts[1] == "HELP":
                result.helps[parts[2]] = _unescape(parts[3]) if len(parts) > 3 else ""
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            raise ValueError(f"malformed sample line: {line!r}")
        labels = _parse_labels(m.group("labels") or "", line)
        rest = m.group("rest").split()
        if len(rest) not in (1, 2):
            raise ValueError(f"malformed sample value: {line!r}")
        ts = int(rest[1]) if len(rest) == 2 else None
        result.samples.append(PromSample(m.group("name"), labels, _parse_value(rest[0]), ts))
    return result
