"""The installed pyrock version (kept in sync with ``pyproject.toml``)."""

from __future__ import annotations

__all__ = ["SCOPE_NAME", "__version__"]

__version__ = "0.1.0.dev0"

SCOPE_NAME = "pyrock"
"""The OTLP instrumentation scope name on every export (with :data:`__version__`)."""
