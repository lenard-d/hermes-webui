"""Provider setup and onboarding completion orchestration."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from api.config import get_config_path, invalidate_models_cache, reload_config, save_settings

from .catalog import (
    SUPPORTED_PROVIDER_SETUPS,
    normalize_base_url,
    normalize_model_for_provider,
)
from .persistence import (
    get_active_hermes_home,
    load_env_file,
    load_yaml_config,
    provider_api_key_present,
    provider_oauth_authenticated,
    save_yaml_config,
    write_env_values,
)
from .status import get_onboarding_status

logger = logging.getLogger(__name__)


def _validate_provider_endpoint(metadata: dict, base_url: str, message: str) -> None:
    if not metadata.get("requires_base_url"):
        return
    if not base_url:
        raise ValueError(message)
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base_url must start with http:// or https://")


def _reload_runtime_config(
    home: Path,
    *,
    env_var: str = "",
    api_key: str = "",
    set_env_before_dotenv: bool = False,
) -> None:
    if set_env_before_dotenv and env_var and api_key:
        os.environ[env_var] = api_key
    try:
        from api.profiles import reload_profile_environment

        reload_profile_environment(home)
    except Exception:
        logger.debug("Failed to reload dotenv", exc_info=True)
    if not set_env_before_dotenv and env_var and api_key:
        os.environ[env_var] = api_key
    try:
        from hermes_cli.config import reload as reload_cli_config

        reload_cli_config()
    except Exception:
        logger.debug("Failed to reload hermes_cli config", exc_info=True)


def apply_onboarding_setup(body: dict) -> dict:
    if os.environ.get("HERMES_WEBUI_SKIP_ONBOARDING", "").strip() in {
        "1",
        "true",
        "yes",
    }:
        save_settings({"onboarding_completed": True})
        return get_onboarding_status()

    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    base_url = normalize_base_url(str(body.get("base_url") or ""))
    metadata = SUPPORTED_PROVIDER_SETUPS.get(provider)
    if metadata is None:
        save_settings({"onboarding_completed": True})
        return get_onboarding_status()
    if not model:
        raise ValueError("model is required")
    _validate_provider_endpoint(
        metadata, base_url, "base_url is required for custom endpoints"
    )

    config_path = get_config_path()
    if config_path.exists() and not body.get("confirm_overwrite"):
        return {
            "error": "config_exists",
            "message": (
                "Hermes is already configured (config.yaml exists). "
                "Pass confirm_overwrite=true to overwrite it."
            ),
            "requires_confirm": True,
        }

    home = get_active_hermes_home()
    config = load_yaml_config(config_path)
    env_values = load_env_file(home / ".env")
    if not api_key and not provider_api_key_present(provider, config, env_values):
        oauth_ready = bool(metadata.get("oauth_provider")) and provider_oauth_authenticated(
            str(metadata.get("oauth_provider")), home
        )
        if not metadata.get("key_optional") and not oauth_ready:
            raise ValueError(f"{metadata['env_var']} is required")

    model_config = config.get("model", {})
    if not isinstance(model_config, dict):
        model_config = {}
    model_config["provider"] = provider
    model_config["default"] = normalize_model_for_provider(provider, model)
    if metadata.get("requires_base_url"):
        model_config["base_url"] = base_url
    elif metadata.get("default_base_url"):
        model_config["base_url"] = metadata["default_base_url"]
    else:
        model_config.pop("base_url", None)
    config["model"] = model_config
    save_yaml_config(config_path, config)

    env_var = str(metadata["env_var"])
    if api_key:
        write_env_values(home / ".env", {env_var: api_key})
    _reload_runtime_config(home, env_var=env_var, api_key=api_key)
    reload_config()
    return get_onboarding_status()


def apply_self_hosted_provider_setup(body: dict) -> dict:
    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    base_url = normalize_base_url(str(body.get("base_url") or ""))
    activate = body.get("activate")
    do_activate = activate is None or bool(activate)
    if provider not in {"ollama", "lmstudio"}:
        raise ValueError(f"unsupported self-hosted provider: {provider}")
    if not model:
        raise ValueError("model is required")

    metadata = SUPPORTED_PROVIDER_SETUPS[provider]
    _validate_provider_endpoint(
        metadata, base_url, "base_url is required for this provider"
    )
    config_path = get_config_path()
    config = load_yaml_config(config_path)
    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
    provider_config = providers.setdefault(provider, {})
    if not isinstance(provider_config, dict):
        provider_config = {}
        providers[provider] = provider_config
    provider_config["base_url"] = base_url

    model_config = config.get("model", {})
    if not isinstance(model_config, dict):
        model_config = {}
    original_model_config = dict(model_config)
    if do_activate:
        model_config.update(
            provider=provider,
            default=normalize_model_for_provider(provider, model),
            base_url=base_url,
        )
        config["model"] = model_config
    elif "model" in config:
        config["model"] = original_model_config
    save_yaml_config(config_path, config)

    home = get_active_hermes_home()
    env_var = str(metadata.get("env_var") or "")
    if api_key and env_var:
        write_env_values(home / ".env", {env_var: api_key})
    _reload_runtime_config(
        home,
        env_var=env_var,
        api_key=api_key,
        set_env_before_dotenv=True,
    )
    invalidate_models_cache()
    result = {"ok": True, "provider": provider, "base_url": base_url}
    if do_activate:
        result["model"] = model_config.get("default")
    return result


def complete_onboarding() -> dict:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()
