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
* Resource keys that identify a host, process, container or k8s object are
  dropped unless ``resource_keys`` allowlists them.
* Sensitive HTTP attributes (``http.user_agent``, and the raw outgoing
  ``http.target`` / ``http.url``) are emitted only when ``attribute_keys``
  allowlists them (exact key or a ``"http.*"`` wildcard), even when
  ``attribute_keys`` is ``None``. URLs always lose credentials, query and
  fragment (:func:`sanitize_url`).

This is a leaf module: it imports nothing from pyrock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ALIAS_MARKER",
    "CYCLE_MARKER",
    "DEFAULT_MAX_STRING_LENGTH",
    "MAX_NODES",
    "MAX_PROFILE_LABELS",
    "MAX_PROFILE_LABEL_DEPTH",
    "Policy",
    "Redactor",
    "sanitize_url",
]

MAX_NODES = 4096
CYCLE_MARKER = "[cycle]"
ALIAS_MARKER = "[alias]"
DEFAULT_MAX_STRING_LENGTH = 1024
MAX_PROFILE_LABELS = 64
MAX_PROFILE_LABEL_DEPTH = 8

Redactor = Callable[[str, Any], tuple[Any, bool]]
"""``redact(path, value) -> (new_value, keep)``."""


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _key_tuple(value: Iterable[str] | None, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError(f"{name} must be an iterable of keys, not a single string")
    keys = tuple(value)
    for key in keys:
        if not _is_nonempty_str(key):
            raise TypeError(f"{name} entries must be non-empty strings")
    return keys


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
        if not _is_positive_int(self.max_string_length):
            raise ValueError("max_string_length must be a positive int")
        if self.redact is not None and not callable(self.redact):
            raise TypeError("redact must be callable")

    # -- enforcement -------------------------------------------------------

    def apply(self, attrs: Mapping[str, Any], /, *, path: str = "") -> dict[str, Any]:
        """Return the attributes allowed by this policy (values cleaned recursively)."""
        raise NotImplementedError

    def value(self, path: str, value: Any, /) -> tuple[Any, bool]:
        """Clean one value at qualified ``path``; returns ``(value, keep)``."""
        raise NotImplementedError

    def resource(self, attrs: Mapping[str, Any], /) -> dict[str, Any]:
        """Filter resource attributes (identifying infrastructure keys need allowlisting)."""
        raise NotImplementedError

    def message(self, text: str, /) -> str:
        """Clean a free-form message (span status, log message)."""
        raise NotImplementedError

    def label_value(self, key: str, value: str, /) -> str:
        """Clean a metric label value; disallowed values become ``"_"``."""
        raise NotImplementedError

    def profile_labels(self, labels: Mapping[str, Any], /) -> dict[str, Any]:
        """Clean profile labels, raising ``ValueError`` above the size/depth limits."""
        raise NotImplementedError


def sanitize_url(url: str, /, *, keep_path: bool = True) -> str:
    """Strip credentials, query and fragment from ``url`` (and the path unless ``keep_path``)."""
    raise NotImplementedError
