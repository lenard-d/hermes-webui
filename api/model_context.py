"""Model context-window resolution shared by HTTP and run persistence."""

from __future__ import annotations

import logging
import os
import re


logger = logging.getLogger(__name__)


def _clean_session_model_provider(value: str | None) -> str | None:
    provider = str(value or "").strip().lower()
    if not provider or provider == "default":
        return None
    if provider.startswith("@"):
        provider = provider[1:]
    return provider or None

def _split_provider_qualified_model(model: str) -> tuple[str, str | None]:
    model = str(model or "").strip()
    if model.startswith("@") and ":" in model:
        provider_hint, bare_model = model[1:].rsplit(":", 1)
        provider = _clean_session_model_provider(provider_hint)
        bare = bare_model.strip()
        if provider and bare:
            return bare, provider
    return model, None

def _model_matches_configured_default(
    session_model: str | None,
    cfg_default: str | None,
    provider: str | None = None,
) -> bool:
    """Return True when ``session_model`` refers to the configured ``model.default``.

    The global ``model.context_length`` cap applies ONLY to the default model
    (#3256/#3263). An exact string compare is not enough because ``model.default``
    and the session model can be stored in different but equivalent shapes:
      - bare:            ``claude-opus-4.8``
      - slash-prefixed:  ``anthropic/claude-opus-4.8``  (OpenRouter-style)
      - @provider:model: ``@anthropic:claude-opus-4.8``

    Matching rule (correct in both directions):
      1. Identical strings → match.
      2. Otherwise compare BARE model ids — BUT only after a provider-compatibility
         check: if BOTH sides carry an identifiable provider (from a ``provider/``
         prefix, an ``@provider:`` qualifier, or the explicit ``provider`` arg for
         the session side) and those providers DIFFER, it is NOT a match. This
         stops a non-default model on a different provider that happens to share a
         bare name (``openai/gpt-4o`` vs default ``openrouter/gpt-4o``) from being
         treated as the default and wrongly receiving its cap.
      3. When a provider can't be identified on one side, fall through to the bare
         comparison (lenient-when-unknown — a bare default config still matches a
         bare/prefixed session model).
    Empty default → no match.
    """
    sess = str(session_model or "").strip()
    default = str(cfg_default or "").strip()
    if not sess or not default:
        return False
    if sess == default:
        return True

    def _split(value: str) -> tuple[str, str | None]:
        """Return (bare_model, provider_or_None) for any of the 3 shapes."""
        value = str(value or "").strip()
        # @provider:model
        unq, q_prov = _split_provider_qualified_model(value)
        if q_prov:
            return unq.strip(), str(q_prov).strip().lower()
        # provider/model (single leading slash segment)
        if "/" in value:
            prefix, rest = value.split("/", 1)
            return rest.strip(), prefix.strip().lower()
        return value, None

    sess_bare, sess_prov = _split(sess)
    default_bare, default_prov = _split(default)
    # The explicit provider arg is the session side's provider when the model
    # string itself didn't carry one.
    if not sess_prov and provider:
        sess_prov = str(provider).strip().lower() or None

    if not sess_bare or not default_bare or sess_bare != default_bare:
        return False
    # Bare ids match. Reject only when both sides name DIFFERENT providers.
    if sess_prov and default_prov and sess_prov != default_prov:
        return False
    return True

class _ContextLengthLookupInputs:
    __slots__ = ("config_context_length", "custom_providers", "base_url", "provider", "api_key")

    def __init__(
        self,
        *,
        config_context_length: int | None = None,
        custom_providers: list | None = None,
        base_url: str = "",
        provider: str = "",
        api_key: str = "",
    ) -> None:
        self.config_context_length = config_context_length
        self.custom_providers = custom_providers
        self.base_url = base_url
        self.provider = provider
        self.api_key = api_key

def _positive_context_length(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

def _model_lookup_candidates(model: str) -> tuple[str, ...]:
    raw = str(model or "").strip()
    candidates = []
    for candidate in (raw, _split_provider_qualified_model(raw)[0]):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        if "/" in candidate:
            bare = candidate.split("/", 1)[1].strip()
            if bare and bare not in candidates:
                candidates.append(bare)
    return tuple(candidates)

def _models_config_context_length(models_cfg, model: str) -> int | None:
    candidates = _model_lookup_candidates(model)
    if isinstance(models_cfg, dict):
        for candidate in candidates:
            entry = models_cfg.get(candidate)
            raw_ctx = entry.get("context_length") if isinstance(entry, dict) else entry
            ctx = _positive_context_length(raw_ctx)
            if ctx is not None:
                return ctx
    if isinstance(models_cfg, list):
        for entry in models_cfg:
            if not isinstance(entry, dict):
                continue
            entry_model = str(entry.get("id") or entry.get("model") or entry.get("name") or "").strip()
            if entry_model in candidates:
                ctx = _positive_context_length(entry.get("context_length"))
                if ctx is not None:
                    return ctx
    return None

def _canonical_context_provider(value: str | None) -> str:
    provider = _clean_session_model_provider(value) or ""
    if not provider:
        return ""
    try:
        from api.config import resolve_provider_alias

        provider = resolve_provider_alias(provider)
    except Exception:
        pass
    return str(provider or "").strip().lower()

def _custom_provider_slug_for_context(name: object) -> str:
    try:
        from api.config import custom_provider_slug_from_name

        return custom_provider_slug_from_name(name)
    except Exception:
        raw = str(name or "").strip().lower()
        if not raw:
            return ""
        if raw.startswith("custom:"):
            return raw
        slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
        slug = re.sub(r"-{2,}", "-", slug)
        return f"custom:{slug}" if slug else ""

def _providers_match_for_context(config_key: object, requested_provider: str) -> bool:
    if not requested_provider:
        return False
    raw_key = str(config_key or "").strip().lower()
    key = _canonical_context_provider(raw_key)
    requested = _canonical_context_provider(requested_provider)
    return bool(
        requested
        and (
            raw_key == requested
            or key == requested
            or raw_key == str(requested_provider or "").strip().lower()
        )
    )

def _custom_provider_api_key_for_context(entry: dict, provider: str) -> str:
    """Resolve the API key for a matched ``custom_providers`` entry.

    Static session hydration/update routes already have a per-profile config
    snapshot. Resolve from the matched entry instead of re-reading global config,
    while preserving the same literal, ``${ENV_VAR}``, ``key_env``, and
    sanitized-env shapes used by streaming/provider resolution.
    """
    raw_api_key = entry.get("api_key")
    if raw_api_key is not None:
        api_key_text = str(raw_api_key).strip()
        if api_key_text.startswith("${") and api_key_text.endswith("}") and len(api_key_text) > 3:
            env_name = api_key_text[2:-1]
            resolved = os.getenv(env_name, "").strip()
            if resolved:
                return resolved
            logger.debug(
                "Custom provider %s api_key references %s, but the environment variable is unset or empty",
                provider,
                api_key_text,
            )
        elif api_key_text:
            return api_key_text

    key_env = str(entry.get("key_env") or "").strip()
    if key_env:
        resolved = os.getenv(key_env, "").strip()
        if resolved:
            return resolved

    try:
        from api.config import lookup_custom_api_key_env

        return lookup_custom_api_key_env(provider) or ""
    except Exception:
        return ""

def _context_length_config_api_key_for_provider(
    provider: str | None,
    cfg: dict | None,
) -> str:
    """Return a config/env API key usable for context-window metadata lookup."""
    cfg = cfg if isinstance(cfg, dict) else {}
    provider = _canonical_context_provider(provider)

    def _resolve_key(raw_api_key, raw_key_env=None) -> str:
        api_key_text = str(raw_api_key or "").strip()
        if (
            api_key_text.startswith("${")
            and api_key_text.endswith("}")
            and len(api_key_text) > 3
        ):
            resolved = os.getenv(api_key_text[2:-1], "").strip()
            if resolved:
                return resolved
        elif api_key_text:
            return api_key_text
        key_env = str(raw_key_env or "").strip()
        if key_env:
            resolved = os.getenv(key_env, "").strip()
            if resolved:
                return resolved
        return ""

    providers_cfg = cfg.get("providers") or {}
    if isinstance(providers_cfg, dict):
        for provider_key, provider_cfg in providers_cfg.items():
            if not isinstance(provider_cfg, dict):
                continue
            if not _providers_match_for_context(provider_key, provider):
                continue
            api_key = _resolve_key(provider_cfg.get("api_key"), provider_cfg.get("key_env"))
            if api_key:
                return api_key

    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        model_provider = _canonical_context_provider(model_cfg.get("provider"))
        if not provider or _providers_match_for_context(model_provider, provider):
            api_key = _resolve_key(model_cfg.get("api_key"), model_cfg.get("key_env"))
            if api_key:
                return api_key
    return ""

def _context_length_lookup_inputs_for_model(
    model: str | None,
    provider: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    cfg: dict | None = None,
) -> _ContextLengthLookupInputs:
    """Return the effective metadata resolver inputs for a WebUI model.

    ``agent.model_metadata.get_model_context_length`` understands global
    ``config_context_length`` and custom-provider overrides, but only when the
    matching base URL is supplied. WebUI also owns ``providers.<provider>.models``
    overrides, so normalize those here and keep route/session-save/SSE aligned.
    """
    model_for_lookup = str(model or "").strip()
    if not model_for_lookup:
        return _ContextLengthLookupInputs()

    if cfg is None:
        try:
            from api.config import get_config as _get_config_for_cl

            cfg = _get_config_for_cl()
        except Exception:
            cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}

    bare_model, explicit_provider = _split_provider_qualified_model(model_for_lookup)
    effective_provider = _canonical_context_provider(provider or explicit_provider)
    effective_base_url = str(base_url or "").strip()

    model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    if isinstance(model_cfg, dict):
        if not effective_provider:
            effective_provider = _canonical_context_provider(model_cfg.get("provider"))
        if not effective_base_url:
            effective_base_url = str(model_cfg.get("base_url") or "").strip()

    custom_providers = cfg.get("custom_providers") if isinstance(cfg, dict) else None
    if not isinstance(custom_providers, list):
        custom_providers = None

    provider_context_length = None
    providers_cfg = (cfg.get("providers") or {}) if isinstance(cfg, dict) else {}
    if isinstance(providers_cfg, dict):
        for provider_key, provider_cfg in providers_cfg.items():
            if not isinstance(provider_cfg, dict):
                continue
            if not _providers_match_for_context(provider_key, effective_provider):
                continue
            if not effective_base_url:
                effective_base_url = str(provider_cfg.get("base_url") or "").strip()
            provider_context_length = _models_config_context_length(
                provider_cfg.get("models"),
                bare_model or model_for_lookup,
            )
            break

    custom_context_length = None
    effective_api_key = str(api_key or "").strip()
    if custom_providers:
        target_base = effective_base_url.rstrip("/")
        model_candidates = set(_model_lookup_candidates(bare_model or model_for_lookup))
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            entry_name = str(entry.get("name") or "").strip()
            entry_slug = _custom_provider_slug_for_context(entry_name)
            entry_base = str(entry.get("base_url") or "").strip()
            entry_base_norm = entry_base.rstrip("/")
            provider_matches = bool(
                effective_provider
                and (
                    effective_provider == entry_slug
                    or effective_provider == entry_name.lower()
                    or (effective_provider == "custom" and len(custom_providers) == 1)
                )
            )
            base_matches = bool(target_base and entry_base_norm and target_base == entry_base_norm)
            model_matches = bool(model_candidates.intersection(set(_model_lookup_candidates(entry.get("model")))))
            models_cfg = entry.get("models")
            if isinstance(models_cfg, dict):
                model_matches = model_matches or any(candidate in models_cfg for candidate in model_candidates)
            if not (provider_matches or base_matches or (not effective_provider and model_matches)):
                continue
            if not effective_provider and entry_slug:
                effective_provider = entry_slug
            if not effective_base_url and entry_base:
                effective_base_url = entry_base
            if not effective_api_key:
                effective_api_key = _custom_provider_api_key_for_context(entry, effective_provider or entry_slug)
            custom_context_length = _models_config_context_length(models_cfg, bare_model or model_for_lookup)
            break

    global_context_length = None
    if isinstance(model_cfg, dict):
        cfg_default_model = str(model_cfg.get("default") or "").strip()
        raw_cfg_ctx = model_cfg.get("context_length")
        if raw_cfg_ctx is not None and (
            not cfg_default_model
            or _model_matches_configured_default(
                model_for_lookup,
                cfg_default_model,
                effective_provider,
            )
        ):
            global_context_length = _positive_context_length(raw_cfg_ctx)

    if not effective_api_key:
        effective_api_key = _context_length_config_api_key_for_provider(effective_provider, cfg)

    return _ContextLengthLookupInputs(
        config_context_length=provider_context_length or custom_context_length or global_context_length,
        custom_providers=custom_providers,
        base_url=effective_base_url,
        provider=effective_provider,
        api_key=effective_api_key,
    )

def _should_accept_session_context_length_refresh(
    persisted: int,
    resolved: int,
    *,
    model_changed: bool = False,
) -> bool:
    if not resolved:
        return False
    if not persisted:
        return True
    # #4248: an anonymous reload resolver can still fall through to the agent
    # metadata default fallback. Do not let that lower-confidence 256k value
    # clobber a larger context window persisted by the streaming path. If the
    # effective model changed, though, a 256k result may be the real new model
    # window and should replace the old snapshot.
    return model_changed or not (resolved == 256_000 and persisted > resolved)
