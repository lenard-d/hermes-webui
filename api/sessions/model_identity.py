"""Provider and catalog identity rules for persisted session models."""

from __future__ import annotations

from api.config import get_available_models
from api.model_context import (
    _clean_session_model_provider,
    _split_provider_qualified_model,
)


_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "gpt": "openai",
    "gemini": "google",
    "openai-codex": "openai",
    "openai-api": "openai",
    "google-gemini": "google",
    "google-ai-studio": "google",
    "claude-code": "anthropic",
}


def _starts_token(raw: str, prefix: str) -> bool:
    if not raw.startswith(prefix):
        return False
    rest = raw[len(prefix) :]
    return rest == "" or rest[0] in ":/"


def _normalize_provider_id(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[raw]
    for prefix, normalized in (
        ("openai-codex", "openai"),
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("claude", "anthropic"),
        ("google", "google"),
        ("gemini", "google"),
        ("openrouter", "openrouter"),
        ("custom", "custom"),
    ):
        if _starts_token(raw, prefix):
            return normalized
    return ""


def _catalog_provider_id_sets(catalog: dict) -> tuple[set[str], set[str]]:
    raw_provider_ids: set[str] = set()
    normalized_provider_ids: set[str] = set()
    for group in catalog.get("groups") or []:
        raw = str(group.get("provider_id") or "").strip().lower()
        if not raw:
            continue
        raw_provider_ids.add(raw)
        normalized = _normalize_provider_id(raw)
        if normalized:
            normalized_provider_ids.add(normalized)
    return raw_provider_ids, normalized_provider_ids


def _catalog_has_provider(
    provider_raw: str,
    provider_normalized: str,
    raw_provider_ids: set[str],
    normalized_provider_ids: set[str],
) -> bool:
    return (
        provider_raw in raw_provider_ids
        or (provider_normalized and provider_normalized in raw_provider_ids)
        or (provider_normalized and provider_normalized in normalized_provider_ids)
    )


def _model_matches_active_provider_family(model: str, active_provider: str) -> bool:
    model_lower = model.lower()
    for bare_prefix in ("gpt", "claude", "gemini"):
        if model_lower.startswith(bare_prefix):
            return _normalize_provider_id(bare_prefix) == active_provider
    return False


def _catalog_model_id_matches(candidate: str, model: str) -> bool:
    candidate = str(candidate or "").strip()
    if candidate.startswith("@") and ":" in candidate:
        candidate = candidate.rsplit(":", 1)[1]
    if "/" in candidate:
        candidate = candidate.split("/", 1)[1]
    return candidate.replace("-", ".").lower() == model.replace("-", ".").lower()


def _catalog_group_owns_exact_model(group: dict, model: str) -> bool:
    provider_id = str(group.get("provider_id") or "").strip()
    wrapper = f"@{provider_id}:"
    for bucket in ("models", "extra_models"):
        for entry in group.get(bucket) or []:
            if not isinstance(entry, dict):
                continue
            candidate = str(entry.get("id") or "").strip()
            if candidate.lower().startswith(wrapper.lower()):
                candidate = candidate[len(wrapper) :]
            if candidate == model or _catalog_model_id_matches(candidate, model):
                return True
    return False


def _repair_foreign_session_model_provider(
    session,
    *,
    requested_model: str,
    requested_provider: str | None,
    resolved_model: str,
    resolved_provider: str | None,
    explicit_model_pick: bool,
    profile_provider: str | None,
) -> str | None:
    """Repair a stale provider only when the cached catalog names one owner."""
    stored_model = str(getattr(session, "model", "") or "").strip()
    stored_provider = _clean_session_model_provider(
        getattr(session, "model_provider", None)
    )
    requested_provider = _clean_session_model_provider(requested_provider)
    resolved_provider = _clean_session_model_provider(resolved_provider)
    profile_provider = _clean_session_model_provider(profile_provider)
    _, qualified_provider = _split_provider_qualified_model(requested_model)
    if (
        explicit_model_pick
        or qualified_provider
        or not stored_model
        or not stored_provider
        or (
            str(requested_model or "").strip() != stored_model
            and not _catalog_model_id_matches(
                str(requested_model or "").strip(), stored_model
            )
        )
        or requested_provider != stored_provider
        or (
            resolved_model != stored_model
            and not _catalog_model_id_matches(resolved_model, stored_model)
        )
        or resolved_provider != stored_provider
        or not profile_provider
        or profile_provider == stored_provider
    ):
        return resolved_provider

    try:
        catalog = get_available_models(prefer_cache=True)
    except Exception:
        return resolved_provider
    groups = [group for group in catalog.get("groups") or [] if isinstance(group, dict)]
    stored_groups = [
        group
        for group in groups
        if str(group.get("provider_id") or "").strip().lower() == stored_provider
    ]
    if (
        not stored_groups
        or any(group.get("models_endpoint_error") for group in stored_groups)
        or any(
            _catalog_group_owns_exact_model(group, stored_model)
            for group in stored_groups
        )
    ):
        return resolved_provider
    owners = [
        group
        for group in groups
        if str(group.get("provider_id") or "").strip().lower() != stored_provider
        and _catalog_group_owns_exact_model(group, stored_model)
    ]
    if len(owners) != 1:
        return resolved_provider
    return str(owners[0].get("provider_id") or "").strip() or resolved_provider


def _should_attach_codex_provider_context(
    model: str, raw_active_provider: str, catalog: dict
) -> bool:
    """Return whether a bare Codex model needs separate provider context."""
    if raw_active_provider != "openai-codex" or not model.lower().startswith("gpt"):
        return False
    for group in catalog.get("groups") or []:
        if str(group.get("provider_id") or "").strip().lower() != "openai-codex":
            continue
        return any(
            _catalog_model_id_matches(entry.get("id"), model)
            for entry in group.get("models", [])
            if isinstance(entry, dict)
        )
    return False
