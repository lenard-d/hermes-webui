"""Compatibility binding for incrementally extracting ``api.routes`` domains.

Route helpers historically resolve every dependency through the ``api.routes``
module namespace.  Focused tests and downstream integrations rely on that seam
when they monkeypatch imported helpers.  Rebinding an extracted function's code
to the facade globals preserves that behavior while allowing its implementation
to live in a cohesive module.
"""

from __future__ import annotations

import functools
import types
from types import ModuleType


def _bind_function(function, facade_globals: dict):
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
    rebound.__module__ = facade_globals["__name__"]
    return rebound


def install_routes_part(facade_globals: dict, part: ModuleType) -> None:
    """Install one part's declared exports into the canonical route facade."""
    for name in part.__routes_exports__:
        value = getattr(part, name)
        if isinstance(value, types.FunctionType):
            value = _bind_function(value, facade_globals)
        facade_globals[name] = value
