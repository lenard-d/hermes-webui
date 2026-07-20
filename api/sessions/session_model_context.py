"""Context-window lookup and refresh projection for session model state."""

from __future__ import annotations

import logging

from api.config import model_with_provider_context
from api.model_context import (
    _canonical_context_provider,
    _context_length_lookup_inputs_for_model,
    _split_provider_qualified_model,
)


logger = logging.getLogger(__name__)


def _resolve_context_length_for_session_model(
    model: str | None,
    provider: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> int:
    """Return the best-effort current context window for a session model."""
    model_for_lookup = str(model or "").strip()
    if not model_for_lookup:
        return 0
    try:
        from agent.model_metadata import get_model_context_length
        from api.config import get_config

        config = get_config()
        lookup = _context_length_lookup_inputs_for_model(
            model_for_lookup,
            provider,
            base_url=base_url,
            api_key=api_key,
            cfg=config if isinstance(config, dict) else {},
        )
        try:
            return (
                get_model_context_length(
                    model_for_lookup,
                    lookup.base_url,
                    api_key=lookup.api_key,
                    config_context_length=lookup.config_context_length,
                    provider=lookup.provider or provider or "",
                    custom_providers=lookup.custom_providers,
                )
                or 0
            )
        except TypeError:
            return get_model_context_length(model_for_lookup, lookup.base_url) or 0
    except Exception:
        return 0


def _session_context_length_lookup_state(
    model: str | None,
    provider: str | None,
) -> tuple[str, str, str, str]:
    """Return side-effect-free model/provider connection inputs for lookup."""
    model_for_lookup = str(model or "").strip()
    provider_for_lookup = str(provider or "").strip()
    base_url_for_lookup = ""
    api_key_for_lookup = ""
    if not model_for_lookup:
        return "", provider_for_lookup, "", ""
    try:
        from api.config import resolve_model_provider

        model_for_resolution = model_with_provider_context(
            model_for_lookup, provider_for_lookup or None
        )
        resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(
            model_for_resolution
        )
        model_for_lookup = str(resolved_model or model_for_lookup).strip()
        provider_for_lookup = str(
            resolved_provider or provider_for_lookup or ""
        ).strip()
        base_url_for_lookup = str(resolved_base_url or "").strip()
    except Exception:
        logger.debug(
            "session context-length lookup state resolution failed", exc_info=True
        )
    if provider_for_lookup.startswith("custom:"):
        try:
            from api.config import resolve_custom_provider_connection

            custom_key, custom_base = resolve_custom_provider_connection(
                provider_for_lookup
            )
            api_key_for_lookup = str(custom_key or "").strip()
            if not base_url_for_lookup:
                base_url_for_lookup = str(custom_base or "").strip()
        except Exception:
            logger.debug(
                "custom provider context-length connection resolution failed",
                exc_info=True,
            )
    return (
        model_for_lookup,
        provider_for_lookup,
        base_url_for_lookup,
        api_key_for_lookup,
    )


def _session_model_identity_matches(
    stored_model: str | None,
    stored_provider: str | None,
    resolved_model: str | None,
    resolved_provider: str | None,
) -> bool:
    stored = str(stored_model or "").strip()
    resolved = str(resolved_model or "").strip()
    if not stored or not resolved:
        return False

    def split_model_identity(value: str) -> tuple[str, str | None]:
        bare, explicit_provider = _split_provider_qualified_model(value)
        if explicit_provider is None and "/" in value:
            prefix, rest = value.split("/", 1)
            prefix = prefix.strip()
            rest = rest.strip()
            if prefix and rest:
                return rest, prefix
        return bare, explicit_provider

    stored_bare, stored_explicit_provider = split_model_identity(stored)
    resolved_bare, resolved_explicit_provider = split_model_identity(resolved)
    stored_provider_normalized = _canonical_context_provider(
        stored_explicit_provider or stored_provider
    )
    resolved_provider_normalized = _canonical_context_provider(
        resolved_explicit_provider or resolved_provider
    )
    if (
        stored == resolved
        and stored_provider_normalized == resolved_provider_normalized
    ):
        return True
    if stored_bare != resolved_bare:
        return False
    if stored_provider_normalized and resolved_provider_normalized:
        return stored_provider_normalized == resolved_provider_normalized
    return True


def _rescale_threshold_tokens_for_context_window(
    threshold: int,
    old_window: int,
    new_window: int,
) -> int:
    try:
        threshold = int(threshold or 0)
        old_window = int(old_window or 0)
        new_window = int(new_window or 0)
    except (TypeError, ValueError):
        return 0
    if threshold <= 0 or old_window <= 0 or new_window <= 0:
        return 0
    return max(1, int(threshold * new_window / old_window))
