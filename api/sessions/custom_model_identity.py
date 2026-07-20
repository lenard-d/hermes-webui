"""Custom-provider model identity repair from configured catalog entries."""

from __future__ import annotations

from api.model_context import _clean_session_model_provider


def _ordered_custom_provider_model_ids(entry: dict) -> list[str]:
    """Return configured model ids in deterministic declaration order."""
    ordered: list[str] = []
    default_model = str(entry.get("model") or "").strip()
    if default_model:
        ordered.append(default_model)
    models = entry.get("models")
    if isinstance(models, dict):
        for key in models:
            if isinstance(key, str):
                model_id = key.strip()
                if model_id and model_id not in ordered:
                    ordered.append(model_id)
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, str):
                model_id = item.strip()
            elif isinstance(item, dict):
                model_id = str(
                    item.get("id") or item.get("model") or item.get("name") or ""
                ).strip()
            else:
                continue
            if model_id and model_id not in ordered:
                ordered.append(model_id)
    return ordered


def _repair_bare_custom_provider_model(
    bare_model: str,
    provider: str | None,
    *,
    config_obj: dict | None = None,
) -> str | None:
    """Re-qualify a bare model using its named custom provider config."""
    try:
        from api.config import (
            custom_provider_entries,
            custom_provider_slug_from_name,
            get_config,
        )

        model = str(bare_model or "").strip()
        provider_id = _clean_session_model_provider(provider)
        if not model or "/" in model or not provider_id:
            return None
        if provider_id != "custom" and not str(provider_id).startswith("custom:"):
            return None

        if isinstance(config_obj, dict):
            entries = custom_provider_entries(config_obj)
        else:
            config = get_config()
            entries = custom_provider_entries(
                config if isinstance(config, dict) else None
            )
        provider_normalized = str(provider_id).strip().lower()
        raw_suffix = provider_normalized.removeprefix("custom:")
        matching_provider = None
        for entry in entries:
            entry_name = str(entry.get("name") or "").strip().lower()
            slug = custom_provider_slug_from_name(entry.get("name"))
            if not slug:
                continue
            if provider_normalized in {
                entry_name,
                slug,
            } or raw_suffix == slug.removeprefix("custom:"):
                matching_provider = entry
                break
        if not matching_provider:
            return None
        for model_id in _ordered_custom_provider_model_ids(matching_provider):
            if "/" in model_id and model_id.rsplit("/", 1)[-1] == model:
                return model_id
        return None
    except Exception:
        return None
