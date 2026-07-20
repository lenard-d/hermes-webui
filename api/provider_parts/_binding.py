"""Compatibility binding for extracted :mod:`api.providers` modules.

Provider helpers historically resolve collaborators through the
``api.providers`` module namespace.  Focused tests and downstream integrations
rely on that seam when they monkeypatch credential readers, clocks, locks, or
network clients.  Rebinding extracted functions to the facade globals keeps
those patches observable without adding pass-through wrappers.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import types
from types import ModuleType


def _bind_function(function, facade_globals: dict):
    wrapped = getattr(function, "__wrapped__", None)
    is_contextmanager = wrapped is not None and inspect.isgeneratorfunction(wrapped)
    if is_contextmanager:
        rebound_generator = _bind_function(wrapped, facade_globals)
        # ``update_wrapper`` always adds a link to the original function.  The
        # context-manager wrapper must instead unwrap to the rebound generator,
        # whose globals are the compatibility facade.
        rebound_generator.__dict__.pop("__wrapped__", None)
        rebound = contextlib.contextmanager(rebound_generator)
    else:
        rebound = types.FunctionType(
            function.__code__,
            facade_globals,
            name=function.__name__,
            argdefs=function.__defaults__,
            closure=function.__closure__,
        )
        rebound.__kwdefaults__ = function.__kwdefaults__
        rebound.__annotations__ = function.__annotations__
    functools.update_wrapper(rebound, function)
    if is_contextmanager:
        rebound.__wrapped__ = rebound_generator
    rebound.__module__ = facade_globals["__name__"]
    return rebound


def install_provider_part(facade_globals: dict, part: ModuleType) -> None:
    """Install one cohesive provider module into the compatibility facade."""
    for name in part.__provider_exports__:
        value = getattr(part, name)
        if isinstance(value, types.FunctionType):
            value = _bind_function(value, facade_globals)
        facade_globals[name] = value
