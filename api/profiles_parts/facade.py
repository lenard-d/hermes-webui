"""Binding helpers for the :mod:`api.profiles` compatibility facade."""

import contextlib
import functools
import inspect
import types
from collections.abc import Callable
from contextvars import ContextVar
from types import ModuleType


_profiles_api_resolver: Callable[[], ModuleType] | None = None
_active_profiles_api: ContextVar[ModuleType | None] = ContextVar(
    "active_profiles_api", default=None
)


def bind_profiles_api(resolver: Callable[[], ModuleType]) -> None:
    """Bind implementation modules to the live compatibility facade."""
    global _profiles_api_resolver
    _profiles_api_resolver = resolver


def profiles_api() -> ModuleType:
    """Return the facade so historical monkeypatch seams remain observable."""
    active = _active_profiles_api.get()
    if active is not None:
        return active
    if _profiles_api_resolver is None:
        raise RuntimeError("profile parts are not bound to the profiles facade")
    return _profiles_api_resolver()


def bind_profile_function(facade: ModuleType, implementation: Callable) -> Callable:
    """Rebind an extracted implementation to its exporting facade.

    A pass-through wrapper would retain this helper module as ``__globals__``
    while ``functools.wraps`` advertised the implementation module.  That split
    identity breaks introspection, monkeypatch seams, and pickle's module/name
    lookup.  A real function rebound to the facade globals has one authoritative
    identity and naturally remains isolated when tests load multiple fresh
    ``api.profiles`` module objects.

    Context-manager decorators need one extra step: their wrapper closes over
    the original generator.  Rebuild the decorator around a rebound generator
    so ``inspect.unwrap`` also terminates in the facade namespace.
    """
    facade_globals = vars(facade)
    wrapped = getattr(implementation, "__wrapped__", None)
    is_contextmanager = wrapped is not None and inspect.isgeneratorfunction(wrapped)
    if is_contextmanager:
        rebound_generator = bind_profile_function(facade, wrapped)
        rebound_generator.__dict__.pop("__wrapped__", None)
        rebound = contextlib.contextmanager(rebound_generator)
    else:
        rebound = types.FunctionType(
            implementation.__code__,
            facade_globals,
            name=implementation.__name__,
            argdefs=implementation.__defaults__,
            closure=implementation.__closure__,
        )
        rebound.__kwdefaults__ = implementation.__kwdefaults__
        rebound.__annotations__ = implementation.__annotations__

    functools.update_wrapper(rebound, implementation)
    if is_contextmanager:
        rebound.__wrapped__ = rebound_generator
    else:
        # update_wrapper adds a link back to the part implementation even when
        # it had no decorator chain.  Remove that false ownership trail so
        # inspect.unwrap() returns the facade-bound function.
        rebound.__dict__.pop("__wrapped__", None)
    rebound.__module__ = facade.__name__
    return rebound
