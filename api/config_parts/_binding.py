"""Compatibility binding for cohesive :mod:`api.config` implementation modules.

Config helpers historically resolve mutable state and collaborators through the
``api.config`` module namespace. Rebinding extracted function code to that
namespace preserves global mutations, object identity, and monkeypatch seams
without introducing pass-through wrappers.
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


def install_config_part(facade_globals: dict, part: ModuleType) -> None:
    """Install one config owner's declared interface into the public facade."""
    for name in part.__config_exports__:
        value = getattr(part, name)
        if isinstance(value, types.FunctionType):
            value = _bind_function(value, facade_globals)
        facade_globals[name] = value
