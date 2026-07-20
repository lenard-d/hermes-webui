"""Temporary route-facade adapter for session model ownership modules."""

# Compatibility exports are consumed dynamically by ``install_routes_part``.
# ruff: noqa: F401

from api.model_context import (
    _ContextLengthLookupInputs,
    _canonical_context_provider,
    _clean_session_model_provider,
    _context_length_config_api_key_for_provider,
    _context_length_lookup_inputs_for_model,
    _custom_provider_api_key_for_context,
    _custom_provider_slug_for_context,
    _model_lookup_candidates,
    _model_matches_configured_default,
    _models_config_context_length,
    _positive_context_length,
    _providers_match_for_context,
    _should_accept_session_context_length_refresh,
    _split_provider_qualified_model,
)
from api.sessions.custom_model_identity import (
    _ordered_custom_provider_model_ids,
    _repair_bare_custom_provider_model,
)
from api.sessions.model_compatibility import (
    _moa_fast_path_model_state,
    _resolve_compatible_session_model_state,
)
from api.sessions.model_identity import (
    _catalog_group_owns_exact_model,
    _catalog_has_provider,
    _catalog_model_id_matches,
    _catalog_provider_id_sets,
    _model_matches_active_provider_family,
    _normalize_provider_id,
    _repair_foreign_session_model_provider,
    _should_attach_codex_provider_context,
    _starts_token,
)
from api.sessions.profile_model_config import (
    _PROFILE_CONFIG_CACHE,
    _PROFILE_CONFIG_CACHE_LOCK,
    _PROFILE_CONFIG_CACHE_TTL_SECONDS,
    _load_profile_config_dict,
    _read_profile_config_cached,
    _read_profile_model_config,
    _worktree_default_from_config,
)
from api.sessions.session_model_context import (
    _rescale_threshold_tokens_for_context_window,
    _resolve_context_length_for_session_model,
    _session_context_length_lookup_state,
    _session_model_identity_matches,
)
from api.sessions.session_model_state import (
    _normalize_session_model_in_place,
    _resolve_compatible_session_model,
    _resolve_effective_session_model_for_display,
    _resolve_effective_session_model_provider_for_display,
    _session_model_state_from_request,
)


__routes_exports__ = (
    "_starts_token",
    "_normalize_provider_id",
    "_catalog_provider_id_sets",
    "_catalog_has_provider",
    "_model_matches_active_provider_family",
    "_catalog_model_id_matches",
    "_catalog_group_owns_exact_model",
    "_repair_foreign_session_model_provider",
    "_clean_session_model_provider",
    "_split_provider_qualified_model",
    "_model_matches_configured_default",
    "_ContextLengthLookupInputs",
    "_positive_context_length",
    "_model_lookup_candidates",
    "_models_config_context_length",
    "_canonical_context_provider",
    "_custom_provider_slug_for_context",
    "_providers_match_for_context",
    "_custom_provider_api_key_for_context",
    "_context_length_config_api_key_for_provider",
    "_context_length_lookup_inputs_for_model",
    "_should_attach_codex_provider_context",
    "_read_profile_model_config",
    "_PROFILE_CONFIG_CACHE",
    "_PROFILE_CONFIG_CACHE_TTL_SECONDS",
    "_PROFILE_CONFIG_CACHE_LOCK",
    "_read_profile_config_cached",
    "_load_profile_config_dict",
    "_ordered_custom_provider_model_ids",
    "_repair_bare_custom_provider_model",
    "_moa_fast_path_model_state",
    "_resolve_compatible_session_model_state",
    "_resolve_compatible_session_model",
    "_normalize_session_model_in_place",
    "_resolve_effective_session_model_for_display",
    "_resolve_effective_session_model_provider_for_display",
    "_resolve_context_length_for_session_model",
    "_session_context_length_lookup_state",
    "_session_model_identity_matches",
    "_should_accept_session_context_length_refresh",
    "_rescale_threshold_tokens_for_context_window",
    "_worktree_default_from_config",
    "_session_model_state_from_request",
)
