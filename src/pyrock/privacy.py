"""Data policy and its enforcement walker (Go: ``privacy.Policy``).

One walker enforces the policy on span attributes, events, status messages,
log records (including ``extra`` and nested dicts/lists), metric label values
and profile labels:

* Traversal is bounded to :data:`MAX_NODES` nodes. Cycles and repeated aliases
  are replaced with :data:`CYCLE_MARKER` / :data:`ALIAS_MARKER`.
* ``redact`` receives qualified paths (``"request.headers.authorization"``),
  must be thread-safe, and any exception it raises drops the value (fail
  closed).
* The ``error.*`` and ``exception.*`` namespaces stay private through group
  ancestry unless ``allow_error_details``.
* Unknown object types are suppressed; ``str()``/``repr()`` are never called on
  arbitrary objects.
* Values rejected by the allowlist, by ``redact`` or for their type are
  omitted; sensitive keys (``password``, ``authorization``, ``cookie``,
  ``*token``...) and private error details become ``"[redacted]"``. Strings
  over ``max_string_length`` and containers over the node budget or nested
  deeper than 32 levels end in ``"[truncated]"``.
* Resource keys that identify a host, process, container or k8s object are
  dropped unless ``resource_keys`` allowlists them.
* Sensitive HTTP attributes (``http.user_agent``, and the raw outgoing
  ``http.target`` / ``http.url``) are emitted only when ``attribute_keys``
  allowlists them (exact key or a ``"http.*"`` wildcard), even when
  ``attribute_keys`` is ``None``. URLs always lose credentials, query and
  fragment (:func:`sanitize_url`).

This is a leaf module: it imports only the :mod:`pyrock._compat` utilities.
"""

from __future__ import annotations

import datetime
import enum
import functools
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from pyrock._compat import MISSING_LABEL, is_nonempty_str, is_positive_int

__all__ = [
    "ALIAS_MARKER",
    "CYCLE_MARKER",
    "DEFAULT_MAX_STRING_LENGTH",
    "MAX_NODES",
    "MAX_PROFILE_LABELS",
    "MAX_PROFILE_LABEL_DEPTH",
    "REDACTED_MARKER",
    "Policy",
    "Redactor",
    "sanitize_url",
]

MAX_NODES = 4096
CYCLE_MARKER = "[cycle]"
ALIAS_MARKER = "[alias]"
REDACTED_MARKER: Final = "[redacted]"
"""Replaces sensitive values and private error details (also used by :mod:`pyrock.logs`)."""
DEFAULT_MAX_STRING_LENGTH = 1024
MAX_PROFILE_LABELS = 64
MAX_PROFILE_LABEL_DEPTH = 8

Redactor = Callable[[str, Any], tuple[Any, bool]]
"""``redact(path, value) -> (new_value, keep)``."""


def _key_tuple(value: Iterable[str] | None, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError(f"{name} must be an iterable of keys, not a single string")
    keys = tuple(value)
    for key in keys:
        if not is_nonempty_str(key):
            raise TypeError(f"{name} entries must be non-empty strings")
    return keys


# -- path rules ----------------------------------------------------------------

_TRUNCATED: Final = "[truncated]"
_INVALID_URL: Final = "[invalid URL]"
_MAX_DEPTH: Final = 32
"""Containers nested deeper than this become ``"[truncated]"``, which keeps
every encoder's own recursion shallow."""

_SOURCE_PATH: Final = 1  # needs allow_source_paths
_SENSITIVE: Final = 2  # always redacted
_ERROR_DETAIL: Final = 4  # needs allow_error_details
_ERROR_SUMMARY: Final = 8  # a flat error summary key; private only as a group
_RAW_HTTP: Final = 16  # emitted only when attribute_keys allowlists it
_URL: Final = 32  # string values are sanitized

_SENSITIVE_RE: Final = re.compile(
    r"password|passwd|authorization|cookie|api[-_]?key|secret|access_token|refresh_token"
    r"|(?:^|\.)token(?:\.|$)"
)
_SOURCE_PATH_RE: Final = re.compile(
    r"(?:^|\.)(?:error\.stack|exception\.stacktrace|code\.filepath|code\.file\.path)(?:\.|$)"
)
_ERROR_NAMESPACES: Final = frozenset({"error", "exception"})
_ERROR_SUMMARY_KEYS: Final = frozenset(
    {"error", "error.type", "error.fingerprint", "error.chain", "error.types", "error.group.count"}
)
_RAW_HTTP_KEYS: Final = ("http.user_agent", "http.target", "http.url")
_URL_KEYS: Final = ("http.target", "http.url", "url.full")
_INFRASTRUCTURE_PREFIXES: Final = ("host.", "process.", "k8s.", "container.")


def _qualified(path: str, keys: tuple[str, ...]) -> bool:
    return any(path == key or path.endswith("." + key) for key in keys)


@functools.lru_cache(maxsize=4096)
def _classify(path: str) -> int:
    """The policy-independent rule flags for a qualified attribute path."""
    lower = path.lower()
    flags = 0
    if _SOURCE_PATH_RE.search(lower):
        flags |= _SOURCE_PATH
    if _SENSITIVE_RE.search(lower):
        flags |= _SENSITIVE
    segments = lower.split(".")
    for index, segment in enumerate(segments):
        if segment in _ERROR_NAMESPACES:
            tail = ".".join(segments[index:])
            flags |= _ERROR_SUMMARY if tail in _ERROR_SUMMARY_KEYS else _ERROR_DETAIL
            break
    if _qualified(lower, _RAW_HTTP_KEYS):
        flags |= _RAW_HTTP
    if _qualified(lower, _URL_KEYS):
        flags |= _URL
    return flags


class _Match(enum.Enum):
    """How an allowlist admits a path."""

    NONE = 0
    PARTIAL = 1  # an ancestor of an entry: only a group, whose children are filtered
    FULL = 2  # an entry or below one: admitted with all its children


def _match(keys: tuple[str, ...], path: str) -> _Match:
    """Match ``path`` against allowlist entries (exact keys, groups, ``group.*``)."""
    partial = False
    for entry in keys:
        group = entry[:-2] if entry.endswith(".*") else None
        if group is not None:
            if path.startswith(group + "."):
                return _Match.FULL
        elif path == entry or path.startswith(entry + "."):
            return _Match.FULL
        if entry.startswith(path + "."):
            partial = True
    return _Match.PARTIAL if partial else _Match.NONE


# -- walker ----------------------------------------------------------------


class _Drop(enum.Enum):
    DROP = 0


_DROP: Final = _Drop.DROP
"""Walker verdict: omit the value (and its key)."""

_END: Final = object()

_CONTAINER_TYPES: Final = frozenset({dict, list, tuple})
_SCALAR_TYPES: Final = frozenset(
    {str, bool, int, float, bytes, type(None), datetime.datetime, datetime.timedelta}
)
"""Exact types the walker returns unchanged at an unflagged path (``str`` and
``bytes`` only up to ``max_string_length``)."""


def _is_container(value: object) -> bool:
    tp = type(value)
    if tp in _CONTAINER_TYPES:
        return True
    if tp in _SCALAR_TYPES:
        return False
    return isinstance(value, Mapping | list | tuple)


class _Frame:
    """An open container on the walker's explicit stack."""

    __slots__ = ("depth", "filtered", "ident", "items", "mode", "out", "path")

    def __init__(
        self, path: str, items: Iterator[Any], out: dict[str, Any] | list[Any], mode: _Match
    ) -> None:
        self.path = path
        self.items = items
        self.out = out
        self.mode = mode
        self.filtered = mode is _Match.PARTIAL and type(out) is dict
        self.depth = 0
        self.ident: int | None = None

    def add(self, key: str, value: Any) -> None:
        out = self.out
        if isinstance(out, dict):
            out[key] = value
        else:
            out.append(value)


class _Walk:
    """One bounded, iterative traversal. Never shared: all state is per call."""

    __slots__ = ("active", "exhausted", "max_depth", "policy", "remaining", "seen")

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.remaining = MAX_NODES
        self.exhausted = False
        self.max_depth = 0
        # Created on the first container, so scalar-only calls stay cheap.
        self.seen: dict[int, object] | None = None  # id -> object (keeps ids unique)
        self.active: set[int] = set()  # ids of the containers being walked

    def top_level(self, path: str, attrs: Mapping[Any, Any], mode: _Match) -> dict[str, Any]:
        """Clean a top-level mapping: no budget for the mapping itself, no marker at the end."""
        frame = self.open(path, attrs, mode, depth=0)
        if not isinstance(frame, _Frame):
            return {}
        self.drain(frame, top_level=True)
        out: dict[str, Any] = frame.out  # type: ignore[assignment]
        return out

    def value(self, path: str, value: Any, mode: _Match) -> Any:
        result = self.visit(path, value, mode, depth=1)
        if isinstance(result, _Frame):
            self.drain(result, top_level=False)
            return result.out
        return result

    def visit(self, path: str, value: Any, mode: _Match, depth: int) -> Any:
        """Clean one value: a final value, a marker, ``_DROP`` or a new ``_Frame``."""
        self.remaining -= 1
        self.max_depth = max(self.max_depth, depth)
        policy = self.policy
        container = _is_container(value)
        if mode is _Match.PARTIAL and not container:
            return _DROP
        flags = _classify(path)
        if flags:
            verdict = policy._verdict(path, flags, group=isinstance(value, Mapping))
            if verdict is not None:
                return verdict
        if policy.redact is not None:
            try:
                value, keep = policy.redact(path, value)
            except Exception:
                return _DROP  # fail closed
            if not keep:
                return _DROP
            container = _is_container(value)
            if mode is _Match.PARTIAL and not container:
                return _DROP
        if container:
            return self.open(path, value, mode, depth)
        return policy._scalar(flags, value)

    def open(self, path: str, value: Any, mode: _Match, depth: int) -> Any:
        """Start a container: a ``_Frame``, an empty result or a marker."""
        if depth > _MAX_DEPTH:
            return _TRUNCATED
        is_map = isinstance(value, Mapping)
        try:
            if not value:
                return {} if is_map else []
            items = iter(value.items()) if is_map else iter(value)
        except Exception:
            return _DROP  # a faulty user container
        frame = _Frame(path, items, {} if is_map else [], mode)
        frame.depth = depth
        if not isinstance(value, tuple):  # only mutable containers can form cycles
            if self.seen is None:
                self.seen = {}
            ident = id(value)
            if ident in self.active:
                return CYCLE_MARKER
            if ident in self.seen:
                return ALIAS_MARKER
            self.seen[ident] = value
            self.active.add(ident)
            frame.ident = ident
        return frame

    def drain(self, root: _Frame, *, top_level: bool) -> None:
        stack = [root]
        while stack:
            frame = stack[-1]
            item: Any
            try:
                item = next(frame.items, _END)
            except Exception:  # e.g. a dict mutated by another thread
                item = _END
            if item is _END:
                self.close(stack.pop())
                continue
            key = ""
            if isinstance(frame.out, dict):
                key, child = item
                if type(key) is not str:
                    if not isinstance(key, str):
                        continue  # non-string keys are suppressed, never rendered
                    key = str.__str__(key)
                path = f"{frame.path}.{key}" if frame.path else key
                mode = self.policy._admit(path) if frame.filtered else _Match.FULL
                if mode is _Match.NONE:
                    continue
            else:
                child, path, mode = item, frame.path, frame.mode
            if self.remaining <= 0:
                self.exhausted = True
                if top_level and frame is root:
                    break
                frame.add(key, _TRUNCATED)
                self.close(stack.pop())
                continue
            result = self.visit(path, child, mode, frame.depth + 1)
            if result is _DROP:
                continue
            if isinstance(result, _Frame):
                frame.add(key, result.out)
                stack.append(result)
            else:
                frame.add(key, result)

    def close(self, frame: _Frame) -> None:
        if frame.ident is not None:
            self.active.discard(frame.ident)


@dataclass(frozen=True, slots=True, kw_only=True)
class Policy:
    """What telemetry may contain.

    ``attribute_keys``: ``None`` keeps non-sensitive fields; a sequence is a
    strict allowlist (supports ``"group.*"`` wildcards).
    ``resource_keys``: resource attributes allowed through in addition to the
    always-safe ones (``service.name`` etc.).
    ``max_string_length``: strings longer than this are truncated.
    ``redact``: optional hook ``(path, value) -> (new_value, keep)``.
    """

    attribute_keys: tuple[str, ...] | None = None
    resource_keys: tuple[str, ...] = ()
    allow_error_details: bool = False
    allow_source_paths: bool = False
    allow_profile_labels: bool = False
    max_string_length: int = DEFAULT_MAX_STRING_LENGTH
    redact: Redactor | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        # Accept any iterable of keys (lists are the natural spelling) but store
        # tuples so the policy is immutable and safe to share across threads.
        object.__setattr__(
            self, "attribute_keys", _key_tuple(self.attribute_keys, "attribute_keys")
        )
        object.__setattr__(
            self, "resource_keys", _key_tuple(self.resource_keys, "resource_keys") or ()
        )
        if not is_positive_int(self.max_string_length):
            raise ValueError("max_string_length must be a positive int")
        if self.redact is not None and not callable(self.redact):
            raise TypeError("redact must be callable")

    # -- enforcement -------------------------------------------------------

    def apply(self, attrs: Mapping[str, Any], /, *, path: str = "") -> dict[str, Any]:
        """Return the attributes allowed by this policy (values cleaned recursively).

        ``path`` qualifies every key (``path + "." + key``) for the allowlist,
        the path rules and ``redact``. Input order is kept and the input is
        never mutated. Rejected values are omitted; sensitive and private ones
        become ``"[redacted]"``.
        """
        if (
            not path
            and type(attrs) is dict
            and self.attribute_keys is None
            and self.redact is None
            and (flat := self._apply_flat(attrs)) is not None
        ):
            return flat
        mode = self._root_mode(path)
        if mode is _Match.NONE:
            return {}
        return _Walk(self).top_level(path, attrs, mode)

    def value(self, path: str, value: Any, /) -> tuple[Any, bool]:
        """Clean one value at qualified ``path``; returns ``(value, keep)``."""
        if self.attribute_keys is None and self.redact is None and self._passes_flat(path, value):
            return value, True
        mode = self._root_mode(path)
        if mode is _Match.NONE:
            return None, False
        result = _Walk(self).value(path, value, mode)
        if result is _DROP:
            return None, False
        return result, True

    def resource(self, attrs: Mapping[str, Any], /) -> dict[str, Any]:
        """Filter resource attributes (identifying infrastructure keys need allowlisting).

        ``host.*``, ``process.*``, ``k8s.*`` and ``container.*`` keys pass only
        when ``resource_keys`` names them; every other key passes. Values are
        cleaned as in :meth:`apply`, but ``attribute_keys`` does not apply.
        """
        items: Iterable[tuple[object, Any]] = attrs.items()
        kept = {
            key: value
            for key, value in items
            if not (
                isinstance(key, str)  # other keys are suppressed by the walker
                and key.startswith(_INFRASTRUCTURE_PREFIXES)
                and _match(self.resource_keys, key) is not _Match.FULL
            )
        }
        return _Walk(self).top_level("", kept, _Match.FULL)

    def message(self, text: str, /) -> str:
        """Clean a free-form message (span status, log message)."""
        value: object = text
        if not isinstance(value, str):
            return ""
        return self._truncate(str.__str__(value))

    def label_value(self, key: str, value: str, /) -> str:
        """Clean a metric label value; disallowed values become ``"_"``."""
        cleaned, keep = self.value(key, value)
        return cleaned if keep and type(cleaned) is str else MISSING_LABEL

    def profile_labels(self, labels: Mapping[str, Any], /) -> dict[str, Any]:
        """Clean profile labels, raising ``ValueError`` above the size/depth limits.

        Labels are dropped entirely unless ``allow_profile_labels``. The limits
        (:data:`MAX_PROFILE_LABELS` items, :data:`MAX_NODES` nodes and
        :data:`MAX_PROFILE_LABEL_DEPTH` levels) apply after the policy.
        """
        if not self.allow_profile_labels:
            return {}
        walk = _Walk(self)
        mode = self._root_mode("")
        out = walk.top_level("", labels, mode)
        if len(out) > MAX_PROFILE_LABELS:
            raise ValueError(f"profile labels exceed {MAX_PROFILE_LABELS} items")
        if walk.exhausted:
            raise ValueError(f"profile labels exceed {MAX_NODES} nodes")
        if walk.max_depth > MAX_PROFILE_LABEL_DEPTH:
            raise ValueError(f"profile labels nest deeper than {MAX_PROFILE_LABEL_DEPTH}")
        return out

    # -- fast path -----------------------------------------------------------
    #
    # Most attribute sets are flat dicts of plain scalars under keys no path
    # rule matches. Without an allowlist or ``redact`` hook the walker returns
    # such values unchanged, so these helpers skip it. Anything else (a rule
    # match, a container, a subclass, an over-long value) returns ``None`` /
    # ``False`` and the caller runs the walker, which stays the single source
    # of the policy's semantics.

    def _passes_flat(self, key: str, value: Any) -> bool:
        """Whether the walker would keep ``value`` at ``key`` unchanged."""
        tp = type(value)
        if tp not in _SCALAR_TYPES or _classify(key):
            return False
        if tp is str or tp is bytes:
            return len(value) <= self.max_string_length
        return True

    def _apply_flat(self, attrs: dict[Any, Any]) -> dict[str, Any] | None:
        """``attrs`` copied when every item passes :meth:`_passes_flat`, else ``None``."""
        if len(attrs) > MAX_NODES:
            return None  # the walker truncates at the node budget
        out: dict[str, Any] = {}
        passes = self._passes_flat
        try:
            for key, value in attrs.items():
                if type(key) is not str or not passes(key, value):
                    return None
                out[key] = value
        except RuntimeError:  # mutated by another thread: let the walker cope
            return None
        return out

    # -- helpers used by the walker -----------------------------------------

    def _admit(self, path: str) -> _Match:
        keys = self.attribute_keys
        return _Match.FULL if keys is None else _match(keys, path)

    def _root_mode(self, path: str) -> _Match:
        if self.attribute_keys is not None and not path:
            return _Match.PARTIAL  # the root is a group; its keys are filtered
        return self._admit(path)

    def _verdict(self, path: str, flags: int, *, group: bool) -> str | _Drop | None:
        """Apply the path rules: a marker, ``_DROP``, or ``None`` to keep going.

        ``group`` is true for mappings: an error summary key holding a mapping
        is a private group, while a list such as ``error.chain`` stays public.
        """
        if flags & _SOURCE_PATH and not self.allow_source_paths:
            return REDACTED_MARKER
        if flags & _SENSITIVE:
            return REDACTED_MARKER
        if not self.allow_error_details and (
            flags & _ERROR_DETAIL or (flags & _ERROR_SUMMARY and group)
        ):
            return REDACTED_MARKER
        if flags & _RAW_HTTP and not (
            self.attribute_keys is not None and _match(self.attribute_keys, path) is _Match.FULL
        ):
            return _DROP
        return None

    def _scalar(self, flags: int, value: Any) -> Any:
        """Clean a non-container value; unknown types give ``_DROP`` unrendered."""
        tp = type(value)
        if tp is str:
            return self._text(flags, value)
        if value is None or tp is bool or tp is int or tp is float:
            return value
        if tp is bytes:
            return self._truncate_bytes(value)
        if tp is datetime.datetime or tp is datetime.timedelta:
            return value
        # Subclasses are reduced with base-class methods, so overrides never run.
        if isinstance(value, BaseException):
            return _DROP  # callers expand exceptions with attr.error()
        if isinstance(value, str):
            return self._text(flags, str.__str__(value))
        if isinstance(value, int):
            return int.__int__(value)
        if isinstance(value, float):
            return float.__float__(value)
        if isinstance(value, bytes):
            return self._truncate_bytes(bytes.__getitem__(value, slice(None)))
        if isinstance(value, datetime.datetime | datetime.timedelta):
            return value
        return _DROP

    def _text(self, flags: int, text: str) -> str:
        if flags & _URL:
            text = sanitize_url(text)
        return self._truncate(text)

    def _truncate(self, text: str) -> str:
        limit = self.max_string_length
        return text if len(text) <= limit else text[:limit] + _TRUNCATED

    def _truncate_bytes(self, data: bytes) -> bytes:
        limit = self.max_string_length
        return data if len(data) <= limit else data[:limit] + _TRUNCATED.encode()


def sanitize_url(url: str, /, *, keep_path: bool = True) -> str:
    """Strip credentials, query and fragment from ``url`` (and the path unless ``keep_path``).

    Never raises: an unparseable URL becomes ``"[invalid URL]"``.
    """
    import urllib.parse  # deferred: keeps ``import pyrock`` fast (it pulls in ipaddress)

    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # validates the port
    except ValueError:
        return _INVALID_URL
    host = parts.netloc.rpartition("@")[2]
    path = parts.path if keep_path else ""
    return urllib.parse.urlunsplit((parts.scheme, host, path, "", ""))
