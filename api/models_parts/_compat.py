"""Compatibility plumbing for the split :mod:`api.models` implementation.

The former monolith exposed every helper and dependency from one mutable module
namespace. Tests and runtime adapters intentionally patch those names. Each
implementation module is therefore seeded with the facade namespace, and the
facade mirrors later assignments back into every part.
"""

from __future__ import annotations

import inspect
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
    """Give every part the final facade namespace and record stable bindings."""
    exported = {
        name: getattr(facade, name)
        for name in getattr(facade, "_MODELS_FACADE_EXPORTS", ())
    }
    bindings: dict[str, tuple[ModuleType, ...]] = {}
    for part in parts:
        owned = frozenset(getattr(part, "__all__", ()))
        for name, value in exported.items():
            if name not in owned:
                vars(part)[name] = value
    for name in exported:
        bindings[name] = tuple(part for part in parts if name in vars(part))
    vars(facade)["_MODELS_FACADE_BINDINGS"] = bindings


def normalize_session_compatibility(session_class: type, facade_name: str) -> None:
    """Restore the monolith-era metadata of ``Session`` and its methods."""
    session_class.__module__ = facade_name
    session_class.__qualname__ = "Session"
    for base in session_class.__mro__:
        for name, descriptor in vars(base).items():
            if isinstance(descriptor, (classmethod, staticmethod)):
                function = descriptor.__func__
            else:
                function = descriptor
            if not inspect.isfunction(function):
                continue
            if not function.__module__.startswith("api.models_parts."):
                continue
            function.__module__ = facade_name
            function.__qualname__ = f"Session.{name}"


def snapshot_part_namespaces(
    qualified_names: tuple[str, ...],
) -> dict[str, tuple[ModuleType, dict[str, Any]]]:
    """Capture existing implementation-module namespaces before facade reload."""
    snapshots = {}
    for qualified_name in qualified_names:
        part = sys.modules.get(qualified_name)
        if part is not None:
            snapshots[qualified_name] = (part, dict(vars(part)))
    return snapshots


def commit_facade_namespace() -> None:
    """Remember the last fully synchronized facade namespace for rollback."""
    facade = sys.modules[_FACADE_NAME]
    committed = dict(vars(facade))
    committed.pop("_MODELS_FACADE_COMMITTED_NAMESPACE", None)
    vars(facade)["_MODELS_FACADE_COMMITTED_NAMESPACE"] = committed


def rollback_facade_reload(
    facade: ModuleType,
    facade_class: type[ModuleType],
    committed_namespace: dict[str, Any],
    part_snapshots: dict[str, tuple[ModuleType, dict[str, Any]]],
    qualified_names: tuple[str, ...],
) -> None:
    """Restore the last committed facade and every part after reload failure."""
    for qualified_name in qualified_names:
        snapshot = part_snapshots.get(qualified_name)
        if snapshot is None:
            sys.modules.pop(qualified_name, None)
            continue
        part, namespace = snapshot
        sys.modules[qualified_name] = part
        vars(part).clear()
        vars(part).update(namespace)

    vars(facade).clear()
    vars(facade).update(committed_namespace)
    vars(facade)["_MODELS_FACADE_COMMITTED_NAMESPACE"] = committed_namespace
    facade.__class__ = facade_class


class ModelsFacade(ModuleType):
    """Module type that preserves assignments to historical patch seams."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name.startswith("_MODELS_FACADE_") or name in _MODULE_METADATA:
            return
        committed = self.__dict__.get("_MODELS_FACADE_COMMITTED_NAMESPACE")
        if committed is not None:
            committed[name] = value
        bindings = self.__dict__.get("_MODELS_FACADE_BINDINGS", {})
        for part in bindings.get(name, ()):
            vars(part)[name] = value

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        if name.startswith("_MODELS_FACADE_") or name in _MODULE_METADATA:
            return
        committed = self.__dict__.get("_MODELS_FACADE_COMMITTED_NAMESPACE")
        if committed is not None:
            committed.pop(name, None)
        bindings = self.__dict__.get("_MODELS_FACADE_BINDINGS", {})
        for part in bindings.get(name, ()):
            vars(part).pop(name, None)
