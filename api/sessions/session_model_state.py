"""Session mutation and display projection for compatible model state."""

from __future__ import annotations

from api.model_context import (
    _clean_session_model_provider,
    _split_provider_qualified_model,
)
from api.sessions.model_compatibility import (
    _resolve_compatible_session_model_state,
)
from api.sessions.profile_model_config import _read_profile_model_config


def _resolve_compatible_session_model(model_id: str | None) -> tuple[str, bool]:
    """Return the effective model and whether legacy state was normalized."""
    effective_model, _provider, changed = _resolve_compatible_session_model_state(
        model_id
    )
    return effective_model, changed


def _normalize_session_model_in_place(session) -> str:
    """Normalize persisted model/provider state, saving only real repairs."""
    original_model = getattr(session, "model", None) or ""
    original_provider = _clean_session_model_provider(
        getattr(session, "model_provider", None)
    )
    effective_model, effective_provider, changed = (
        _resolve_compatible_session_model_state(
            original_model or None,
            original_provider,
        )
    )
    provider_changed = effective_provider != original_provider
    if (
        original_model
        and effective_model
        and ((changed and original_model != effective_model) or provider_changed)
    ):
        if changed and original_model != effective_model:
            session.model = effective_model
        session.model_provider = effective_provider
        session.save(touch_updated_at=False)
    return effective_model


def _resolve_effective_session_model_for_display(session) -> str:
    """Resolve display model without mutating persisted session state."""
    original_model = getattr(session, "model", None) or ""
    requested_provider = getattr(session, "model_provider", None)
    profile_provider, profile_default, profile_config = _read_profile_model_config(
        session, requested_provider
    )
    effective_model, _provider, _changed = _resolve_compatible_session_model_state(
        original_model or None,
        requested_provider,
        profile_provider=profile_provider,
        profile_default_model=profile_default,
        profile_config=profile_config,
        prefer_cached_catalog=True,
    )
    return effective_model or original_model


def _resolve_effective_session_model_provider_for_display(session) -> str | None:
    """Resolve display provider without mutating persisted session state."""
    original_model = getattr(session, "model", None) or ""
    requested_provider = getattr(session, "model_provider", None)
    profile_provider, profile_default, profile_config = _read_profile_model_config(
        session, requested_provider
    )
    _model, provider, _changed = _resolve_compatible_session_model_state(
        original_model or None,
        requested_provider,
        profile_provider=profile_provider,
        profile_default_model=profile_default,
        profile_config=profile_config,
        prefer_cached_catalog=True,
    )
    return provider


def _session_model_state_from_request(
    model: str | None,
    requested_provider: str | None,
    current_provider: str | None = None,
) -> tuple[str | None, str | None]:
    """Normalize the authoritative model/provider pair from a request."""
    model_value = str(model).strip() if model is not None else None
    provider = (
        _clean_session_model_provider(requested_provider)
        if requested_provider is not None
        else None
    )
    if model_value:
        _bare, explicit_provider = _split_provider_qualified_model(model_value)
        if explicit_provider:
            provider = explicit_provider
        elif requested_provider is None:
            provider = _clean_session_model_provider(current_provider)
        model_value, provider, _changed = _resolve_compatible_session_model_state(
            model_value,
            provider,
        )
    return model_value, provider
