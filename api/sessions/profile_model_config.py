"""Profile-scoped model configuration and safe session defaults."""

from __future__ import annotations

import logging
import os
import threading
import time

from api.config import get_config_for_profile_home
from api.model_context import _clean_session_model_provider


logger = logging.getLogger(__name__)

_PROFILE_CONFIG_CACHE: "dict[tuple, tuple[float, str, dict]]" = {}
_PROFILE_CONFIG_CACHE_TTL_SECONDS = 60.0
_PROFILE_CONFIG_CACHE_LOCK = threading.Lock()


def _read_profile_config_cached(profile_name: str, cfg_path: str) -> dict | None:
    """Return parsed profile config from the bounded, content-verified cache."""
    try:
        st = os.stat(cfg_path)
    except OSError:
        return None
    mtime = float(getattr(st, "st_mtime", 0.0) or 0.0)
    size = int(getattr(st, "st_size", 0) or 0)
    inode = int(getattr(st, "st_ino", 0) or 0)
    key = (str(profile_name or ""), inode, mtime, size)
    now = time.monotonic()
    with _PROFILE_CONFIG_CACHE_LOCK:
        cached = _PROFILE_CONFIG_CACHE.get(key)
        if cached is not None:
            cached_at, cached_content, cached_dict = cached
            if (now - cached_at) <= _PROFILE_CONFIG_CACHE_TTL_SECONDS:
                current_content = None
                try:
                    with open(cfg_path, "r", encoding="utf-8") as config_file:
                        current_content = config_file.read()
                except Exception:
                    pass
                if current_content == cached_content:
                    return cached_dict

    import yaml

    try:
        with open(cfg_path, encoding="utf-8") as config_file:
            content = config_file.read()
            parsed = yaml.safe_load(content) or {}
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    with _PROFILE_CONFIG_CACHE_LOCK:
        _PROFILE_CONFIG_CACHE[key] = (now, content, parsed)
        if len(_PROFILE_CONFIG_CACHE) > 32:
            excess = max(0, len(_PROFILE_CONFIG_CACHE) - 32)
            for old_key in list(_PROFILE_CONFIG_CACHE.keys())[:excess]:
                _PROFILE_CONFIG_CACHE.pop(old_key, None)
    return parsed


def _read_profile_model_config(
    session,
    requested_provider: str | None,
) -> tuple[str | None, str | None, dict | None]:
    """Read the session profile's provider, default model, and config mapping."""
    if not getattr(session, "profile", None):
        return None, None, None

    try:
        from api.profiles import get_hermes_home_for_profile

        profile_name = str(session.profile or "")
        profile_home = get_hermes_home_for_profile(profile_name)
        profile_cfg_path = os.path.join(str(profile_home), "config.yaml")
        if not os.path.isfile(profile_cfg_path):
            return None, None, None
        profile_config = _read_profile_config_cached(profile_name, profile_cfg_path)
        if profile_config is None:
            return None, None, None
        model_config = profile_config.get("model") or {}
        if not isinstance(model_config, dict):
            return None, None, profile_config
        provider = (model_config.get("provider") or "").strip() or None
        default_model = (model_config.get("default") or "").strip() or None
    except Exception:
        logger.warning(
            "profile provider read failed for %r",
            getattr(session, "profile", None),
            exc_info=True,
        )
        return None, None, None

    requested = _clean_session_model_provider(requested_provider)
    if requested:
        if _clean_session_model_provider(provider) != requested:
            return None, None, profile_config
        return None, default_model, profile_config
    return provider, default_model, profile_config


def _load_profile_config_dict(session) -> dict | None:
    """Load the session profile's config.yaml as a dict, or None."""
    if not getattr(session, "profile", None):
        return None
    try:
        from api.profiles import get_hermes_home_for_profile

        profile_cfg_path = os.path.join(
            str(get_hermes_home_for_profile(session.profile)),
            "config.yaml",
        )
        if not os.path.isfile(profile_cfg_path):
            return None
        import yaml

        with open(profile_cfg_path, encoding="utf-8") as config_file:
            profile_config = yaml.safe_load(config_file) or {}
        return profile_config if isinstance(profile_config, dict) else None
    except Exception:
        logger.warning(
            "profile config read failed for %r",
            getattr(session, "profile", None),
            exc_info=True,
        )
        return None


def _worktree_default_from_config(profile: str | None) -> bool:
    """Return the strict boolean worktree default for the selected profile."""
    try:
        if profile:
            from api.profiles import get_hermes_home_for_profile

            config = get_config_for_profile_home(get_hermes_home_for_profile(profile))
        else:
            config = get_config_for_profile_home(None)
        return (config or {}).get("worktree", False) is True
    except Exception:
        logger.warning("failed to read worktree config default", exc_info=True)
        return False
