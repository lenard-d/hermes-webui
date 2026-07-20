"""Provider identity and custom-endpoint discovery helpers.

The model catalog and active configuration remain owned by ``api.config`` for
compatibility.  Helpers resolve that facade at call time so existing patches of
``cfg``, provider tables, plugin hooks, and environment readers remain effective.
"""

import re
from types import ModuleType
from typing import Protocol, cast
from urllib.parse import urlparse

from api.config_parts.facade import config_api


class ProviderDiscoveryAPI(Protocol):
    cfg: dict
    _PROVIDER_ALIASES: dict
    _PROVIDER_DISPLAY: dict
    _PROVIDER_MODELS: dict
    _LEGACY_CUSTOM_API_KEY_ENV_WARNED: set[str]
    logger: object

    def _is_plugin_model_provider(self, provider_id: str) -> bool: ...
    def _thread_local_env_value(self, name: str) -> str: ...
    def _custom_provider_slug_from_name(self, name: object) -> str: ...
    def _custom_provider_entries(self, config_obj: dict | None = None) -> list[dict]: ...
    def _configured_model_ids(self, raw_models: object) -> list[str]: ...
    def _named_custom_provider_slug_for_provider(
        self, provider: object, config_obj: dict | None = None
    ) -> str: ...
    def _named_custom_provider_slug_for_base_url(
        self, base_url: object, config_obj: dict | None = None
    ) -> str: ...
    def _normalize_base_url_for_match(self, value: object) -> str: ...
    def _resolve_provider_alias(self, name: str) -> str: ...
    def _api_key_env_name(self, provider_id: object) -> str: ...
    def _legacy_custom_api_key_env_name(self, provider_id: object) -> str: ...


def _config_api() -> ProviderDiscoveryAPI:
    return cast(ProviderDiscoveryAPI, cast(ModuleType, config_api()))


def _get_anthropic_fallback_env_vars() -> tuple[str, ...]:
    """Read Anthropic auth env vars from the shared agent registry when available."""
    fallback = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    )
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        anthropic = (
            PROVIDER_REGISTRY.get("anthropic")
            if isinstance(PROVIDER_REGISTRY, dict)
            else None
        )
        env_vars = getattr(anthropic, "api_key_env_vars", None)
        if not env_vars:
            return fallback

        out = []
        for _var in env_vars:
            if not isinstance(_var, str):
                continue
            _normalized = _var.strip()
            if _normalized and _normalized not in out:
                out.append(_normalized)
        return tuple(out) if out else fallback
    except Exception:
        return fallback


def _resolve_provider_alias(name: str) -> str:
    """Return the canonical provider slug for *name*.

    Applies the WebUI's local alias table first, then merges any additional
    aliases the agent provides when hermes_cli is on ``sys.path``.
    """
    if not name:
        return name
    raw = str(name).strip().lower()
    try:
        from hermes_cli.models import _PROVIDER_ALIASES as _agent_aliases

        if raw in _agent_aliases:
            return _agent_aliases[raw]
    except Exception:
        pass
    return _config_api()._PROVIDER_ALIASES.get(raw, name)


def _is_known_model_provider(provider_id: str) -> bool:
    """Return whether WebUI can render the model provider in its picker."""
    pid = (provider_id or "").strip().lower()
    if not pid:
        return False
    if pid.startswith("custom:"):
        return True
    api = _config_api()
    if pid in api._PROVIDER_DISPLAY or pid in api._PROVIDER_MODELS:
        return True
    try:
        if api._is_plugin_model_provider(pid):
            return True
    except Exception:
        api.logger.warning(
            "plugin model-provider check failed for %s", pid, exc_info=True
        )
    return False


def _custom_provider_slug_from_name(name: object) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("custom:"):
        return raw
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return ""
    return "custom:" + slug


def _custom_provider_entries(config_obj: dict | None = None) -> list[dict]:
    source = config_obj if isinstance(config_obj, dict) else _config_api().cfg
    entries = source.get("custom_providers", [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _configured_model_ids(raw_models: object) -> list[str]:
    """Return ordered model IDs from supported config allowlist shapes."""
    if isinstance(raw_models, dict):
        candidates = (key for key in raw_models if isinstance(key, str))
    elif isinstance(raw_models, list):
        candidates = raw_models
    else:
        return []

    model_ids: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            candidate = item.get("id") or item.get("model") or item.get("name")
        else:
            candidate = item
        model_id = str(candidate or "").strip()
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)
    return model_ids


def _configured_model_options(raw_models: object) -> list[dict[str, str]]:
    """Return picker option rows from supported config allowlist shapes."""
    labels: dict[str, str] = {}
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            candidate = item.get("id") or item.get("model") or item.get("name")
            model_id = str(candidate or "").strip()
            if not model_id or model_id in labels:
                continue
            label = str(item.get("label") or model_id).strip() or model_id
            labels[model_id] = label
    return [
        {"id": model_id, "label": labels.get(model_id, model_id)}
        for model_id in _config_api()._configured_model_ids(raw_models)
    ]


def _named_custom_provider_slugs(config_obj: dict | None = None) -> set[str]:
    api = _config_api()
    return {
        slug
        for slug in (
            api._custom_provider_slug_from_name(entry.get("name"))
            for entry in api._custom_provider_entries(config_obj)
        )
        if slug
    }


def _named_custom_provider_slug_for_provider(
    provider: object,
    config_obj: dict | None = None,
) -> str:
    raw = str(provider or "").strip().lower()
    if not raw:
        return ""
    api = _config_api()
    raw_suffix = raw.removeprefix("custom:")
    for entry in api._custom_provider_entries(config_obj):
        entry_name = str(entry.get("name") or "").strip().lower()
        slug = api._custom_provider_slug_from_name(entry_name)
        if not entry_name or not slug:
            continue
        if raw in {entry_name, slug} or raw_suffix == slug.removeprefix("custom:"):
            return slug
    return ""


def _resolve_configured_provider_id(
    provider: object,
    config_obj: dict | None = None,
    *,
    base_url: object = None,
    resolve_alias: bool = True,
) -> str:
    """Normalize configured and named-custom provider identifiers."""
    api = _config_api()
    named_slug = api._named_custom_provider_slug_for_provider(provider, config_obj)
    if named_slug:
        return named_slug

    if not resolve_alias:
        raw = str(provider or "").strip().lower()
        if base_url and raw == "custom":
            by_base_url = api._named_custom_provider_slug_for_base_url(
                base_url, config_obj
            )
            if by_base_url:
                return by_base_url
        return str(provider or "")

    resolved = api._resolve_provider_alias(provider)
    if base_url and str(resolved or "").strip().lower() == "custom":
        by_base_url = api._named_custom_provider_slug_for_base_url(base_url, config_obj)
        if by_base_url:
            return by_base_url
    return resolved


def _canonicalise_provider_id(name: object) -> str:
    """Normalise a provider id into a stable lowercase-hyphenated form."""
    if not name:
        return ""
    raw = str(name).strip().lower().replace("_", "-")
    if not raw:
        return ""
    api = _config_api()
    if raw in api._PROVIDER_DISPLAY or raw in api._PROVIDER_MODELS:
        return raw
    resolved = api._resolve_provider_alias(raw)
    if resolved and (
        resolved.lower() in api._PROVIDER_DISPLAY
        or resolved.lower() in api._PROVIDER_MODELS
    ):
        return resolved.lower()
    return raw


def _normalize_base_url_for_match(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed_url = urlparse(url if "://" in url else f"http://{url}")
    scheme = (parsed_url.scheme or "http").lower()
    netloc = (parsed_url.netloc or parsed_url.path).lower().rstrip("/")
    path = parsed_url.path.rstrip("/")
    if not parsed_url.netloc:
        path = ""
    return f"{scheme}://{netloc}{path}"


def _custom_endpoint_slugs_for_base_url(value: object) -> set[str]:
    """Return endpoint-derived custom provider slugs for a base URL."""
    url = str(value or "").strip().rstrip("/")
    if not url:
        return set()
    parsed_url = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed_url.hostname or "").strip().lower()
    if not host:
        return set()
    port = parsed_url.port
    if port is None:
        scheme = (parsed_url.scheme or "http").lower()
        port = 443 if scheme == "https" else 80
    return {f"custom:{host}:{port}", f"custom:{host}-{port}"}


def _api_key_env_name(provider_id: object) -> str:
    """Return the POSIX-safe default API-key env var for a custom provider id."""
    sanitized = re.sub(r"[^A-Za-z0-9]", "_", str(provider_id or "")).upper().strip("_")
    if not sanitized:
        sanitized = "CUSTOM"
    if not sanitized.startswith("CUSTOM_"):
        sanitized = f"CUSTOM_{sanitized}"
    return f"{sanitized}_API_KEY"


def _legacy_custom_api_key_env_name(provider_id: object) -> str:
    """Return the pre-#2541 custom-provider env hint shape, if any."""
    raw = str(provider_id or "").strip().upper()
    if not raw:
        return ""
    return f"{raw}_API_KEY"


def _lookup_custom_api_key_env(provider_id: object) -> str | None:
    """Look up sanitized custom-provider env first, then legacy broken shape."""
    api = _config_api()
    env_name = api._api_key_env_name(provider_id)
    api_key = api._thread_local_env_value(env_name).strip()
    if api_key:
        return api_key

    legacy_env_name = api._legacy_custom_api_key_env_name(provider_id)
    if legacy_env_name and legacy_env_name != env_name:
        legacy_key = api._thread_local_env_value(legacy_env_name).strip()
        if legacy_key:
            if legacy_env_name not in api._LEGACY_CUSTOM_API_KEY_ENV_WARNED:
                api._LEGACY_CUSTOM_API_KEY_ENV_WARNED.add(legacy_env_name)
                api.logger.warning(
                    "Custom provider API key env var %s is deprecated; use %s instead",
                    legacy_env_name,
                    env_name,
                )
            return legacy_key
    return None


def _named_custom_provider_slug_for_base_url(
    base_url: object,
    config_obj: dict | None = None,
) -> str:
    api = _config_api()
    target = api._normalize_base_url_for_match(base_url)
    if not target:
        return ""
    for entry in api._custom_provider_entries(config_obj):
        entry_base_url = api._normalize_base_url_for_match(entry.get("base_url"))
        if entry_base_url != target:
            continue
        return api._custom_provider_slug_from_name(entry.get("name")) or "custom"
    return ""


def _provider_is_known_or_configured(
    provider_id: object,
    config_obj: dict | None = None,
) -> bool:
    """Return whether static registry or user config recognizes a provider."""
    raw = str(provider_id or "").strip().lower()
    if not raw:
        return False
    api = _config_api()
    if api._named_custom_provider_slug_for_provider(raw, config_obj):
        return True
    if raw == "custom" or raw.startswith("custom:"):
        return bool(api._custom_provider_entries(config_obj))
    canonical = api._resolve_provider_alias(raw)
    return (
        raw in api._PROVIDER_DISPLAY
        or canonical in api._PROVIDER_DISPLAY
        or raw in api._PROVIDER_MODELS
        or canonical in api._PROVIDER_MODELS
    )
