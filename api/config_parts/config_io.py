"""Profile-aware ``config.yaml`` loading, caching, expansion, and saving.

The mutable cache and the public ``cfg`` alias stay on ``api.config`` for
backward compatibility.  This module owns the I/O policy and resolves the
facade for every decision/action pair, preserving long-standing monkeypatch
seams for profile paths, cache state, env readers, and model-cache invalidation.
"""

import copy
import json
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from api.config_parts.facade import config_api


class ConfigIOAPI(Protocol):
    cfg: dict
    _cfg_cache: dict
    _cfg_lock: object
    _cfg_mtime: float
    _cfg_path: Path | None
    _cfg_fingerprint: str | None
    _yaml_file_cache: dict[str, tuple]
    _yaml_file_cache_lock: object
    _thread_ctx: object
    _DEFAULT_HERMES_HOME: Path
    _DEFAULT_AGENT_PERSONALITIES: dict
    _DEFAULT_EXPERIMENTAL_CONFIG: dict
    _DEFAULT_WEBUI_SESSION_SAVE_MODE: str
    _WEBUI_SESSION_SAVE_MODES: set[str]
    logger: object

    def _thread_local_env_value(self, name: str, default: str = "") -> str: ...
    def _expand_env_vars(self, obj): ...
    def _fingerprint_config(self, data: dict) -> str: ...
    def _cfg_has_in_memory_overrides(self) -> bool: ...
    def _get_config_path(self) -> Path: ...
    def _apply_config_defaults(self, config_data: dict) -> None: ...
    def _refresh_config_cache(self, config_path: Path | None = None) -> None: ...
    def _load_yaml_config_file_raw(
        self, config_path: Path, *, _copy: bool = True
    ) -> dict: ...
    def _load_yaml_config_file(self, config_path: Path) -> dict: ...
    def _config_for_yaml_save(self, config_data: dict) -> dict: ...
    def _delete_models_cache_on_disk(self) -> None: ...
    def get_config(self) -> dict: ...


def _config_api() -> ConfigIOAPI:
    return cast(ConfigIOAPI, cast(ModuleType, config_api()))


def _thread_local_env_value(name: str, default: str = "") -> str:
    """Return thread-local profile env first, then process env when allowed."""
    import os

    env_name = str(name or "").strip()
    if not env_name:
        return default or ""
    api = _config_api()
    thread_env = getattr(api._thread_ctx, "env", {})
    if isinstance(thread_env, dict) and env_name in thread_env:
        thread_value = thread_env.get(env_name)
        if thread_value is None:
            return default or ""
        return str(thread_value)
    if bool(getattr(api._thread_ctx, "block_process_env_fallback", False)):
        return default or ""
    return str(os.getenv(env_name, default or ""))


def _expand_env_vars(obj):
    """Recursively expand ``${VAR}`` references through profile-scoped env."""
    import re

    api = _config_api()
    if isinstance(obj, str):
        return re.sub(
            r"\${([^}]+)}",
            lambda match: api._thread_local_env_value(
                match.group(1), match.group(0)
            ),
            obj,
        )
    if isinstance(obj, dict):
        return {key: api._expand_env_vars(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [api._expand_env_vars(item) for item in obj]
    return obj


def _fingerprint_config(data: dict) -> str:
    """Return a stable fingerprint for config dictionaries."""
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return repr(data)


def _cfg_has_in_memory_overrides() -> bool:
    """Return whether the compatibility facade carries an in-memory override."""
    api = _config_api()
    if (
        api._cfg_fingerprint is not None
        and api._fingerprint_config(api._cfg_cache) != api._cfg_fingerprint
    ):
        return True
    try:
        return api.cfg is not api._cfg_cache
    except AttributeError:
        return False


def _get_config_path() -> Path:
    """Return ``config.yaml`` for the active profile or explicit override."""
    import os

    api = _config_api()
    env_override = os.getenv("HERMES_CONFIG_PATH")
    if env_override:
        return Path(env_override).expanduser()
    try:
        from api.profiles import get_active_hermes_home

        return get_active_hermes_home() / "config.yaml"
    except ImportError:
        return api._DEFAULT_HERMES_HOME / "config.yaml"


def _apply_config_defaults(config_data: dict) -> None:
    """Populate documented default-only config keys in-place."""
    api = _config_api()
    agent_cfg = config_data.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        config_data["agent"] = agent_cfg
    personalities = agent_cfg.get("personalities")
    if isinstance(personalities, dict):
        merged = copy.deepcopy(api._DEFAULT_AGENT_PERSONALITIES)
        merged.update(copy.deepcopy(personalities))
        agent_cfg["personalities"] = merged
    else:
        agent_cfg["personalities"] = copy.deepcopy(api._DEFAULT_AGENT_PERSONALITIES)

    experimental = config_data.get("experimental")
    if not isinstance(experimental, dict):
        experimental = {}
        config_data["experimental"] = experimental
    for key, value in api._DEFAULT_EXPERIMENTAL_CONFIG.items():
        experimental.setdefault(key, value)


def reload_config_if_stale() -> None:
    """Refresh ``config.yaml`` once for concurrent stale read paths."""
    api = _config_api()
    with api._cfg_lock:
        config_path = api._get_config_path()
        try:
            current_mtime = config_path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        path_changed = api._cfg_path != config_path
        mtime_stale = current_mtime != api._cfg_mtime
        if (
            not api._cfg_cache
            or path_changed
            or (mtime_stale and not api._cfg_has_in_memory_overrides())
        ):
            api._refresh_config_cache(config_path)
            if path_changed:
                api.cfg = api._cfg_cache


def get_config() -> dict:
    """Return the active cached config, honoring facade-level overrides."""
    api = _config_api()
    config_path = api._get_config_path()
    try:
        current_mtime = config_path.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    path_changed = api._cfg_path != config_path
    mtime_stale = current_mtime != api._cfg_mtime
    if (
        not api._cfg_cache
        or path_changed
        or (mtime_stale and not api._cfg_has_in_memory_overrides())
    ):
        reload_config_if_stale()
    if api.cfg is not api._cfg_cache:
        return api.cfg
    return api._cfg_cache


def get_webui_session_save_mode(config_data: dict | None = None) -> str:
    """Return the validated first-turn session persistence mode."""
    api = _config_api()
    active_cfg = config_data if isinstance(config_data, dict) else api.cfg
    webui_cfg = active_cfg.get("webui", {}) if isinstance(active_cfg, dict) else {}
    if not isinstance(webui_cfg, dict):
        return api._DEFAULT_WEBUI_SESSION_SAVE_MODE
    mode = webui_cfg.get("session_save_mode", api._DEFAULT_WEBUI_SESSION_SAVE_MODE)
    if isinstance(mode, str):
        normalized = mode.strip().lower()
        if normalized in api._WEBUI_SESSION_SAVE_MODES:
            return normalized
    return api._DEFAULT_WEBUI_SESSION_SAVE_MODE


def is_unified_session_db_enabled(config_data: dict | None = None) -> bool:
    """Return the dormant unified-session-db feature flag."""
    active_cfg = config_data if isinstance(config_data, dict) else _config_api().cfg
    experimental = (
        active_cfg.get("experimental", {}) if isinstance(active_cfg, dict) else {}
    )
    if not isinstance(experimental, dict):
        return False
    return experimental.get("unified_session_db") is True


def _refresh_config_cache(config_path: Path | None = None) -> None:
    """Refresh facade-owned config state; caller must hold ``_cfg_lock``."""
    api = _config_api()
    if config_path is None:
        config_path = api._get_config_path()
    api._cfg_cache.clear()
    old_cfg_mtime = api._cfg_mtime
    api._cfg_path = config_path
    api._cfg_mtime = 0.0
    try:
        if config_path.exists():
            loaded = api._load_yaml_config_file_raw(config_path)
            if isinstance(loaded, dict):
                if loaded:
                    previous_block = getattr(
                        api._thread_ctx, "block_process_env_fallback", False
                    )
                    previous_env = getattr(api._thread_ctx, "env", None)
                    try:
                        api._thread_ctx.block_process_env_fallback = False
                        api._thread_ctx.env = {}
                        api._cfg_cache.update(api._expand_env_vars(loaded))
                    finally:
                        api._thread_ctx.block_process_env_fallback = previous_block
                        if previous_env is None:
                            try:
                                del api._thread_ctx.env
                            except AttributeError:
                                pass
                        else:
                            api._thread_ctx.env = previous_env
                try:
                    api._cfg_mtime = Path(config_path).stat().st_mtime
                except OSError:
                    api._cfg_mtime = 0.0
    except Exception:
        api.logger.debug("Failed to load yaml config from %s", config_path)
    api._apply_config_defaults(api._cfg_cache)
    api._cfg_fingerprint = api._fingerprint_config(api._cfg_cache)
    if old_cfg_mtime != 0.0:
        api._delete_models_cache_on_disk()


def reload_config() -> None:
    """Force a reload from the active profile's config path."""
    api = _config_api()
    with api._cfg_lock:
        api._refresh_config_cache(api._get_config_path())


def _load_yaml_config_file_raw(config_path: Path, *, _copy: bool = True) -> dict:
    """Return memoized raw YAML keyed by path, mtime, and size."""
    try:
        import yaml as yaml_module
    except ImportError:
        return {}
    try:
        stat = config_path.stat()
    except OSError:
        return {}

    api = _config_api()
    cache_key = str(config_path)
    stat_key = (stat.st_mtime_ns, stat.st_size)
    with api._yaml_file_cache_lock:
        cached = api._yaml_file_cache.get(cache_key)
        if cached is not None and cached[0] == stat_key:
            raw = cached[1]
            if not isinstance(raw, dict):
                return {}
            return copy.deepcopy(raw) if _copy else raw

    try:
        loaded = yaml_module.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        api.logger.debug("Failed to parse yaml config from %s", config_path)
        return {}
    raw = loaded if isinstance(loaded, dict) else {}
    with api._yaml_file_cache_lock:
        api._yaml_file_cache[cache_key] = (stat_key, raw)
    return copy.deepcopy(raw) if _copy else raw


def _load_yaml_config_file(config_path: Path) -> dict:
    """Load YAML and expand env references against the current profile scope."""
    api = _config_api()
    raw = api._load_yaml_config_file_raw(config_path, _copy=False)
    if not raw:
        return {}
    expanded = api._expand_env_vars(raw)
    return expanded if isinstance(expanded, dict) else {}


def get_config_for_profile_home(profile_home: Path | str | None) -> dict:
    """Read config for a known profile home without mutating global cache state."""
    api = _config_api()
    if not profile_home:
        return api.get_config()
    try:
        target = Path(profile_home).expanduser()
    except Exception:
        return api.get_config()
    try:
        from api.profiles import get_active_hermes_home

        if Path(get_active_hermes_home()).expanduser() == target:
            return api.get_config()
    except Exception:
        pass
    try:
        if api._get_config_path().parent == target:
            return api.get_config()
    except Exception:
        pass
    if not target.exists():
        return {}
    profile_cfg = api._load_yaml_config_file(target / "config.yaml")
    api._apply_config_defaults(profile_cfg)
    return profile_cfg


def _config_for_yaml_save(config_data: dict) -> dict:
    """Return YAML-safe config without runtime-expanded built-in defaults."""
    if not isinstance(config_data, dict):
        return {}
    api = _config_api()
    data = copy.deepcopy(config_data)
    agent_cfg = data.get("agent")
    if isinstance(agent_cfg, dict):
        personalities = agent_cfg.get("personalities")
        if isinstance(personalities, dict):
            custom_personalities = {
                name: value
                for name, value in personalities.items()
                if api._DEFAULT_AGENT_PERSONALITIES.get(name) != value
            }
            if custom_personalities:
                agent_cfg["personalities"] = custom_personalities
            else:
                agent_cfg.pop("personalities", None)
        if not agent_cfg:
            data.pop("agent", None)
    return data


def _save_yaml_config_file(config_path: Path, config_data: dict) -> None:
    """Persist config YAML and invalidate the exact memoized read entry."""
    try:
        import yaml as yaml_module
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to write Hermes config.yaml") from exc
    api = _config_api()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml_module.safe_dump(
            api._config_for_yaml_save(config_data),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with api._yaml_file_cache_lock:
        api._yaml_file_cache.pop(str(config_path), None)
