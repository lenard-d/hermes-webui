"""Authoritative and live capability probes for reasoning options.

The resolver delegates all external capability discovery here.  Network
credentials and redirect policy stay local to the LM Studio adapter so callers
cannot accidentally separate target validation from credential forwarding.
"""

import json
import os
import urllib.error
import urllib.request

from api import config as _config_module


def models_dev_reasoning_efforts(
    model_id: str, provider_id: str
) -> list[str] | None:
    """Return Hermes Agent model-metadata efforts when it has an answer."""
    api = _config_module
    model = api._strip_provider_hint_for_reasoning(model_id)
    provider = str(provider_id or "").strip().lower()
    if not model or not provider:
        return None

    try:
        from agent.models_dev import get_model_capabilities
    except Exception:
        return None

    try:
        capabilities = get_model_capabilities(provider=provider, model=model)
    except Exception:
        return None
    if capabilities is None:
        return None

    supports_reasoning = getattr(capabilities, "supports_reasoning", None)
    if supports_reasoning is True:
        return api._filter_reasoning_efforts_for_provider(
            list(api.VALID_REASONING_EFFORTS), model, provider
        )
    if supports_reasoning is False:
        return []
    return None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so probe credentials cannot escape their endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_lmstudio_reasoning_probe_api_key() -> str | None:
    """Resolve the LM Studio key with WebUI config/environment precedence."""
    config_data = _config_module.cfg
    model_cfg = config_data.get("model") or {}
    if isinstance(model_cfg, dict):
        active_provider = str(model_cfg.get("provider") or "").strip().lower()
        model_key = str(model_cfg.get("api_key") or "").strip()
        if active_provider == "lmstudio" and model_key:
            return model_key

    providers_cfg = config_data.get("providers") or {}
    if isinstance(providers_cfg, dict):
        lmstudio_cfg = providers_cfg.get("lmstudio") or {}
        if isinstance(lmstudio_cfg, dict):
            config_key = str(lmstudio_cfg.get("api_key") or "").strip()
            if config_key:
                return config_key

    env_key = str(os.getenv("LM_API_KEY") or "").strip()
    if env_key:
        return env_key
    legacy_env_key = str(os.getenv("LMSTUDIO_API_KEY") or "").strip()
    return legacy_env_key or None


def lmstudio_reasoning_probe_options_fallback(
    model: str,
    base_url: str | None,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Query LM Studio directly through the credential-safe probe adapter."""
    server_root = str(base_url or "").strip().rstrip("/")
    if server_root.endswith("/v1"):
        server_root = server_root[:-3].rstrip("/")
    if not server_root or not model:
        return []

    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-webui-reasoning-probe",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        server_root + "/api/v1/models",
        headers=headers,
        method="GET",
    )
    api = _config_module
    opener = urllib.request.build_opener(api._NoRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        api.logger.debug(
            "LM Studio reasoning probe at %s failed with HTTP %s",
            server_root,
            exc.code,
        )
        return []
    except Exception as exc:
        api.logger.debug("LM Studio reasoning probe at %s failed: %s", server_root, exc)
        return []

    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        api.logger.debug(
            "LM Studio reasoning probe at %s returned malformed payload", server_root
        )
        return []

    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        if raw.get("key") != model and raw.get("id") != model:
            continue
        capabilities = raw.get("capabilities")
        reasoning = (
            capabilities.get("reasoning") if isinstance(capabilities, dict) else None
        )
        options = (
            reasoning.get("allowed_options") if isinstance(reasoning, dict) else None
        )
        if isinstance(options, list):
            return [
                str(option).strip().lower()
                for option in options
                if isinstance(option, str)
            ]
        return []
    return []


def lmstudio_model_reasoning_options(
    model: str,
    base_url: str | None,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Prefer hermes_cli for keyless probes; own credentialed probes locally."""
    api = _config_module
    if api_key:
        return api._lmstudio_reasoning_probe_options_fallback(
            model, base_url, api_key=api_key, timeout=timeout
        )

    try:
        from hermes_cli.models import (
            lmstudio_model_reasoning_options as cli_lmstudio_model_reasoning_options,
        )
    except Exception:
        return api._lmstudio_reasoning_probe_options_fallback(
            model, base_url, api_key=api_key, timeout=timeout
        )

    try:
        return cli_lmstudio_model_reasoning_options(
            model,
            base_url,
            api_key=api_key,
            timeout=timeout,
        )
    except (TypeError, AttributeError):
        api.logger.warning(
            "hermes_cli.lmstudio_model_reasoning_options has an unexpected signature; "
            "falling back to the built-in LM Studio reasoning probe",
            exc_info=True,
        )
    except Exception:
        pass
    return api._lmstudio_reasoning_probe_options_fallback(
        model, base_url, api_key=api_key, timeout=timeout
    )
