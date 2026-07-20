"""Fail-closed onboarding detection and status projection."""

from __future__ import annotations

import logging
import os
from api.auth import is_auth_enabled
from api.config import (
    DEFAULT_MODEL,
    DEFAULT_WORKSPACE,
    get_available_models,
    get_config,
    get_config_path,
    is_hermes_agent_available,
    load_settings,
    provider_display_name,
    save_settings,
    verify_hermes_imports,
)
from api.workspace import get_last_workspace, load_workspaces

from .catalog import (
    SUPPORTED_PROVIDER_SETUPS,
    build_setup_catalog,
    extract_current_base_url,
    extract_current_model,
    extract_current_provider,
)
from .persistence import (
    get_active_hermes_home,
    load_env_file,
    provider_api_key_present,
    provider_oauth_authenticated,
)

logger = logging.getLogger(__name__)


def status_from_runtime(config: dict, imports_ok: bool) -> dict:
    provider = extract_current_provider(config)
    model = extract_current_model(config)
    base_url = extract_current_base_url(config)
    hermes_home = get_active_hermes_home()
    env_values = load_env_file(hermes_home / ".env")
    configured = bool(provider and model)
    ready = False

    if configured:
        metadata = SUPPORTED_PROVIDER_SETUPS.get(provider)
        if metadata is not None:
            if metadata.get("key_optional"):
                ready = bool(base_url) if metadata.get("requires_base_url") else True
            else:
                ready = provider_api_key_present(provider, config, env_values)
                if metadata.get("requires_base_url"):
                    ready = bool(base_url and ready)
                if not ready and metadata.get("oauth_provider"):
                    ready = provider_oauth_authenticated(
                        str(metadata["oauth_provider"]), hermes_home
                    )
        else:
            ready = provider_api_key_present(
                provider, config, env_values
            ) or provider_oauth_authenticated(provider, hermes_home)

    hermes_found = is_hermes_agent_available()
    chat_ready = bool(hermes_found and imports_ok and ready)
    note_args: list[str] = []
    if not hermes_found or not imports_ok:
        state = "agent_unavailable"
        note_key = "onboarding_notice_system_unavailable"
        note = (
            "Hermes is not fully importable from the Web UI yet. Finish bootstrap "
            "or fix the agent install before provider setup will work."
        )
    elif chat_ready:
        state = "ready"
        note_key = "onboarding_notice_system_ready"
        provider_name = provider_display_name(provider) if provider else "Hermes"
        note = f"Hermes is minimally configured and ready to chat via {provider_name}."
    elif configured:
        state = "provider_incomplete"
        if provider == "custom" and not base_url:
            note_key = "onboarding_notice_custom_base_url_required"
            note = (
                "Hermes has a saved provider/model selection, but the custom "
                "provider still needs a base URL. Add the API key too if that "
                "server requires one."
            )
        elif provider not in SUPPORTED_PROVIDER_SETUPS:
            note_key = "onboarding_notice_provider_auth_required"
            note_args = [provider]
            note = (
                f"Provider '{provider}' is configured but not yet authenticated. "
                "Run 'hermes auth' or 'hermes model' in a terminal to complete "
                "setup, then reload the Web UI."
            )
        else:
            note_key = "onboarding_notice_provider_api_key_required"
            note = (
                "Hermes has a saved provider/model selection but still needs the "
                "API key required to chat."
            )
    else:
        state = "needs_provider"
        note_key = "onboarding_notice_provider_choice_required"
        note = (
            "Hermes is installed, but you still need to choose a provider and "
            "save working credentials."
        )

    return {
        "provider_configured": configured,
        "provider_ready": ready,
        "chat_ready": chat_ready,
        "setup_state": state,
        "provider_note": note,
        "provider_note_key": note_key,
        "provider_note_args": note_args,
        "current_provider": provider or None,
        "current_model": model or None,
        "current_base_url": base_url or None,
        "env_path": str(hermes_home / ".env"),
    }


def setup_catalog(config: dict) -> dict:
    return build_setup_catalog(
        config,
        hermes_home=get_active_hermes_home(),
        oauth_authenticated=provider_oauth_authenticated,
    )


def _skip_requested() -> bool:
    return os.environ.get("HERMES_WEBUI_SKIP_ONBOARDING", "").strip() in {
        "1",
        "true",
        "yes",
    }


def get_onboarding_status() -> dict:
    settings = load_settings()
    config = get_config()
    imports_ok, missing, errors = verify_hermes_imports()
    runtime = status_from_runtime(config, imports_ok)
    config_path = get_config_path()
    provider = extract_current_provider(config)
    non_wizard_provider = bool(
        provider and provider not in SUPPORTED_PROVIDER_SETUPS
    )
    config_auto_completed = config_path.exists() and (
        bool(runtime.get("chat_ready"))
        or (non_wizard_provider and bool(runtime.get("provider_configured")))
    )
    if config_auto_completed and not settings.get("onboarding_completed"):
        try:
            save_settings({"onboarding_completed": True})
            settings["onboarding_completed"] = True
        except Exception:
            logger.debug("Failed to persist onboarding_completed", exc_info=True)

    return {
        "completed": bool(settings.get("onboarding_completed"))
        or _skip_requested()
        or config_auto_completed,
        "settings": {
            "default_model": settings.get("default_model") or DEFAULT_MODEL,
            "default_workspace": settings.get("default_workspace")
            or str(DEFAULT_WORKSPACE),
            "password_enabled": is_auth_enabled(),
            "bot_name": settings.get("bot_name") or "Hermes",
        },
        "system": {
            "hermes_found": is_hermes_agent_available(),
            "imports_ok": bool(imports_ok),
            "missing_modules": missing,
            "import_errors": errors,
            "config_path": str(config_path),
            "config_exists": config_path.exists(),
            **runtime,
        },
        "setup": setup_catalog(config),
        "workspaces": {
            "items": load_workspaces(),
            "last": get_last_workspace(),
        },
        "models": get_available_models(),
    }
