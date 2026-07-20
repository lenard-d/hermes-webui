"""Instance-bound compatibility seam for the historical update facade."""

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from types import ModuleType
from typing import TypeVar


T = TypeVar("T")
_updates_api_resolver: Callable[[], ModuleType] | None = None
_active_updates_api: ContextVar[ModuleType | None] = ContextVar(
    "active_updates_api", default=None
)


def bind_updates_api(resolver: Callable[[], ModuleType]) -> None:
    """Bind the process fallback without replacing an established facade."""
    global _updates_api_resolver
    if _updates_api_resolver is None:
        _updates_api_resolver = resolver


def updates_api() -> ModuleType:
    """Return the facade instance owning the current update operation."""
    active = _active_updates_api.get()
    if active is not None:
        return active
    if _updates_api_resolver is None:
        raise RuntimeError("update owners are not bound to the updates facade")
    return _updates_api_resolver()


def bind_update_function(facade: ModuleType, implementation: Callable) -> Callable:
    """Bind one owner call, including nested calls, to its exporting facade."""

    @wraps(implementation)
    def bound(*args, **kwargs):
        token = _active_updates_api.set(facade)
        try:
            return implementation(*args, **kwargs)
        finally:
            _active_updates_api.reset(token)

    return bound


def facade_attr(name: str, default: T) -> T:
    """Return the owning facade's current attribute, or the owner default."""
    try:
        facade = updates_api()
    except RuntimeError:
        return default
    return getattr(facade, name, default)
