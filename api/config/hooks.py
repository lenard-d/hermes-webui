"""Explicit downstream adapters used by the config/model foundation."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager


@dataclass(frozen=True, slots=True)
class ConfigRuntimeHooks:
    provider_has_credential: Callable[[str], bool] = lambda _provider: False
    active_profile_name: Callable[[], str] = lambda: "default"
    active_profile_home: Callable[[], Path | str | None] = lambda: None
    active_profile_scope: Callable[[str], ContextManager] = lambda _purpose: nullcontext()
    detached_profile_scope: Callable[[str, str], ContextManager] = (
        lambda _profile, _purpose: nullcontext()
    )
    webui_version: Callable[[], str | None] = lambda: None
    credential_cache_invalidated: Callable[[str], None] = lambda _provider: None


_runtime_hooks = ConfigRuntimeHooks()


def get_config_runtime_hooks() -> ConfigRuntimeHooks:
    return _runtime_hooks


def install_config_runtime_hooks(**changes) -> None:
    """Install downstream adapters without adding reverse imports."""
    global _runtime_hooks
    values = {
        "provider_has_credential": _runtime_hooks.provider_has_credential,
        "active_profile_name": _runtime_hooks.active_profile_name,
        "active_profile_home": _runtime_hooks.active_profile_home,
        "active_profile_scope": _runtime_hooks.active_profile_scope,
        "detached_profile_scope": _runtime_hooks.detached_profile_scope,
        "webui_version": _runtime_hooks.webui_version,
        "credential_cache_invalidated": _runtime_hooks.credential_cache_invalidated,
    }
    values.update({key: value for key, value in changes.items() if value is not None})
    _runtime_hooks = ConfigRuntimeHooks(**values)
