"""Credential, CLI, and local-file sources for model catalog construction."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from api import config as _config_module
from api.config.catalog_state import MODEL_CATALOG_STATE

logger = logging.getLogger(__name__)

def _credential_pool_profile_tag() -> str:
    """Active-profile identity for the credential-pool cache key.

    The credential pool is per-Hermes-profile (it lives in that profile's
    auth.json). Keying the process-global cache by provider id ALONE lets a
    pool loaded under profile A satisfy a lookup under profile B in the same
    server process — so a custom provider configured only in A would falsely
    report configured in B (and then 401 at request time). Scoping every
    cache key by the active profile's auth-store path keeps pools from
    crossing profile boundaries.
    """
    try:
        return str(_config_module._get_auth_store_path())
    except Exception:
        return ""


def _pool_entry_payloads(provider_id: str) -> list[dict[str, Any]]:
    """Return explicit credential-pool entry payloads for the active profile.

    Readonly profile scopes must not let ``load_pool()`` seed from process env,
    because that can materialize server-default credentials into a named
    profile's auth store. In that mode, read raw auth.json payloads only.
    """
    _pid = _config_module._resolve_provider_alias(provider_id)
    if bool(getattr(_config_module._thread_ctx, "block_process_env_fallback", False)):
        try:
            from hermes_cli.auth import read_credential_pool as _read_credential_pool

            raw_entries = _read_credential_pool(_pid)
        except ImportError:
            return []
        payloads: list[dict[str, Any]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            if _config_module._is_ambient_gh_cli_entry(
                str(entry.get("source", "") or ""),
                str(entry.get("label", "") or ""),
                str(entry.get("key_source", "") or ""),
            ):
                continue
            payloads.append(dict(entry))
        return payloads

    try:
        from agent.credential_pool import load_pool as _load_pool

        _ck = (_credential_pool_profile_tag(), _pid)
        _cached = MODEL_CATALOG_STATE.credential_pool_cache.get(_ck)
        if _cached is not None:
            _cp_ts, _cp_pool = _cached
            if (time.time() - _cp_ts) < 86400.0:
                _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
            else:
                _cp_pool = _load_pool(_pid)
                MODEL_CATALOG_STATE.credential_pool_cache[_ck] = (time.time(), _cp_pool)
                _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
        else:
            _cp_pool = _load_pool(_pid)
            MODEL_CATALOG_STATE.credential_pool_cache[_ck] = (time.time(), _cp_pool)
            _all_entries = _cp_pool.entries() if _cp_pool is not None and hasattr(_cp_pool, "entries") else []
    except ImportError:
        return []

    payloads = []
    for entry in _all_entries:
        if _config_module._is_ambient_gh_cli_entry(
            str(getattr(entry, "source", "") or ""),
            str(getattr(entry, "label", "") or ""),
            str(getattr(entry, "key_source", "") or ""),
        ):
            continue
        if hasattr(entry, "to_dict") and callable(entry.to_dict):
            payload = entry.to_dict()
        elif isinstance(entry, dict):
            payload = dict(entry)
        else:
            try:
                payload = dict(vars(entry))
            except TypeError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)
        payload.setdefault("source", str(getattr(entry, "source", "") or ""))
        payload.setdefault("label", str(getattr(entry, "label", "") or ""))
        payload.setdefault("key_source", str(getattr(entry, "key_source", "") or ""))
        runtime_api_key = getattr(entry, "runtime_api_key", None)
        if runtime_api_key:
            payload["runtime_api_key"] = runtime_api_key
        base_url = getattr(entry, "base_url", None)
        if base_url:
            payload["base_url"] = base_url
        inference_base_url = getattr(entry, "inference_base_url", None)
        if inference_base_url:
            payload["inference_base_url"] = inference_base_url
        payloads.append(payload)
    return payloads



def _has_explicit_pool_credentials(provider_id: str) -> bool:
    """Return True when the credential pool has at least one non-ambient entry
    for *provider_id* (i.e. not a gh-cli / GITHUB_TOKEN auto-detect).

    Reuses ``MODEL_CATALOG_STATE.credential_pool_cache`` so that callers on hot paths (provider
    detection, model listing, live-model fetch) don't pay the ~10s load_pool
    cost more than once per TTL window.
    """
    return bool(_pool_entry_payloads(provider_id))


def _current_webui_version() -> str | None:
    """Return the version supplied by the downstream update layer."""
    from api.config.hooks import get_config_runtime_hooks

    value = get_config_runtime_hooks().webui_version()
    return str(value) if value else None


def _get_label_for_model(model_id: str, existing_groups: list) -> str:
    """Return a human-friendly label for *model_id*.

    Resolution order:
    1. If the model already appears in *existing_groups* with a label, use it.
    2. Strip @provider: prefix and namespace prefix, then title-case.

    This ensures the injected default model entry in the dropdown always shows
    the same label as the live-fetched or static-catalog version, rather than
    the raw lowercase ID string (#909).
    """
    # Strip @provider: prefix for lookup
    lookup_id = model_id
    if lookup_id.startswith("@") and ":" in lookup_id:
        lookup_id = lookup_id.split(":", 1)[1]

    # Check existing groups for a matching label.
    # Skip slash stripping for URI-scheme IDs (e.g. gpt://folder/model) (#3429).
    _has_scheme = lambda s: "://" in s
    _norm = lambda s: (s.split("/", 1)[-1] if ("/" in s and not _has_scheme(s)) else s).replace("-", ".").lower()
    norm_lookup = _norm(lookup_id)
    for g in existing_groups:
        for m in g.get("models", []):
            if m.get("label") and _norm(str(m.get("id", ""))) == norm_lookup:
                return m["label"]

    # Fall back: strip only the first slash-segment (provider prefix),
    # preserving vendor hierarchy for multi-slash IDs (#3360).
    # Skip for URI-scheme IDs whose slashes are path separators (#3429).
    bare = lookup_id.split("/", 1)[1] if ("/" in lookup_id and not _has_scheme(lookup_id)) else lookup_id
    return " ".join(
        w.upper() if (len(w) <= 3 and w.replace(".", "").isalnum() and not w.isdigit()) else w.capitalize()
        for w in bare.replace("_", "-").split("-")
    )



def _read_live_provider_model_ids(provider_id: str) -> list[str]:
    """Return live model IDs from Hermes CLI for a provider, or [] on failure.

    WebUI's static ``_config_module._PROVIDER_MODELS`` table is only a fallback.  The agent CLI
    owns the provider registry and catalog-discovery logic, so ordinary picker
    groups should ask ``hermes_cli.models.provider_model_ids()`` first (#1240).
    Provider aliases are tried as a secondary lookup because WebUI keeps a few
    display-facing IDs (for example ``google`` / ``x-ai``) that Hermes CLI may
    normalize internally.
    """
    pid = str(provider_id or "").strip()
    if not pid:
        return []
    try:
        from hermes_cli.models import provider_model_ids as _provider_model_ids
    except Exception:
        return []

    candidates = [pid]
    try:
        alias = _config_module._resolve_provider_alias(pid)
    except Exception:
        alias = ""
    if alias and alias not in candidates:
        candidates.append(alias)

    seen: set[str] = set()
    for candidate in candidates:
        try:
            live_ids = _provider_model_ids(candidate) or []
        except Exception:
            logger.debug("Failed to load %s models from hermes_cli", candidate)
            continue
        result: list[str] = []
        for mid in live_ids:
            mid_s = str(mid or "").strip()
            if mid_s and mid_s not in seen:
                seen.add(mid_s)
                result.append(mid_s)
        if result:
            return result
    return []



def _models_from_live_provider_ids(provider_id: str, live_ids: list[str]) -> list[dict]:
    """Convert Hermes CLI model ids into WebUI picker model entries."""
    formatter = _config_module._format_ollama_label if provider_id in ("ollama", "ollama-cloud") else None
    models: list[dict] = []
    seen: set[str] = set()
    for mid in live_ids:
        mid_s = str(mid or "").strip()
        if not mid_s or mid_s in seen:
            continue
        seen.add(mid_s)
        label = formatter(mid_s) if formatter else _get_label_for_model(mid_s, [])
        models.append({"id": mid_s, "label": label})
    return models



def _moa_preset_models_from_config(config_obj: dict | None = None) -> list[dict]:
    """Return enabled MoA presets from local config as picker model entries."""
    source = config_obj if isinstance(config_obj, dict) else _config_module.cfg
    moa_cfg = source.get("moa") if isinstance(source, dict) else None
    if not isinstance(moa_cfg, dict) or not bool(moa_cfg.get("enabled", True)):
        return []
    presets = moa_cfg.get("presets")
    if not isinstance(presets, dict):
        return []
    models: list[dict] = []
    seen: set[str] = set()
    for name, preset_cfg in presets.items():
        preset_name = str(name or "").strip()
        if not preset_name or preset_name in seen:
            continue
        if isinstance(preset_cfg, dict) and preset_cfg.get("enabled") is False:
            continue
        seen.add(preset_name)
        models.append({"id": preset_name, "label": preset_name})
    return models



def _read_visible_codex_cache_model_ids() -> list[str]:
    """Return visible model slugs from Codex's local models_cache.json.

    The agent's provider_model_ids('openai-codex') intentionally filters IDs
    with ``supported_in_api: false``. Codex CLI still lists some of those models
    in its picker (notably ``gpt-5.3-codex-spark`` from #1680), so the WebUI
    merges this visible local catalog to stay in sync with Codex itself.
    """
    codex_home = Path(os.getenv("CODEX_HOME", "").strip() or (_config_module.HOME / ".codex")).expanduser()
    cache_path = codex_home / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    sortable: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in ("hide", "hidden"):
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug.strip()))

    sortable.sort(key=lambda item: (item[0], item[1]))
    ordered: list[str] = []
    for _, slug in sortable:
        if slug not in ordered:
            ordered.append(slug)
    return ordered

__all__ = (
    "_credential_pool_profile_tag",
    "_pool_entry_payloads",
    "_has_explicit_pool_credentials",
    "_current_webui_version",
    "_get_label_for_model",
    "_read_live_provider_model_ids",
    "_models_from_live_provider_ids",
    "_moa_preset_models_from_config",
    "_read_visible_codex_cache_model_ids",
)
