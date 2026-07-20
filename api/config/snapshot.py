"""Immutable identity resolved once for one config-facing operation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Resolved profile/config identity used throughout one operation."""

    profile_name: str
    hermes_home: Path
    config_path: Path
    auth_store_path: Path
    models_cache_path: Path
    config: Mapping[str, Any]


def profile_name_for_home(home: Path) -> str:
    """Derive the stable profile cache key from a resolved Hermes home."""
    expanded = Path(home).expanduser()
    if expanded.parent.name != "profiles":
        return "default"
    name = expanded.name.lower()
    if not name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in name):
        return "default"
    return name[:64]


def resolve_config_snapshot(
    *,
    profile_home: Path | str | None = None,
    profile_name: str | None = None,
    config_data: Mapping[str, Any] | None = None,
) -> ConfigSnapshot:
    """Resolve paths and config exactly once at an operation boundary."""
    from api import config as config_api

    from api.config.hooks import get_config_runtime_hooks

    hooks = get_config_runtime_hooks()
    env_home = config_api._thread_local_env_value("HERMES_HOME")
    env_config = config_api._thread_local_env_value("HERMES_CONFIG_PATH")
    raw_home = profile_home or env_home
    if not raw_home:
        try:
            raw_home = hooks.active_profile_home()
        except Exception:
            raw_home = None
    if not raw_home and env_config:
        raw_home = Path(env_config).expanduser().parent
    home = Path(raw_home or config_api._DEFAULT_HERMES_HOME).expanduser()

    try:
        hook_name = hooks.active_profile_name()
    except Exception:
        hook_name = "default"
    if profile_name:
        name_source = profile_name
    elif profile_home:
        name_source = profile_name_for_home(home)
    else:
        name_source = hook_name or profile_name_for_home(home)
    raw_name = str(name_source).strip().lower()
    if raw_name in {"", "default"}:
        name = "default"
    else:
        name = "".join(
            ch if ch in "abcdefghijklmnopqrstuvwxyz0123456789_-" else "_"
            for ch in raw_name
        )[:64] or "default"
    config_path = (
        Path(env_config).expanduser()
        if env_config and profile_home is None
        else home / "config.yaml"
    )
    if config_data is None:
        if config_path == config_api._get_config_path():
            resolved_config = dict(config_api.get_config())
        else:
            resolved_config = dict(config_api.get_config_for_profile_home(home))
    else:
        resolved_config = dict(config_data)
    from api.config.catalog_state import MODEL_CATALOG_STATE

    base_cache = MODEL_CATALOG_STATE.models_cache_path
    if base_cache is None:
        base_cache = getattr(config_api, "_models_cache_path", None)
    if base_cache is None:
        base_cache = config_api.STATE_DIR / "models_cache.json"
    models_cache_path = (
        base_cache
        if name == "default"
        else base_cache.with_name(f"{base_cache.stem}.{name}{base_cache.suffix}")
    )
    return ConfigSnapshot(
        profile_name=name,
        hermes_home=home,
        config_path=config_path,
        auth_store_path=home / "auth.json",
        models_cache_path=models_cache_path,
        config=MappingProxyType(resolved_config),
    )
