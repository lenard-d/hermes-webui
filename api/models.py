"""Stable compatibility facade for Hermes WebUI session models.

Implementation lives in cohesive modules under :mod:`api.models_parts`. The
facade republishes the former monolith's complete namespace so existing imports,
monkeypatch seams, and serialized ``api.models.Session`` references keep working.
"""

from __future__ import annotations

import importlib as _importlib
import inspect as _inspect
import sys as _sys
import types as _types

from api.models_parts._compat import (
    ModelsFacade as _ModelsFacade,
    commit_facade_namespace as _commit_facade_namespace,
    normalize_session_compatibility as _normalize_session_compatibility,
    rollback_facade_reload as _rollback_facade_reload,
    snapshot_part_namespaces as _snapshot_part_namespaces,
    sync_facade_to_parts as _sync_facade_to_parts,
)


_PART_NAMES = (
    "foundation", "session_index", "tombstones", "recovered_context",
    "session_persistence", "session_projection", "session", "process_wakeup",
    "interruption", "journal_output", "journal_retry", "recovery_markers",
    "stale_recovery", "cache_freshness", "session_cache", "session_creation",
    "sidebar_projection", "state_db_sidebar", "sidebar_listing", "projects",
    "claude_code", "cli_cache", "sidecar_projection", "cli_projection",
    "cli_listing", "state_db_reads", "message_identity", "state_db_delta",
    "message_merge", "cli_messages", "cli_deletion", "cleanup_manifests",
)


_MODELS_FACADE_TRANSACTION_FACADE = _sys.modules[__name__]
_MODELS_FACADE_TRANSACTION_WAS_LOADED = bool(globals().get("_MODELS_FACADE_READY"))
_MODELS_FACADE_TRANSACTION_COMMITTED = globals().get(
    "_MODELS_FACADE_COMMITTED_NAMESPACE"
)
_MODELS_FACADE_TRANSACTION_CLASS = _MODELS_FACADE_TRANSACTION_FACADE.__class__
_MODELS_FACADE_TRANSACTION_QUALIFIED_NAMES = tuple(
    f"api.models_parts.{part_name}" for part_name in _PART_NAMES
)
_MODELS_FACADE_TRANSACTION_PARTS = _snapshot_part_namespaces(
    _MODELS_FACADE_TRANSACTION_QUALIFIED_NAMES
)

try:
    if isinstance(_MODELS_FACADE_TRANSACTION_FACADE, _ModelsFacade):
        _MODELS_FACADE_TRANSACTION_FACADE.__class__ = _types.ModuleType

    for _old_name in tuple(globals().get("_MODELS_FACADE_EXPORTS", ())):
        globals().pop(_old_name, None)

    _parts = []
    _exports = []
    for _part_name in _PART_NAMES:
        _qualified_name = f"api.models_parts.{_part_name}"
        if _MODELS_FACADE_TRANSACTION_WAS_LOADED and _qualified_name in _sys.modules:
            _part = _importlib.reload(_sys.modules[_qualified_name])
        else:
            _part = _importlib.import_module(_qualified_name)
        _parts.append(_part)
        for _name in _part.__all__:
            _value = getattr(_part, _name)
            if _name == "Session" and _inspect.isclass(_value):
                _normalize_session_compatibility(_value, __name__)
            elif (
                (_inspect.isfunction(_value) or _inspect.isclass(_value))
                and getattr(_value, "__module__", None) == _part.__name__
            ):
                _value.__module__ = __name__
            globals()[_name] = _value
            if _name not in _exports:
                _exports.append(_name)

    _MODELS_FACADE_EXPORTS = tuple(_exports)
    _MODELS_FACADE_PARTS = tuple(_parts)
    _sync_facade_to_parts(
        _MODELS_FACADE_TRANSACTION_FACADE, _MODELS_FACADE_PARTS
    )
    _MODELS_FACADE_READY = True
    _MODELS_FACADE_TRANSACTION_FACADE.__class__ = _ModelsFacade
except BaseException:
    if _MODELS_FACADE_TRANSACTION_COMMITTED is not None:
        _rollback_facade_reload(
            _MODELS_FACADE_TRANSACTION_FACADE,
            _MODELS_FACADE_TRANSACTION_CLASS,
            _MODELS_FACADE_TRANSACTION_COMMITTED,
            _MODELS_FACADE_TRANSACTION_PARTS,
            _MODELS_FACADE_TRANSACTION_QUALIFIED_NAMES,
        )
    raise


for _MODELS_FACADE_TRANSACTION_CLEANUP_NAME in (
    "_exports",
    "_name",
    "_old_name",
    "_part",
    "_part_name",
    "_parts",
    "_qualified_name",
    "_value",
    "_MODELS_FACADE_TRANSACTION_CLASS",
    "_MODELS_FACADE_TRANSACTION_COMMITTED",
    "_MODELS_FACADE_TRANSACTION_FACADE",
    "_MODELS_FACADE_TRANSACTION_PARTS",
    "_MODELS_FACADE_TRANSACTION_QUALIFIED_NAMES",
    "_MODELS_FACADE_TRANSACTION_WAS_LOADED",
):
    globals().pop(_MODELS_FACADE_TRANSACTION_CLEANUP_NAME, None)
del _MODELS_FACADE_TRANSACTION_CLEANUP_NAME
_commit_facade_namespace()
