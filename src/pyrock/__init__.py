"""pyrock: opinionated, zero-dependency observability built around operations.

.. code-block:: python

    import pyrock

    with pyrock.init(service="users-api"):
        with pyrock.operation("process_user", attrs={"user_id": "123"},
                              metric_labels=["status"]) as op:
            op.register(status="active")

Importing ``pyrock`` is cheap: profiling, cloud, gRPC, the server, the HTTP
client and the WSGI/ASGI middleware load on first use.

Logging integration classes (``BedrockHandler``, ``CorrelationFilter``, ...)
live in :mod:`pyrock.logs`; ``pyrock.log(level, msg, ...)`` is the helper.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from pyrock._bedrock import (
    Bedrock,
    counter,
    current,
    current_operation,
    current_span,
    current_span_context,
    debug,
    error,
    gauge,
    histogram,
    info,
    init,
    inject_headers,
    log,
    register,
    track_loop,
    warn,
    warning,
)
from pyrock._context import Thread, bind
from pyrock._operation import (
    Activation as Activation,  # re-exported outside the __all__ contract
)
from pyrock._operation import (
    Operation,
    OperationScope,
    Source,
    SourceScope,
    Step,
    StepScope,
    operation,
    source,
    start_operation,
    step,
)
from pyrock._version import __version__ as __version__
from pyrock.attr import AttrValue, Event
from pyrock.config import Config, ConfigError
from pyrock.metric import MetricConflictError as MetricConflictError
from pyrock.metric import MetricLimitError as MetricLimitError
from pyrock.trace import SpanContext, SpanKind

if TYPE_CHECKING:
    # Lazily loaded on first attribute access (see __getattr__ below).
    from pyrock._cloud import CloudConfig, cloud
    from pyrock._context import ContextThreadPoolExecutor

__all__ = [
    "AttrValue",
    "Bedrock",
    "CloudConfig",
    "Config",
    "ConfigError",
    "ContextThreadPoolExecutor",
    "Event",
    "Operation",
    "OperationScope",
    "Source",
    "SourceScope",
    "SpanContext",
    "SpanKind",
    "Step",
    "StepScope",
    "Thread",
    "bind",
    "cloud",
    "counter",
    "current",
    "current_operation",
    "current_span",
    "current_span_context",
    "debug",
    "error",
    "gauge",
    "histogram",
    "info",
    "init",
    "inject_headers",
    "log",
    "operation",
    "register",
    "source",
    "start_operation",
    "step",
    "track_loop",
    "warn",
    "warning",
]

# name -> (module, attribute or None for the module itself). ``env`` and ``logs``
# need no entry: the core imports them, which binds them as attributes.
_LAZY: dict[str, tuple[str, str | None]] = {
    "CloudConfig": ("pyrock._cloud", "CloudConfig"),
    "ContextThreadPoolExecutor": ("pyrock._context", "ContextThreadPoolExecutor"),
    "cloud": ("pyrock._cloud", "cloud"),
    "asgi": ("pyrock.asgi", None),
    "http": ("pyrock.http", None),
    "profile": ("pyrock.profile", None),
    "server": ("pyrock.server", None),
    "testing": ("pyrock.testing", None),
    "wsgi": ("pyrock.wsgi", None),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'pyrock' has no attribute {name!r}") from None
    module = importlib.import_module(module_name)
    value = module if attr_name is None else getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
