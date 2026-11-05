"""pyrock: opinionated, zero-dependency observability built around operations.

.. code-block:: python

    import pyrock

    with pyrock.init(service="users-api"):
        with pyrock.operation("process_user", attrs={"user_id": "123"},
                              metric_labels=["status"]) as op:
            op.register(status="active")

Importing ``pyrock`` is cheap: profiling, cloud, gRPC, the server, the HTTP
client and the WSGI/ASGI middleware load on first use.

``pyrock.log`` and ``pyrock.cloud`` are both functions and submodules: the
modules are callable, so ``pyrock.log(logging.INFO, "msg")`` and
``pyrock.log.CorrelationFilter`` both work. ``pyrock.operation`` is the
function; the classes of :mod:`pyrock.operation` are re-exported here.
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
    register,
    track_loop,
    warn,
    warning,
)
from pyrock._compat import make_module_callable
from pyrock._context import ContextThreadPoolExecutor, Thread, bind
from pyrock._version import __version__ as __version__
from pyrock.attr import AttrValue, Event
from pyrock.config import Config, ConfigError

# Bound after pyrock.operation (the submodule) is loaded, so the name is the function.
from pyrock.operation import (
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
from pyrock.trace import SpanContext, SpanKind

if TYPE_CHECKING:
    from pyrock._bedrock import log as log
    from pyrock.cloud import CloudConfig as CloudConfig
    from pyrock.cloud import cloud as cloud
else:
    from pyrock import _bedrock as _bedrock_module
    from pyrock import log

    make_module_callable("pyrock.log", _bedrock_module.log)
    del _bedrock_module

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

# name -> (module, attribute or None for the module itself)
_LAZY: dict[str, tuple[str, str | None]] = {
    "CloudConfig": ("pyrock.cloud", "CloudConfig"),
    "cloud": ("pyrock.cloud", None),
    "asgi": ("pyrock.asgi", None),
    "env": ("pyrock.env", None),
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
