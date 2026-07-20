"""Public reasoning configuration interface and capability resolution.

This module coordinates profile/provider context, explicit overrides, external
capability sources, and status projection.  Model identity, provider policy,
and live probes live behind focused internal modules while the established
``api.config`` interface remains stable for callers and tests.
"""

from api import config as _config_module
from api.config import reasoning_identity as _identity
from api.config import reasoning_policy as _policy
from api.config import reasoning_probe as _probe

_NESTED_ROUTE_PATTERN = _identity.NESTED_ROUTE_PATTERN
_candidate_supports_reasoning = _identity.candidate_supports_reasoning
_is_pre_adaptive_anthropic = _identity.is_pre_adaptive_anthropic
_nested_gateway_route_reasoning = _identity.nested_gateway_route_reasoning
_nested_route_reasoning_denied = _identity.nested_route_reasoning_denied
_reasoning_name_candidates = _identity.reasoning_name_candidates
_strip_provider_hint_for_reasoning = _identity.strip_provider_hint_for_reasoning

_KNOWN_REASONING_PROVIDERS = _policy.KNOWN_REASONING_PROVIDERS
coerce_reasoning_effort_for_model = _policy.coerce_reasoning_effort_for_model
_filter_reasoning_efforts_for_provider = _policy.filter_reasoning_efforts_for_provider
_heuristic_reasoning_efforts = _policy.heuristic_reasoning_efforts
_provider_known_reasoning_capable = _policy.provider_known_reasoning_capable
_zai_glm_classification = _policy.zai_glm_classification
_zai_glm_reasoning_efforts_supported = _policy.zai_glm_reasoning_efforts_supported
_zai_glm_thinking_toggle_supported = _policy.zai_glm_thinking_toggle_supported

_NoRedirectHandler = _probe.NoRedirectHandler
_get_lmstudio_reasoning_probe_api_key = _probe.get_lmstudio_reasoning_probe_api_key
_lmstudio_model_reasoning_options = _probe.lmstudio_model_reasoning_options
_lmstudio_reasoning_probe_options_fallback = (
    _probe.lmstudio_reasoning_probe_options_fallback
)
_models_dev_reasoning_efforts = _probe.models_dev_reasoning_efforts


VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def parse_reasoning_effort(effort):
    """Parse a persisted effort into the structure expected by Hermes Agent."""
    if not effort or not str(effort).strip():
        return None
    normalized = str(effort).strip().lower()
    if normalized == "none":
        return {"enabled": False}
    if normalized in _config_module.VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": normalized}
    return None


def resolve_model_reasoning_efforts(
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> list[str]:
    """Return the supported, provider-safe effort ladder for a model."""
    api = _config_module
    raw = api._resolve_model_reasoning_efforts_impl(model_id, provider_id, base_url)
    if not raw:
        return raw
    if api._zai_glm_classification(model_id, provider_id) == "forced":
        return []

    had_none = "none" in raw
    filtered = api._filter_reasoning_efforts_for_provider(
        [effort for effort in raw if effort != "none"],
        str(model_id or ""),
        str(provider_id or ""),
    )
    if not had_none:
        return filtered
    return ["none", *filtered] if raw[0] == "none" else [*filtered, "none"]


def _configured_reasoning_efforts(provider: str) -> list[str] | None:
    """Return a valid explicit provider override, or None to continue discovery."""
    api = _config_module
    configured = None
    try:
        if provider.startswith("custom:"):
            for entry in api._custom_provider_entries():
                if api._custom_provider_slug_from_name(entry.get("name")) == provider:
                    configured = entry.get("reasoning_efforts")
                    break
        else:
            provider_entry = (api.cfg.get("providers") or {}).get(provider, {})
            if isinstance(provider_entry, dict):
                configured = provider_entry.get("reasoning_efforts")
    except Exception:
        return None

    if not isinstance(configured, list) or not configured:
        return None
    valid = [
        str(value).strip().lower()
        for value in configured
        if str(value).strip().lower()
        in {*api.VALID_REASONING_EFFORTS, "none"}
    ]
    return list(dict.fromkeys(valid)) or None


def _resolve_lmstudio_reasoning_efforts(
    model: str,
    resolved_base_url: str | None,
) -> list[str]:
    """Probe LM Studio without forwarding credentials to an untrusted target."""
    api = _config_module
    configured_base = api._get_provider_base_url("lmstudio")
    probe_base = resolved_base_url or configured_base
    probe_key: str | None = None
    if not resolved_base_url or (
        configured_base
        and api._normalize_base_url_for_match(probe_base)
        == api._normalize_base_url_for_match(configured_base)
    ):
        probe_key = api._get_lmstudio_reasoning_probe_api_key()

    options = api._lmstudio_model_reasoning_options(
        model,
        probe_base,
        api_key=probe_key,
    )
    normalized = [str(option).strip().lower() for option in options if str(option).strip()]
    if not normalized or set(normalized).issubset({"off"}):
        return []
    levels = [
        option for option in normalized if option in api.VALID_REASONING_EFFORTS
    ]
    if levels:
        return api._filter_reasoning_efforts_for_provider(levels, model, "lmstudio")
    return []


def _resolve_model_reasoning_efforts_impl(
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> list[str]:
    """Resolve raw effort support from overrides and ordered capability sources."""
    api = _config_module
    model = str(model_id or "").strip()
    if not model:
        return []

    provider = str(provider_id or "").strip().lower() if provider_id else ""
    resolved_base_url = str(base_url or "").strip() or None
    if not provider:
        try:
            _, provider, resolved_base_url = api.resolve_model_provider(model)
        except Exception:
            provider = str((api.cfg.get("model") or {}).get("provider") or "").strip().lower()

    provider = api._resolve_provider_alias(provider)
    if provider in {"cursor-acp", "copilot-acp"}:
        return []

    hinted_model = api._strip_provider_hint_for_reasoning(model, provider)
    if api._nested_route_reasoning_denied(hinted_model):
        return []

    explicit = _configured_reasoning_efforts(provider)
    if explicit:
        return explicit

    if provider in {"copilot", "github-copilot"}:
        try:
            from hermes_cli.models import github_model_reasoning_efforts
        except Exception:
            return api._heuristic_reasoning_efforts(hinted_model, provider)
        return api._filter_reasoning_efforts_for_provider(
            github_model_reasoning_efforts(hinted_model), hinted_model, provider
        )

    if provider == "lmstudio":
        return _resolve_lmstudio_reasoning_efforts(hinted_model, resolved_base_url)

    metadata_efforts = api._models_dev_reasoning_efforts(hinted_model, provider)
    if metadata_efforts is not None:
        return metadata_efforts
    return api._heuristic_reasoning_efforts(hinted_model, provider)


def get_reasoning_status(
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Project the active profile's reasoning config for the composer."""
    api = _config_module
    config_data = api._load_yaml_config_file(api._get_config_path())
    display_cfg = config_data.get("display") or {}
    agent_cfg = config_data.get("agent") or {}
    show_raw = (
        display_cfg.get("show_reasoning") if isinstance(display_cfg, dict) else None
    )
    effort_raw = (
        agent_cfg.get("reasoning_effort") if isinstance(agent_cfg, dict) else None
    )

    resolve_model = model_id
    resolve_provider = provider_id
    resolve_base_url = base_url
    if not resolve_model:
        model_cfg = config_data.get("model") or {}
        if isinstance(model_cfg, dict):
            resolve_model = str(model_cfg.get("default") or "").strip() or None
            if not resolve_provider and model_cfg.get("provider"):
                resolve_provider = str(model_cfg["provider"]).strip()
            if not resolve_base_url and model_cfg.get("base_url"):
                resolve_base_url = str(model_cfg["base_url"]).strip()

    supported_efforts = api.resolve_model_reasoning_efforts(
        resolve_model,
        provider_id=resolve_provider,
        base_url=resolve_base_url,
    )
    zai_thinking = api._zai_glm_thinking_toggle_supported(
        resolve_model, resolve_provider
    )
    return {
        "show_reasoning": bool(show_raw) if isinstance(show_raw, bool) else True,
        "reasoning_effort": api.coerce_reasoning_effort_for_model(
            str(effort_raw or "").strip().lower(),
            resolve_model,
            provider_id=resolve_provider,
            base_url=resolve_base_url,
        ),
        "supported_efforts": supported_efforts,
        "supports_reasoning_effort": bool(supported_efforts),
        "supports_thinking_toggle": bool(supported_efforts) or (zai_thinking is True),
    }
