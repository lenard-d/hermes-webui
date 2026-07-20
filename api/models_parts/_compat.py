"""Compatibility plumbing for the split :mod:`api.models` implementation.

The former monolith exposed every helper and dependency from one mutable module
namespace. Tests and runtime adapters intentionally patch those names. Each
implementation module is therefore seeded with the facade namespace, and the
facade mirrors later assignments back into every part.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any


_FACADE_NAME = "api.models"
_MODULE_METADATA = frozenset({
    "__builtins__", "__cached__", "__doc__", "__file__", "__loader__",
    "__name__", "__package__", "__spec__",
})


def seed_module_globals(namespace: dict[str, Any]) -> None:
    """Seed a part with names already published by the models facade."""
    facade = sys.modules.get(_FACADE_NAME)
    if facade is not None:
        for name, value in vars(facade).items():
            if name not in _MODULE_METADATA and not name.startswith("_MODELS_FACADE_"):
                namespace[name] = value
    namespace["_publish_models_global"] = publish_global


def publish_global(namespace: dict[str, Any], name: str, value: Any) -> None:
    """Mirror a part's deliberate global rebinding through the facade."""
    namespace[name] = value
    facade = sys.modules.get(_FACADE_NAME)
    if facade is not None:
        setattr(facade, name, value)


def sync_facade_to_parts(facade: ModuleType, parts: tuple[ModuleType, ...]) -> None:
    """Give every part the final facade namespace without replacing owners."""
    exported = {
        name: getattr(facade, name)
        for name in getattr(facade, "_MODELS_FACADE_EXPORTS", ())
    }
    for part in parts:
        owned = frozenset(getattr(part, "__all__", ()))
        for name, value in exported.items():
            if name not in owned:
                vars(part)[name] = value


class ModelsFacade(ModuleType):
    """Module type that preserves assignments to historical patch seams."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name.startswith("_MODELS_FACADE_") or name in _MODULE_METADATA:
            return
        for part in self.__dict__.get("_MODELS_FACADE_PARTS", ()):
            if name in vars(part):
                vars(part)[name] = value

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        if name.startswith("_MODELS_FACADE_") or name in _MODULE_METADATA:
            return
        for part in self.__dict__.get("_MODELS_FACADE_PARTS", ()):
            vars(part).pop(name, None)
