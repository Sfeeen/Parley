"""Parley client: enrolment, the live event stream, workspace sync, pigeonhole.

Imports are lazy (PEP 562 module ``__getattr__``). That is not premature
optimisation: ``parley doctor`` and the Hub both want to import *something* from
this package without dragging in the whole client, and a CLI that pays for the
sync engine just to print ``--help`` feels slow for no reason.

    from parley.client import ParleyClient      # works
    from parley.client.sync import WorkspaceSync  # also works, nothing extra loaded
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "ParleyClient",
    "Transport",
    "SSEParser",
    "IgnoreRules",
    "WorkspaceSync",
    "Pigeonhole",
    "Runtime",
    "hub_hello",
]

_EXPORTS = {
    "ParleyClient": ("parley.client.client", "ParleyClient"),
    "hub_hello": ("parley.client.client", "hub_hello"),
    "Transport": ("parley.client.transport", "Transport"),
    "SSEParser": ("parley.client.transport", "SSEParser"),
    "IgnoreRules": ("parley.client.ignore", "IgnoreRules"),
    "WorkspaceSync": ("parley.client.sync", "WorkspaceSync"),
    "Pigeonhole": ("parley.client.pigeonhole", "Pigeonhole"),
    "Runtime": ("parley.client.runtime", "Runtime"),
}

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .client import ParleyClient, hub_hello
    from .ignore import IgnoreRules
    from .pigeonhole import Pigeonhole
    from .runtime import Runtime
    from .sync import WorkspaceSync
    from .transport import SSEParser, Transport


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError("module {0!r} has no attribute {1!r}".format(__name__, name))
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache so the lazy path runs once
    return value


def __dir__() -> list:
    return sorted(set(list(globals().keys()) + __all__))
