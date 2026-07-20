"""Sanitized plugin visibility and dashboard-plugin enablement."""

from __future__ import annotations

import logging

from api.helpers import j


logger = logging.getLogger(__name__)

_VISIBILITY_HOOKS = (
    "pre_tool_call",
    "post_tool_call",
    "pre_llm_call",
    "post_llm_call",
)
_VISIBILITY_HOOK_SET = set(_VISIBILITY_HOOKS)


def get_plugin_manager_for_visibility():
    """Return Hermes Agent's plugin manager for read-only WebUI visibility."""
    from hermes_cli.plugins import get_plugin_manager

    return get_plugin_manager()


def clean_visibility_text(value, *, limit=240) -> str:
    """Return bounded display text without path or callback internals."""
    if value is None:
        return ""
    text = " ".join(str(value).replace("\x00", "").strip().split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def visibility_category_from_key(key: str) -> str:
    raw = str(key or "").strip().replace("\\", "/")
    if "/" not in raw:
        return ""
    category = raw.split("/", 1)[0].strip()
    return category if category and category not in {".", ".."} else ""


def selected_provider(category: str) -> str:
    if not category:
        return ""
    try:
        from api.config import get_config

        config = get_config() or {}
    except Exception:
        return ""
    category_config = config.get(category, {}) if isinstance(config, dict) else {}
    if not isinstance(category_config, dict):
        return ""
    return str(category_config.get("provider") or "").strip().lower()


def visibility_payload(manager=None) -> dict:
    """Build sanitized plugin and lifecycle-hook metadata for Settings."""
    manager = manager or get_plugin_manager_for_visibility()
    manager.discover_and_load(force=False)
    plugins = []
    raw_plugins = getattr(manager, "_plugins", {}) or {}
    for key, loaded in sorted(raw_plugins.items(), key=lambda item: str(item[0])):
        manifest = getattr(loaded, "manifest", None)
        if manifest is None:
            continue
        plugin_key = clean_visibility_text(
            getattr(manifest, "key", None) or key or getattr(manifest, "name", ""),
            limit=120,
        )
        name = clean_visibility_text(
            getattr(manifest, "name", "") or plugin_key, limit=120
        )
        version = clean_visibility_text(getattr(manifest, "version", ""), limit=80)
        description = clean_visibility_text(
            getattr(manifest, "description", ""), limit=280
        )
        kind = clean_visibility_text(
            getattr(manifest, "kind", "") or "standalone", limit=40
        )
        enabled = bool(getattr(loaded, "enabled", False))
        category = visibility_category_from_key(plugin_key)
        configured_provider = selected_provider(category)
        plugin_slug = plugin_key.rsplit("/", 1)[-1].strip().lower()
        if kind == "exclusive":
            activation = "exclusive"
        elif kind == "model-provider" and enabled:
            activation = "provider"
        else:
            activation = "enabled" if enabled else "disabled"

        include_active_provider = True
        if kind == "exclusive":
            if category:
                is_active_provider = bool(configured_provider) and (
                    plugin_slug == configured_provider
                )
            else:
                include_active_provider = False
                is_active_provider = False
        else:
            is_active_provider = kind == "model-provider" and enabled

        registered = []
        hooks = list(getattr(manifest, "provides_hooks", []) or []) + list(
            getattr(loaded, "hooks_registered", []) or []
        )
        for hook in hooks:
            hook_name = str(hook or "").strip()
            if hook_name in _VISIBILITY_HOOK_SET and hook_name not in registered:
                registered.append(hook_name)
        registered.sort(key=_VISIBILITY_HOOKS.index)
        payload = {
            "name": name,
            "key": plugin_key or name,
            "version": version,
            "description": description,
            "enabled": enabled,
            "kind": kind,
            "activation": activation,
            "hooks": registered,
        }
        if include_active_provider:
            payload["is_active_provider"] = bool(is_active_provider)
        plugins.append(payload)
    return {
        "plugins": plugins,
        "empty": not bool(plugins),
        "supported_hooks": list(_VISIBILITY_HOOKS),
        "read_only": True,
    }


def dashboard_plugin_enabled(plugin_name: str) -> bool:
    """Return whether an opt-in WebUI dashboard plugin is enabled."""
    try:
        from api.config import load_settings

        preferences = (load_settings() or {}).get("dashboard_plugins", {}) or {}
        return bool(preferences.get(plugin_name, False))
    except Exception:
        return False


def webui_plugin_payload() -> list[dict]:
    try:
        from api.plugins import get_plugin_metadata

        return get_plugin_metadata()
    except Exception:
        return []


def handle_plugins(handler, parsed) -> bool:
    del parsed
    try:
        hermes_plugins = visibility_payload()
        webui_plugins = webui_plugin_payload()
        plugins = hermes_plugins["plugins"] + webui_plugins
        return j(
            handler,
            {
                "plugins": plugins,
                "empty": not bool(plugins),
                "supported_hooks": hermes_plugins["supported_hooks"],
                "read_only": True,
            },
        )
    except Exception as exc:
        logger.warning("Failed to build plugin visibility payload: %s", exc)
        return j(
            handler,
            {
                "plugins": [],
                "empty": True,
                "supported_hooks": list(_VISIBILITY_HOOKS),
                "read_only": True,
                "unavailable": True,
            },
        )


__all__ = (
    "clean_visibility_text",
    "dashboard_plugin_enabled",
    "get_plugin_manager_for_visibility",
    "handle_plugins",
    "selected_provider",
    "visibility_category_from_key",
    "visibility_payload",
    "webui_plugin_payload",
)
