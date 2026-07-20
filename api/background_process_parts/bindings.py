"""Instance-bound access to a ``background_process`` compatibility facade."""

from __future__ import annotations

import sys
from contextvars import ContextVar
from functools import wraps
from importlib import import_module
from types import ModuleType
from typing import Any, Callable, Mapping


class _FacadeNamespace:
    """Attribute view over one facade module's live globals mapping."""

    def __init__(self, namespace: Mapping[str, Any]) -> None:
        self._namespace = namespace

    def __getattr__(self, name: str) -> Any:
        try:
            return self._namespace[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_BOUND_BACKGROUND_PROCESS_API: ContextVar[_FacadeNamespace | None] = ContextVar(
    "bound_background_process_api",
    default=None,
)


def bind_background_process_api(
    namespace: Mapping[str, Any],
    function: Callable[..., Any],
) -> Callable[..., Any]:
    """Bind an owner function to one facade instance's live namespace.

    A globals mapping is used instead of the canonical ``sys.modules`` entry:
    test/plugin loaders can execute multiple facade module objects from the
    same file, and monkeypatches on each object must remain isolated.
    """
    facade = _FacadeNamespace(namespace)

    @wraps(function)
    def _bound(*args: Any, **kwargs: Any) -> Any:
        token = _BOUND_BACKGROUND_PROCESS_API.set(facade)
        try:
            return function(*args, **kwargs)
        finally:
            _BOUND_BACKGROUND_PROCESS_API.reset(token)

    return _bound


def background_process_api() -> ModuleType | _FacadeNamespace:
    """Return the facade instance bound to the current owner-function call."""
    bound = _BOUND_BACKGROUND_PROCESS_API.get()
    if bound is not None:
        return bound
    module = sys.modules.get("api.background_process")
    if module is None:
        module = import_module("api.background_process")
    return module
