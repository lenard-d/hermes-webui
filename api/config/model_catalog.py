"""Provider model-catalog construction and cache coordination.

The mutable catalog, generation, provenance, and credential-pool state is owned
by :mod:`api.config.catalog_state` and shared with the disk-cache implementation.
"""

# ruff: noqa: F821

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from api import config as _config_module
from api.config.catalog_state import (
    MODEL_CATALOG_STATE,
    import_legacy_model_catalog_state,
    publish_legacy_model_catalog_state,
)


logger = logging.getLogger(__name__)


def _invalidate_models_build_locked() -> None:
    """Revoke publication rights from any detached catalog rebuild."""
    MODEL_CATALOG_STATE.models_cache_build_generation += 1
    MODEL_CATALOG_STATE.active_models_cache_build_generation = None
    MODEL_CATALOG_STATE.cache_build_in_progress = False
    MODEL_CATALOG_STATE.cache_build_cv.notify_all()
    publish_legacy_model_catalog_state(_config_module)


def _sync_models_cache_provenance() -> None:
    """Republish the atomic (snapshot, fingerprint) provenance pair.

    MUST be called at every site that assigns ``MODEL_CATALOG_STATE.available_models_cache`` and
    ``MODEL_CATALOG_STATE.available_models_cache_source_fingerprint`` (publish and invalidate),
    AFTER both have been set. It snapshots the current pair into one immutable
    tuple so ``_endpoint_advertised_model_ids`` reads both consistently with a
    single lock-free load. A reader that races between an underlying assignment
    and this call sees the PREVIOUS consistent tuple (never a torn pair); once
    this runs, readers see the new consistent pair.
    """
    snap = MODEL_CATALOG_STATE.available_models_cache
    MODEL_CATALOG_STATE.models_cache_provenance = (
        (snap, MODEL_CATALOG_STATE.available_models_cache_source_fingerprint) if snap is not None else None
    )
    publish_legacy_model_catalog_state(_config_module)


def _endpoint_advertised_model_ids(provider_id: str | None) -> frozenset | None:
    """Model ids the given provider's group advertised in the current catalog.

    Reads ONLY the already-published in-memory catalog snapshot
    (``MODEL_CATALOG_STATE.available_models_cache``) — it never builds, live-probes, or touches
    disk, so it is safe to call on the per-turn send hot path. Returns:

      * a ``frozenset`` of the ids advertised by ``provider_id``'s own group
        (bare ids for the active provider, e.g. ``x-ai/grok-4.5``), or
      * ``None`` when the catalog is cold/unbuilt OR the provider has no group.

    ``None`` means "no provenance signal available" — callers MUST treat that as
    "preserve the model id verbatim" so a cache miss never silently strips a
    vendor namespace off an id the user actively selected (#5979). Scoping to
    the provider's OWN group prevents a same-named id in a sibling group (e.g.
    an ``openai/gpt-5.4`` sitting in the OpenRouter group) from masquerading as
    something this custom endpoint advertised.
    """
    # Single lock-free atomic read of the immutable (snapshot, fingerprint) pair
    # published by _sync_models_cache_provenance(). Reading one tuple can never
    # tear, and acquiring no lock means this per-send check adds no lock-ordering
    # edge (no _cfg_lock ↔ MODEL_CATALOG_STATE.available_models_cache_lock deadlock) and never waits
    # behind a catalog rebuild.
    provenance = MODEL_CATALOG_STATE.models_cache_provenance
    if provenance is None:
        return None
    snapshot, published_fp = provenance
    if snapshot is None:
        return None
    # Profile-isolation fail-safe (profiles are islands): the catalog cache is a
    # process global, so a concurrently-active profile could have published the
    # snapshot we're now reading. Only trust it for provenance when the
    # fingerprint captured AT PUBLISH TIME still matches the current runtime
    # fingerprint — the ``config_yaml`` axis of that fingerprint is the
    # PROFILE-SPECIFIC config path (_config_module._get_config_path -> get_active_hermes_home),
    # so a match guarantees the snapshot belongs to the profile asking. Any
    # mismatch (foreign profile, config edit, stale) returns None so the caller
    # preserves the id verbatim rather than stripping against another profile's
    # catalog.
    try:
        if published_fp != _config_module._models_cache_source_fingerprint():
            return None
    except Exception:
        return None  # fingerprint unavailable → no trustworthy provenance
    memo = MODEL_CATALOG_STATE.advertised_model_ids_memo
    # Identity check (``is``), not id(): holding the snapshot reference in the
    # memo keeps it alive, so a freed-then-reused id() can't cause a false hit.
    if memo is None or memo[0] is not snapshot:
        by_slug: dict[str, frozenset] = {}
        try:
            groups = snapshot.get("groups", []) or []
        except AttributeError:
            return None
        for group in groups:
            if not isinstance(group, dict):
                continue
            slug = str(group.get("provider_id") or "").strip().lower()
            if not slug:
                continue
            # Union BOTH catalog buckets: a provider's models can be split across
            # ``models`` (visible) and ``extra_models`` (overflow) by the picker,
            # so an id the endpoint genuinely advertised may live in either. Only
            # reading ``models`` would miss it and mis-resolve (e.g. leave the
            # #433 bare id unstripped because it sits in extra_models).
            ids = frozenset(
                str(m.get("id"))
                for bucket in ("models", "extra_models")
                for m in (group.get(bucket) or [])
                if isinstance(m, dict) and m.get("id")
            )
            by_slug[slug] = by_slug.get(slug, frozenset()) | ids
        memo = (snapshot, by_slug)
        MODEL_CATALOG_STATE.advertised_model_ids_memo = memo
    slug = str(provider_id or "").strip().lower()
    return memo[1].get(slug)


def _should_warn_budget(reason: str, cooldown_s: float | None = None) -> bool:
    """Return True iff the budget warning for ``reason`` should log at
    warning level (first hit, or last warn-level emit was more than
    ``cooldown_s`` seconds ago). Otherwise False — the caller should demote
    to info for the same payload so the signal is retained but the noise is
    capped. Thread-safe; the cooldown is shared across all live-rebuild
    callers in this process.
    """
    cooldown = (
        MODEL_CATALOG_STATE.budget_warn_cooldown_seconds if cooldown_s is None else float(cooldown_s)
    )
    now = time.monotonic()
    with MODEL_CATALOG_STATE.budget_warn_lock:
        last = MODEL_CATALOG_STATE.budget_warn_state.get(reason)
        if last is None or (now - last) >= cooldown:
            MODEL_CATALOG_STATE.budget_warn_state[reason] = now
            return True
        return False


def _invoke_models_rebuild(builder):
    """Indirection seam around the cold catalog rebuild.

    Production simply calls ``builder()``. Exists so tests can simulate a
    slow / hanging provider probe without having to reach the closure that
    actually does the per-provider network calls.
    """
    return builder()


def _configured_model_badges_from_static_catalog(
    groups: list[dict],
    *,
    active_provider: str | None,
    default_model: str,
) -> dict[str, dict[str, str]]:
    configured_entries: list[dict[str, str]] = []
    if active_provider and default_model:
        configured_entries.append(
            {
                "provider": active_provider,
                "model": default_model,
                "role": "primary",
                "label": "Primary",
            }
        )

    fallback_cfg = _config_module.cfg.get("fallback_providers", []) if isinstance(_config_module.cfg, dict) else []
    if isinstance(fallback_cfg, list):
        for idx, entry in enumerate(fallback_cfg, start=1):
            if not isinstance(entry, dict):
                continue
            provider = _config_module._resolve_provider_alias(entry.get("provider"))
            model = str(entry.get("model") or "").strip()
            if not provider or not model:
                continue
            configured_entries.append(
                {
                    "provider": provider,
                    "model": model,
                    "role": "fallback",
                    "label": f"Fallback {idx}",
                }
            )

    option_ids = [
        m.get("id", "")
        for g in groups
        for m in g.get("models", [])
        if m.get("id")
    ]
    option_lookup = {str(opt_id): str(opt_id) for opt_id in option_ids}
    option_provider_lookup = {
        str(m.get("id")): str(g.get("provider_id") or "")
        for g in groups
        for m in g.get("models", [])
        if m.get("id")
    }

    def _norm_static_model_id(model_id: str) -> str:
        s = str(model_id or "").strip().lower()
        stripped_at_provider = False
        if s.startswith("@") and ":" in s:
            colon_idx = s.index(":", 1)
            candidate = s[colon_idx + 1:]
            stripped_at_provider = bool(candidate)
            s = candidate or s
        if "://" not in s:
            if (
                not stripped_at_provider
                and "/" in s
                and ":" in s
                and s.index(":") < s.index("/")
            ):
                s = s[s.index("/") + 1 :] or s
            if "/" in s:
                stripped = s.split("/", 1)[1]
                s = stripped or s
        return s.replace("-", ".")

    norm_lookup: dict[str, list[str]] = {}
    for opt_id in option_ids:
        norm_lookup.setdefault(_norm_static_model_id(opt_id), []).append(opt_id)

    badges: dict[str, dict[str, str]] = {}
    for entry in configured_entries:
        provider = entry["provider"]
        model = entry["model"]
        raw_candidates = []
        for candidate in (model, f"{provider}/{model}", f"@{provider}:{model}"):
            if candidate and candidate not in raw_candidates:
                raw_candidates.append(candidate)

        match_id = None
        for candidate in raw_candidates:
            if (
                candidate in option_lookup
                and option_provider_lookup.get(candidate) == provider
            ):
                match_id = option_lookup[candidate]
                break
        if match_id is None:
            for candidate in raw_candidates:
                normalized = _norm_static_model_id(candidate)
                matches = norm_lookup.get(normalized, [])
                if not matches:
                    continue
                provider_match = next(
                    (m for m in matches if option_provider_lookup.get(m) == provider),
                    None,
                )
                match_id = provider_match or matches[0]
                if match_id:
                    break

        badge_payload = {
            "role": entry["role"],
            "label": entry["label"],
            "provider": provider,
        }
        for candidate in raw_candidates:
            candidate_provider = option_provider_lookup.get(candidate)
            if candidate_provider and candidate_provider != provider:
                continue
            badges[candidate] = badge_payload
        if match_id:
            badges[match_id] = badge_payload

    return badges



def _minimal_static_models_catalog() -> dict:
    """Return the emergency one-model fallback for /api/models."""
    try:
        active_provider = None
        cfg_base_url = ""
        model_cfg = _config_module.cfg.get("model", {}) if isinstance(_config_module.cfg, dict) else {}
        if isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_base_url = model_cfg.get("base_url", "") or ""
        if active_provider:
            try:
                active_provider = _config_module._resolve_configured_provider_id(
                    active_provider, _config_module.cfg, base_url=cfg_base_url
                )
            except Exception:
                active_provider = str(active_provider or "").strip() or None
        if not active_provider:
            try:
                _ap = _config_module._get_auth_store_path()
                if _ap.exists():
                    _store = json.loads(_ap.read_text(encoding="utf-8"))
                    active_provider = (
                        _config_module._resolve_configured_provider_id(
                            _store.get("active_provider"), _config_module.cfg, base_url=cfg_base_url
                        )
                        or None
                    )
            except Exception:
                pass
        default_model = _config_module.get_effective_default_model(_config_module.cfg)
        groups: list[dict] = []
        if default_model:
            try:
                label = _get_label_for_model(default_model, [])
            except Exception:
                label = default_model
            groups.append(
                {
                    "provider": "Default",
                    "provider_id": active_provider or "default",
                    "models": [{"id": default_model, "label": label}],
                }
            )
        return _config_module._annotate_fast_tier_model_groups({
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": {},
            "groups": groups,
            "aliases": {},
        })
    except Exception:
        logger.debug("minimal static models catalog build failed", exc_info=True)
        return {
            "active_provider": None,
            "default_model": "",
            "configured_model_badges": {},
            "groups": [],
            "aliases": {},
        }



def _static_models_catalog_without_live_probes() -> dict:
    """Return a network-free /api/models catalog from local config/auth only."""
    try:
        from api.config.hooks import get_config_runtime_hooks

        _provider_has_key = get_config_runtime_hooks().provider_has_credential

        active_provider = None
        cfg_base_url = ""
        model_cfg = _config_module.cfg.get("model", {}) if isinstance(_config_module.cfg, dict) else {}
        if isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_base_url = model_cfg.get("base_url", "") or ""
        if active_provider:
            try:
                active_provider = _config_module._resolve_configured_provider_id(
                    active_provider,
                    _config_module.cfg,
                    base_url=cfg_base_url,
                )
            except Exception:
                active_provider = str(active_provider or "").strip() or None

        auth_store: dict = {}
        try:
            auth_store_path = _config_module._get_auth_store_path()
            if auth_store_path.exists():
                auth_store = json.loads(auth_store_path.read_text(encoding="utf-8"))
                if not active_provider:
                    active_provider = (
                        _config_module._resolve_configured_provider_id(
                            auth_store.get("active_provider"),
                            _config_module.cfg,
                            base_url=cfg_base_url,
                        )
                        or None
                    )
        except Exception:
            logger.debug("Failed to load auth store for static models catalog", exc_info=True)

        default_model = _config_module.get_effective_default_model(_config_module.cfg)
        detected_providers: set[str] = set()
        configured_model_ids: dict[str, list[str]] = {}
        named_custom_groups: dict[str, dict[str, object]] = {}
        custom_group_models: list[dict] = []
        canonical_to_raw_provider_key: dict[str, str] = {}
        providers_cfg = _config_module._get_providers_cfg()

        def _append_model_id(provider_id: str | None, model_id: object) -> None:
            pid = _config_module._canonicalise_provider_id(provider_id)
            mid = str(model_id or "").strip()
            if not pid or not mid:
                return
            configured_model_ids.setdefault(pid, [])
            if mid not in configured_model_ids[pid]:
                configured_model_ids[pid].append(mid)

        if active_provider:
            detected_providers.add(active_provider)
            _append_model_id(active_provider, default_model)

        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid, _entries in _pool.items():
                    if not isinstance(_entries, list) or not _entries:
                        continue
                    if any(
                        isinstance(_entry, dict)
                        and not _config_module._is_ambient_gh_cli_entry(
                            str(_entry.get("source", "") or ""),
                            str(_entry.get("label", "") or ""),
                            str(_entry.get("key_source", "") or ""),
                        )
                        for _entry in _entries
                    ):
                        detected_providers.add(_config_module._resolve_provider_alias(str(_pid)))
        except Exception:
            logger.debug("Failed to inspect auth-store credential pool", exc_info=True)

        if isinstance(providers_cfg, dict):
            for provider_key, provider_cfg in providers_cfg.items():
                canonical = _config_module._canonicalise_provider_id(provider_key)
                if not canonical:
                    continue
                is_known_provider = (
                    canonical in _config_module._PROVIDER_MODELS
                    or canonical in _config_module._PROVIDER_DISPLAY
                    or _config_module._is_plugin_model_provider(canonical)
                )
                is_provider_config = isinstance(provider_cfg, dict)
                if not (is_known_provider or is_provider_config):
                    continue
                canonical_to_raw_provider_key.setdefault(canonical, provider_key)
                if isinstance(provider_cfg, dict):
                    has_local_signal = any(
                        str(provider_cfg.get(key) or "").strip()
                        for key in ("api_key", "key_env", "base_url")
                    )
                    provider_models = provider_cfg.get("models")
                    for model_id in _config_module._configured_model_ids(provider_models):
                        _append_model_id(canonical, model_id)
                        has_local_signal = True
                    if has_local_signal:
                        detected_providers.add(canonical)

        for provider_id in set(_config_module._PROVIDER_MODELS) | set(_config_module._PROVIDER_DISPLAY):
            canonical = _config_module._canonicalise_provider_id(provider_id)
            if canonical and _provider_has_key(canonical):
                detected_providers.add(canonical)

        # Plugin-only providers (e.g. 9router) are not in the static
        # _config_module._PROVIDER_MODELS / _config_module._PROVIDER_DISPLAY tables and are detected above
        # only when the user puts them in `providers.<slug>`.  Plugins ship
        # with their own env-var wiring, so an installed-and-keyed plugin
        # provider should also enter the static catalog even without a
        # `providers:` block — otherwise the picker silently drops the
        # group when the live-rebuild cache is cold.
        try:
            for _plugin_pid in list(_config_module._plugin_model_provider_profiles().keys()):
                if not _plugin_pid or not _provider_has_key(_plugin_pid):
                    continue
                _canonical = _config_module._canonicalise_provider_id(_plugin_pid) or _plugin_pid
                if _canonical:
                    detected_providers.add(_canonical)
        except Exception:
            logger.debug("Plugin provider detection failed in static catalog", exc_info=True)

        fallback_cfg = _config_module.cfg.get("fallback_providers", []) if isinstance(_config_module.cfg, dict) else []
        if isinstance(fallback_cfg, list):
            for entry in fallback_cfg:
                if not isinstance(entry, dict):
                    continue
                provider = _config_module._resolve_provider_alias(entry.get("provider"))
                if provider:
                    detected_providers.add(provider)
                    _append_model_id(provider, entry.get("model"))

        for entry in _config_module._custom_provider_entries(_config_module.cfg):
            provider_name = str(entry.get("name") or "").strip()
            provider_slug = _config_module._custom_provider_slug_from_name(provider_name) or "custom"
            if provider_slug != "custom":
                named_custom_groups.setdefault(
                    provider_slug,
                    {"name": provider_name, "models": []},
                )
            detected_providers.add(provider_slug)

            configured_ids: list[str] = []
            model_id = str(entry.get("model") or "").strip()
            if model_id:
                configured_ids.append(model_id)
            for configured_id in _config_module._configured_model_ids(entry.get("models")):
                if configured_id not in configured_ids:
                    configured_ids.append(configured_id)

            for configured_id in configured_ids:
                label = _get_label_for_model(configured_id, [])
                if provider_slug == "custom":
                    custom_group_models.append({"id": configured_id, "label": label})
                else:
                    named_custom_groups[provider_slug]["models"].append(
                        {"id": configured_id, "label": label}
                    )
                _append_model_id(provider_slug, configured_id)

        if cfg_base_url:
            detected_providers.add(
                _config_module._named_custom_provider_slug_for_base_url(cfg_base_url, _config_module.cfg)
                or active_provider
                or "custom"
            )

        if detected_providers:
            detected_providers = {
                _config_module._canonicalise_provider_id(provider_id) or provider_id
                for provider_id in detected_providers
                if provider_id
            }

        groups: list[dict] = []
        for pid in sorted(detected_providers):
            if pid.startswith("custom:"):
                custom_group = named_custom_groups.get(pid, {})
                group_models = copy.deepcopy(custom_group.get("models", []))
                if group_models or pid == active_provider:
                    groups.append(
                        {
                            "provider": custom_group.get("name") or pid.replace("custom:", ""),
                            "provider_id": pid,
                            "models": _config_module._apply_provider_prefix(
                                group_models,
                                pid,
                                active_provider,
                            ),
                        }
                    )
                continue

            if pid == "custom":
                group_models = copy.deepcopy(custom_group_models)
                for model_id in configured_model_ids.get(pid, []):
                    if not any(m.get("id") == model_id for m in group_models):
                        group_models.append(
                            {"id": model_id, "label": _get_label_for_model(model_id, [])}
                        )
                if group_models or cfg_base_url or pid == active_provider:
                    groups.append(
                        {
                            "provider": _config_module._PROVIDER_DISPLAY.get(pid, "Custom"),
                            "provider_id": pid,
                            "models": _config_module._apply_provider_prefix(
                                group_models,
                                pid,
                                active_provider,
                            ),
                        }
                    )
                continue

            provider_name = _config_module._PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title())
            raw_key = canonical_to_raw_provider_key.get(pid, pid)
            provider_cfg = _config_module._get_provider_cfg(raw_key)
            raw_models = []
            if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                raw_models = _config_module._configured_model_options(provider_cfg["models"])
            if not raw_models:
                raw_models = copy.deepcopy(_config_module._PROVIDER_MODELS.get(pid, []))
            # Plugin-only providers (e.g. 9router) are not in _config_module._PROVIDER_MODELS
            # and rarely ship a `models:` allowlist in providers.<slug>, so
            # the static catalog above would render them as empty groups that
            # the picker filters out. Fall back to the plugin's own
            # ProviderProfile.fallback_models so the provider surfaces a
            # curated, network-free subset on the cold path. The live
            # rebuild (_build_available_models_uncached) does a full
            # /v1/models fetch and supersedes this view on the next call.
            if not raw_models and _config_module._is_plugin_model_provider(pid):
                _plugin_profile = _config_module._plugin_model_provider_profiles().get(
                    (pid or "").strip().lower()
                )
                if _plugin_profile is not None:
                    _fallback = getattr(_plugin_profile, "fallback_models", ()) or ()
                    raw_models = [{"id": str(mid), "label": str(mid)} for mid in _fallback]
            for model_id in configured_model_ids.get(pid, []):
                if model_id and not any(m.get("id") == model_id for m in raw_models):
                    raw_models.append(
                        {"id": model_id, "label": _get_label_for_model(model_id, groups)}
                    )
            # Plugin-only providers (e.g. 9router) must enter `groups` even
            # when `raw_models` is empty so the post-loop filter sees them.
            # Without this, the earlier plugin-fallback pass only seeds
            # `raw_models` when `fallback_models` is non-empty; the cold-cache
            # picker still silently drops a keyed plugin with no models yet.
            if raw_models or _config_module._is_plugin_model_provider(pid):
                groups.append(
                    {
                        "provider": provider_name,
                        "provider_id": pid,
                        "models": _config_module._apply_provider_prefix(raw_models, pid, active_provider),
                    }
                )

        if default_model:
            all_model_ids = {
                str(model.get("id") or "")
                for group in groups
                for model in group.get("models", [])
            }
            if default_model not in all_model_ids and f"@{active_provider}:{default_model}" not in all_model_ids:
                label = _get_label_for_model(default_model, groups)
                target_group = next(
                    (group for group in groups if group.get("provider_id") == active_provider),
                    None,
                )
                if target_group is not None:
                    target_group.setdefault("models", []).insert(0, {"id": default_model, "label": label})
                elif groups:
                    groups.append(
                        {
                            "provider": "Default",
                            "provider_id": active_provider or "default",
                            "models": [{"id": default_model, "label": label}],
                        }
                    )

        _config_module._deduplicate_model_ids(groups)
        groups = [
            group
            for group in groups
            if group.get("models")
            or str(group.get("provider_id") or "").startswith("custom:")
            # Keep plugin-only providers visible even when no models surfaced
            # yet (e.g. plugin's fallback_models is empty and live rebuild
            # hasn't completed). Otherwise they silently drop from the
            # picker and look "not installed" — the same 9router-empty-group
            # regression this branch was added to fix.
            or _config_module._is_plugin_model_provider(str(group.get("provider_id") or ""))
        ]

        providers_with_keys: set[str] = set()
        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid in _pool:
                    _canonical = _config_module._canonicalise_provider_id(_pid)
                    if _canonical:
                        providers_with_keys.add(_canonical)
        except Exception:
            pass
        try:
            for _pk, _pv in providers_cfg.items():
                if isinstance(_pv, dict) and (
                    _pv.get("api_key")
                    or _pv.get("key_env")
                    or _pv.get("base_url")
                ):
                    _canonical = _config_module._canonicalise_provider_id(_pk)
                    if _canonical:
                        providers_with_keys.add(_canonical)
        except Exception:
            pass

        def _group_sort_key(group: dict) -> tuple[int, str]:
            provider_id = str(group.get("provider_id") or "")
            if provider_id == active_provider:
                return (0, provider_id)
            if provider_id.startswith("custom:"):
                return (1, provider_id)
            if provider_id in providers_with_keys:
                return (2, provider_id)
            return (3, provider_id)

        groups.sort(key=_group_sort_key)

        model_aliases: dict[str, str] = {}
        try:
            raw_aliases = _config_module.cfg.get("model", {}).get("aliases", {})
            if isinstance(raw_aliases, dict):
                model_aliases = {
                    str(k).strip(): str(v).strip()
                    for k, v in raw_aliases.items()
                    if k and v
                }
        except Exception:
            pass

        if not groups and default_model:
            return copy.deepcopy(_minimal_static_models_catalog())

        return _config_module._annotate_fast_tier_model_groups({
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": _configured_model_badges_from_static_catalog(
                groups,
                active_provider=active_provider,
                default_model=default_model,
            ),
            "groups": groups,
            "aliases": model_aliases,
        })
    except Exception:
        logger.debug("static models catalog build failed", exc_info=True)
        return copy.deepcopy(_minimal_static_models_catalog())

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



def get_available_models(*, prefer_cache: bool = False, force_refresh: bool = False) -> dict:
    import_legacy_model_catalog_state(_config_module)
    """
    Return available models grouped by provider.

    Discovery order:
      1. Read config.yaml 'model' section for active provider info
      2. Check for known API keys in env or ~/.hermes/.env
      3. Fetch models from custom endpoint if base_url is configured
      4. Fall back to hardcoded model list (OpenRouter-style)

    Returns: {
        'active_provider': str|None,
        'default_model': str,
        'groups': [{'provider': str, 'models': [{'id': str, 'label': str}]}]
    }

    ``prefer_cache=True`` resolves WITHOUT ever triggering a live provider
    probe: it serves the warm in-memory cache, then the last-known on-disk
    cache, and only as a last resort a network-free minimal catalog
    (config/auth derived). It NEVER does the per-provider live rebuild (the
    Copilot token-exchange HTTPS call et al.). This is the path a
    server-initiated wakeup turn (Option Z) takes so a cold catalog can never
    block the wakeup chat/start on a flaky network. A normal human request
    leaves this False and keeps the full live-discovery behaviour.

    ``force_refresh=True`` is an internal escape hatch for bounded freshness
    checks that need a real live rebuild while preserving the default cache
    contract for every existing caller.
    """
    # Config mtime check — must come before any config reads.
    # (Test #585 verifies _current_mtime appears before active_provider = None)
    try:
        _current_path = _config_module._get_config_path()
        _current_mtime = _current_path.stat().st_mtime
    except OSError:
        _current_path = _config_module._get_config_path()
        _current_mtime = 0.0
    path_changed = _current_path != _config_module._cfg_path
    mtime_stale = _current_mtime != _config_module._cfg_mtime
    if path_changed or (mtime_stale and not _config_module._cfg_has_in_memory_overrides()):
        _config_module.reload_config_if_stale()
    # ── COLD PATH helper ─────────────────────────────────────────────────────
    # Extracted so it runs inside MODEL_CATALOG_STATE.available_models_cache_lock (RLock) to
    # prevent thundering-herd: only one thread rebuilds while others wait.
    def _build_available_models_uncached() -> dict:
        active_provider = None
        default_model = _config_module.get_effective_default_model(_config_module.cfg)
        groups = []

        def _norm_model_id(model_id: str) -> str:
            s = str(model_id or "").strip().lower()
            stripped_at_provider = False
            # Strip @provider: prefix (e.g., @custom:jingdong:GLM-5 -> GLM-5).
            # Defensive: if the last segment is empty (trailing colon, malformed
            # config), keep the original to avoid collapsing distinct IDs to ''.
            if s.startswith("@") and ":" in s:
                # Strip @provider: prefix, preserving remaining hierarchy including
                # colon-suffixed model IDs like provider/model:free (#3959).
                colon_idx = s.index(":", 1)
                candidate = s[colon_idx + 1:]
                stripped_at_provider = bool(candidate)
                s = candidate or s
            # Skip slash-based stripping for URI-scheme IDs (e.g.
            # gpt://folder/model/latest) whose slashes are path separators,
            # not provider delimiters (#3429).
            if "://" not in s:
                if (
                    not stripped_at_provider
                    and "/" in s
                    and ":" in s
                    and s.index(":") < s.index("/")
                ):
                    s = s[s.index("/") + 1 :] or s
                # Strip only the first slash-segment (provider prefix), preserving
                # any remaining vendor hierarchy.  Using parts[-1] here previously
                # discarded ALL segments except the last, collapsing distinct
                # multi-slash IDs like 'vendor_a/deepseek-v4-pro' and
                # 'vendor_b/deepseek/deepseek-v4-pro' to the same key (#3360).
                if "/" in s:
                    stripped = s.split("/", 1)[1]
                    s = stripped or s
            return s.replace("-", ".")

        def _build_configured_model_badges() -> dict[str, dict[str, str]]:
            configured_entries: list[dict[str, str]] = []
            if active_provider and default_model:
                configured_entries.append(
                    {
                        "provider": active_provider,
                        "model": default_model,
                        "role": "primary",
                        "label": "Primary",
                    }
                )
            fallback_cfg = _config_module.cfg.get("fallback_providers", [])
            if isinstance(fallback_cfg, list):
                for idx, entry in enumerate(fallback_cfg, start=1):
                    if not isinstance(entry, dict):
                        continue
                    provider = _config_module._resolve_provider_alias(entry.get("provider"))
                    model = str(entry.get("model") or "").strip()
                    if not provider or not model:
                        continue
                    configured_entries.append(
                        {
                            "provider": provider,
                            "model": model,
                            "role": "fallback",
                            "label": f"Fallback {idx}",
                        }
                    )

            option_ids = [m.get("id", "") for g in groups for m in g.get("models", []) if m.get("id")]
            option_lookup = {str(opt_id): str(opt_id) for opt_id in option_ids}
            option_provider_lookup = {
                str(m.get("id")): str(g.get("provider_id") or "")
                for g in groups
                for m in g.get("models", [])
                if m.get("id")
            }
            norm_lookup: dict[str, list[str]] = {}
            for opt_id in option_ids:
                norm_lookup.setdefault(_norm_model_id(opt_id), []).append(opt_id)

            badges: dict[str, dict[str, str]] = {}
            for entry in configured_entries:
                provider = entry["provider"]
                model = entry["model"]
                raw_candidates = []
                for candidate in (
                    model,
                    f"{provider}/{model}",
                    f"@{provider}:{model}",
                ):
                    if candidate and candidate not in raw_candidates:
                        raw_candidates.append(candidate)

                match_id = None
                exact_match = next((option_lookup[c] for c in raw_candidates if c in option_lookup), None)
                for candidate in raw_candidates:
                    if candidate in option_lookup and option_provider_lookup.get(candidate) == provider:
                        match_id = option_lookup[candidate]
                        break
                if match_id is None:
                    for candidate in raw_candidates:
                        normalized = _norm_model_id(candidate)
                        matches = norm_lookup.get(normalized, [])
                        if not matches:
                            continue
                        provider_match = next(
                            (m for m in matches if option_provider_lookup.get(m) == provider),
                            None,
                        )
                        match_id = provider_match or exact_match or matches[0]
                        if match_id:
                            break

                badge_payload = {"role": entry["role"], "label": entry["label"], "provider": provider}
                for candidate in raw_candidates:
                    candidate_provider = option_provider_lookup.get(candidate)
                    if candidate_provider and candidate_provider != provider:
                        continue
                    badges[candidate] = badge_payload
                if match_id:
                    badges[match_id] = badge_payload
            return badges

        # 1. Read config.yaml model section
        cfg_base_url = ""  # must be defined before conditional blocks (#117)
        model_cfg = _config_module.cfg.get("model", {})
        cfg_base_url = ""
        if isinstance(model_cfg, str):
            pass  # default_model already set by get_effective_default_model
        elif isinstance(model_cfg, dict):
            active_provider = model_cfg.get("provider")
            cfg_default = model_cfg.get("default", "")
            cfg_base_url = model_cfg.get("base_url", "")
            if cfg_default:
                default_model = cfg_default

        # Normalize active_provider to its canonical key.  Named custom
        # providers are first-class provider ids in WebUI routing; accept the
        # user-facing name from config.yaml (``provider: ollama-local``) and
        # route it through the same ``custom:<name>`` slug the picker emits.
        if active_provider:
            active_provider = _config_module._resolve_configured_provider_id(
                active_provider,
                _config_module.cfg,
                base_url=cfg_base_url,
            )

        # 2. Read auth store (active_provider fallback + credential_pool inspection)
        auth_store = {}
        auth_store_path = _config_module._get_auth_store_path()
        if auth_store_path.exists():
            try:
                import json as _j

                auth_store = _j.loads(auth_store_path.read_text(encoding="utf-8"))
                if not active_provider:
                    active_provider = _config_module._resolve_configured_provider_id(
                        auth_store.get("active_provider"),
                        _config_module.cfg,
                        base_url=cfg_base_url,
                    )
            except Exception:
                logger.debug("Failed to load auth store from %s", auth_store_path)

        # 3. Detect available providers.
        detected_providers = set()
        if active_provider:
            detected_providers.add(active_provider)

        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict) and _pool:
                try:
                    from agent.credential_pool import load_pool as _load_pool

                    for _pid in list(_pool.keys()):
                        try:
                            _canonical_pid = _config_module._resolve_provider_alias(str(_pid))
                            # Check credential pool cache first (profile-scoped key
                            # so a pool loaded under another profile can't leak in).
                            _ck = (_credential_pool_profile_tag(), _pid)
                            _cached = MODEL_CATALOG_STATE.credential_pool_cache.get(_ck)
                            if _cached is not None:
                                _cp_ts, _cp_pool = _cached
                                if (time.time() - _cp_ts) < 86400.0:
                                    _all_entries = _cp_pool.entries()
                                else:
                                    _lp_t0 = time.monotonic()
                                    _cp_pool = _load_pool(_pid)
                                    MODEL_CATALOG_STATE.credential_pool_cache[_ck] = (time.time(), _cp_pool)
                                    _all_entries = _cp_pool.entries()
                            else:
                                _lp_t0 = time.monotonic()
                                _cp_pool = _load_pool(_pid)
                                MODEL_CATALOG_STATE.credential_pool_cache[_ck] = (time.time(), _cp_pool)
                                _all_entries = _cp_pool.entries()
                            _explicit = [
                                e for e in _all_entries
                                if not _config_module._is_ambient_gh_cli_entry(
                                    str(getattr(e, "source", "") or ""),
                                    str(getattr(e, "label", "") or ""),
                                    str(getattr(e, "key_source", "") or ""),
                                )
                            ]
                            if _explicit and _config_module._is_known_model_provider(_canonical_pid):
                                detected_providers.add(_canonical_pid)
                        except Exception:
                            logger.debug("credential_pool.load_pool(%s) failed", _pid)
                except ImportError:
                    for _pid, _entries in _pool.items():
                        if not isinstance(_entries, list) or len(_entries) == 0:
                            continue
                        _has_explicit_cred = any(
                            isinstance(_entry, dict)
                            and not _config_module._is_ambient_gh_cli_entry(
                                str(_entry.get("source", "") or ""),
                                str(_entry.get("label", "") or ""),
                                str(_entry.get("key_source", "") or ""),
                            )
                            for _entry in _entries
                        )
                        if _has_explicit_cred:
                            _canonical_pid = _config_module._resolve_provider_alias(str(_pid))
                            if _config_module._is_known_model_provider(_canonical_pid):
                                detected_providers.add(_canonical_pid)
        except Exception:
            logger.debug("Failed to inspect credential_pool from auth store")

        all_env: dict = {}

        _hermes_auth_used = False
        try:
            from hermes_cli.models import list_available_providers as _lap
            from hermes_cli.auth import get_auth_status as _gas

            for _p in _lap():
                if not _p.get("authenticated"):
                    continue
                try:
                    _src = _gas(_p["id"]).get("key_source", "")
                    if _src == "gh auth token":
                        continue
                except Exception:
                    logger.debug("Failed to get key source for provider %s", _p.get("id", "unknown"))
                detected_providers.add(_p["id"])
            _hermes_auth_used = True

            # Belt-and-braces: list_available_providers() is the primary signal
            # for OAuth providers, but its `authenticated` field can disagree
            # with `get_auth_status(<id>).logged_in` on some hermes_cli versions
            # (the two fields are computed via different code paths). When the
            # disagreement happens for Nous Portal, the Settings → Providers
            # card renders the live catalog (because api/providers.py iterates
            # all OAuth providers regardless of authentication state) but the
            # picker dropdown comes up empty — a confusing asymmetry reported
            # in #1567. Add Nous explicitly when get_auth_status agrees so the
            # picker stays in sync with the providers card.
            try:
                if _gas("nous").get("logged_in"):
                    detected_providers.add("nous")
            except Exception:
                logger.debug("Failed to check Nous Portal auth status")
        except Exception:
            logger.debug("Failed to detect auth providers from hermes")

        if not _hermes_auth_used:
            hermes_env_path = _config_module._get_config_path().parent / ".env"
            env_keys = {}
            if hermes_env_path.exists():
                try:
                    for line in hermes_env_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env_keys[k.strip()] = v.strip().strip('"').strip("'")
                except Exception:
                    logger.debug("Failed to parse hermes env file")
            all_env = {**env_keys}
            _anthropic_env_vars = _config_module._get_anthropic_fallback_env_vars()
            for k in (
                *_anthropic_env_vars,
                "OPENAI_API_KEY",
                "OPENROUTER_API_KEY",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
                "GLM_API_KEY",
                "KIMI_API_KEY",
                "DEEPSEEK_API_KEY",
                "XIAOMI_API_KEY",
                "OPENCODE_ZEN_API_KEY",
                "OPENCODE_GO_API_KEY",
                "OPENCODE_API_KEY",
                "MINIMAX_API_KEY",
                "MINIMAX_CN_API_KEY",
                "XAI_API_KEY",
                "MISTRAL_API_KEY",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
            ):
                val = _config_module._thread_local_env_value(k).strip()
                if val:
                    all_env[k] = val
            if any(all_env.get(env_var) for env_var in _anthropic_env_vars):
                detected_providers.add("anthropic")
            if all_env.get("OPENAI_API_KEY"):
                # hermes-agent registers its OPENAI_API_KEY/OPENAI_BASE_URL provider
                # under the slug `openai-api` (there is no bare `openai` in the agent
                # registry — only `openai-api` and `openai-codex`). Detecting `openai`
                # here would emit `@openai:` picker entries the agent can't resolve on
                # the send path, so detect `openai-api` to match the registry (#3443).
                detected_providers.add("openai-api")
                # openai-codex uses ChatGPT OAuth (not OPENAI_API_KEY) for its default endpoint.
                # Detecting it here lets users who have both credentials configured find it in the
                # picker without a manual config.yaml edit. Users without Codex OAuth will see
                # picker entries but hit auth errors at inference time (#1189 known limitation).
                detected_providers.add("openai-codex")
            if all_env.get("OPENROUTER_API_KEY"):
                detected_providers.add("openrouter")
            if all_env.get("GOOGLE_API_KEY"):
                detected_providers.add("google")
            if all_env.get("GEMINI_API_KEY"):
                detected_providers.add("gemini")
            if all_env.get("GLM_API_KEY"):
                detected_providers.add("zai")
            if all_env.get("KIMI_API_KEY"):
                detected_providers.add("kimi-coding")
            if all_env.get("MINIMAX_API_KEY"):
                detected_providers.add("minimax")
            if all_env.get("MINIMAX_CN_API_KEY"):
                detected_providers.add("minimax-cn")
            if all_env.get("DEEPSEEK_API_KEY"):
                detected_providers.add("deepseek")
            if all_env.get("XIAOMI_API_KEY"):
                detected_providers.add("xiaomi")
            if all_env.get("XAI_API_KEY"):
                detected_providers.add("x-ai")
            if all_env.get("MISTRAL_API_KEY"):
                detected_providers.add("mistralai")
            if all_env.get("OPENCODE_ZEN_API_KEY") or all_env.get("OPENCODE_API_KEY"):
                detected_providers.add("opencode-zen")
            if all_env.get("OPENCODE_GO_API_KEY") or all_env.get("OPENCODE_API_KEY"):
                detected_providers.add("opencode-go")
            # AWS Bedrock uses IAM credentials rather than a single API key.
            # Detect when both access key and secret are available (#2720).
            if all_env.get("AWS_ACCESS_KEY_ID") and all_env.get("AWS_SECRET_ACCESS_KEY"):
                detected_providers.add("bedrock")
            # LM Studio: detect via LM_API_KEY + LM_BASE_URL in ~/.hermes/.env
            if all_env.get("LM_API_KEY") and all_env.get("LM_BASE_URL"):
                detected_providers.add("lmstudio")

        # Also detect providers explicitly listed in config.yaml providers section.
        # A user may configure a provider key via config.yaml providers.<name>.api_key
        # without setting the corresponding env var. (#604)
        #
        # Gating: only seed picker groups for keys whose canonical id is known
        # to ``_config_module._PROVIDER_MODELS`` / ``_config_module._PROVIDER_DISPLAY``, or whose value is a
        # dict-shaped provider config (custom/local). Scalar siblings under
        # ``providers:`` (e.g. ``providers.only_configured: true``) are config
        # flags, not providers, and must not render as phantom picker groups
        # like ``Only-Configured`` (#2399).
        #
        # Canonicalise the id slug here so a user with ``providers.opencode_go``
        # (underscore variant) doesn't see TWO provider groups in the picker —
        # one for the canonical ``opencode-go`` from active_provider detection
        # and a phantom ``Opencode_Go`` group for the config-key form (#1568).
        # The same applies to mixed-case ids like ``OpenCode-Go`` and to
        # legitimate aliases like ``z-ai`` → ``zai``.
        _cfg_providers = _config_module._get_providers_cfg()
        # Map canonical provider IDs back to raw config keys so the
        # generic-provider branch can preserve mixed-case/underscore
        # provider_cfg values (#2245).
        _canonical_to_raw_provider_key: dict[str, str] = {}
        if isinstance(_cfg_providers, dict):
            for _pid_key, _provider_cfg in _cfg_providers.items():
                _canonical = _config_module._canonicalise_provider_id(_pid_key)
                if not _canonical:
                    continue

                # See the gating comment on the block above. ``_config_module._PROVIDER_MODELS``
                # / ``_config_module._PROVIDER_DISPLAY`` membership accepts known providers and
                # aliases; ``isinstance(_provider_cfg, dict)`` accepts custom
                # entries that supply their own models/api_key/base_url. (#2399)
                _is_known_provider = (
                    _canonical in _config_module._PROVIDER_MODELS
                    or _canonical in _config_module._PROVIDER_DISPLAY
                    or _config_module._is_plugin_model_provider(_canonical)
                )
                _is_provider_config = isinstance(_provider_cfg, dict)
                _has_provider_route = False
                if _is_provider_config:
                    _has_provider_route = any(
                        str(_provider_cfg.get(_route_key) or "").strip()
                        for _route_key in ("api", "base_url", "api_key", "key_env")
                    )
                # A models-only provider config (no api/base_url/api_key/key_env)
                # is only admitted as evidence when it's the active/configured
                # provider (e.g. the lmstudio-style custom shape from #1970).
                # This must NOT re-open the door for a spurious duplicate alias
                # of a known provider (e.g. ``copilot-2: {name: "copilot",
                # models: {...}}`` from #644/dedup regression) — that case is
                # still rejected because it isn't the active provider and it
                # isn't a route-bearing config in its own right.
                _has_models_only_active_route = (
                    not _has_provider_route
                    and _is_provider_config
                    and isinstance(_provider_cfg.get("models"), (dict, list))
                    and _provider_cfg["models"]
                    and _canonical == _config_module._canonicalise_provider_id(active_provider)
                )
                if not (_is_known_provider or _has_provider_route or _has_models_only_active_route):
                    continue

                _canonical_to_raw_provider_key.setdefault(_canonical, _pid_key)
                detected_providers.add(_canonical)

        def _configured_provider_for_base_url(base_url: object) -> str:
            target = _config_module._normalize_base_url_for_match(base_url)
            if not target:
                return ""

            if isinstance(model_cfg, dict):
                model_base_url = _config_module._normalize_base_url_for_match(model_cfg.get("base_url"))
                if model_base_url == target:
                    provider_hint = _config_module._resolve_configured_provider_id(
                        model_cfg.get("provider"),
                        _config_module.cfg,
                        base_url=base_url,
                    )
                    if provider_hint:
                        return str(provider_hint).strip().lower()

            providers_cfg = _config_module.cfg.get("providers", {})
            if isinstance(providers_cfg, dict):
                for provider_key, provider_cfg in providers_cfg.items():
                    if not isinstance(provider_cfg, dict):
                        continue
                    provider_base_url = _config_module._normalize_base_url_for_match(
                        provider_cfg.get("base_url")
                    )
                    if provider_base_url == target:
                        provider_hint = _config_module._resolve_provider_alias(provider_key)
                        if provider_hint:
                            return str(provider_hint).strip().lower()

            custom_providers_cfg = _config_module.cfg.get("custom_providers", [])
            if isinstance(custom_providers_cfg, list):
                for entry in custom_providers_cfg:
                    if not isinstance(entry, dict):
                        continue
                    entry_base_url = _config_module._normalize_base_url_for_match(entry.get("base_url"))
                    if entry_base_url != target:
                        continue
                    entry_name = str(entry.get("name") or "").strip()
                    if entry_name:
                        return _config_module._custom_provider_slug_from_name(entry_name)
                    return "custom"

            return ""

        def _models_endpoint_for_base_url(base_url: str) -> str:
            base = str(base_url or "").strip().rstrip("/")
            if base.endswith("/v1"):
                return base + "/models"
            return base + "/v1/models"

        def _extract_model_entries_from_payload(data: object, provider: str) -> list[dict]:
            models_list = []
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    models_list = data["data"]
                elif "models" in data and isinstance(data["models"], list):
                    models_list = data["models"]
            models = []
            seen = set()
            for model in models_list:
                if not isinstance(model, dict):
                    continue
                model_id = (
                    model.get("id", "")
                    or model.get("name", "")
                    or model.get("model", "")
                )
                model_name = model.get("name", "") or model.get("model", "") or model_id
                model_id = str(model_id or "").strip()
                model_name = str(model_name or "").strip()
                if not model_id or not model_name or model_id in seen:
                    continue
                seen.add(model_id)
                label = _config_module._format_ollama_label(model_id) if provider in ("ollama", "ollama-cloud") else model_name
                models.append({"id": model_id, "label": label})
            return models

        def _custom_endpoint_error(
            provider: str,
            exc: Exception,
            *,
            code: int | None = None,
        ) -> dict:
            provider_label = str(provider or "custom").replace("custom:", "")
            status_code = code if code is not None else getattr(exc, "code", None)
            if status_code in (401, 403):
                return {
                    "kind": "auth",
                    "code": int(status_code),
                    "message": f"Models endpoint returned {status_code} — check the API key for {provider_label}.",
                }
            if isinstance(status_code, int):
                return {
                    "kind": "http",
                    "code": int(status_code),
                    "message": f"Models endpoint returned {status_code} for {provider_label}; see logs.",
                }
            return {
                "kind": "network",
                "code": None,
                "message": f"Models endpoint unreachable for {provider_label}; verify base_url.",
            }

        def _read_custom_endpoint_models(
            base_url: object,
            provider: str,
            *,
            api_key: object = "",
            trusted_base_urls: tuple[object, ...] = (),
        ) -> tuple[list[dict], dict | None]:
            base = str(base_url or "").strip()
            if not base:
                return [], None
            try:
                import ipaddress
                import urllib.error
                import urllib.request
                import socket

                endpoint_url = _models_endpoint_for_base_url(base)
                headers = {}
                key = str(api_key or "").strip()
                if key:
                    headers["Authorization"] = f"Bearer {key}"

                # User-configured custom provider endpoints are explicitly trusted,
                # but keep the same private-IP guard for non-matching targets used by
                # the legacy active model.base_url path.
                _ssrf_trusted_hosts: set[str] = set()
                for trusted in (base, *trusted_base_urls):
                    _cp_parsed = urlparse(
                        str(trusted) if "://" in str(trusted) else f"http://{trusted}"
                    )
                    if _cp_parsed.hostname:
                        _ssrf_trusted_hosts.add(_cp_parsed.hostname.lower())

                parsed_url = urlparse(endpoint_url if "://" in endpoint_url else f"http://{endpoint_url}")
                if parsed_url.scheme not in ("", "http", "https"):
                    raise ValueError(f"Invalid URL scheme: {parsed_url.scheme}")
                if parsed_url.hostname:
                    try:
                        resolved_ips = socket.getaddrinfo(parsed_url.hostname, None)
                        for _, _, _, _, addr in resolved_ips:
                            addr_obj = ipaddress.ip_address(addr[0])
                            if addr_obj.is_private or addr_obj.is_loopback or addr_obj.is_link_local:
                                host_l = (parsed_url.hostname or "").lower()
                                is_known_local = any(
                                    k in host_l
                                    for k in ("ollama", "localhost", "127.0.0.1", "lmstudio", "lm-studio")
                                ) or host_l in _ssrf_trusted_hosts
                                if not is_known_local:
                                    raise ValueError(f"SSRF: resolved hostname to private IP {addr[0]}")
                    except socket.gaierror:
                        pass

                req = urllib.request.Request(endpoint_url, method="GET")
                req.add_header("User-Agent", "OpenAI/Python 1.0")
                for k, v in headers.items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=_config_module.CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS) as response:  # nosec B310
                    data = json.loads(response.read().decode("utf-8"))
                return _extract_model_entries_from_payload(data, provider), None
            except urllib.error.HTTPError as exc:
                error = _custom_endpoint_error(provider, exc, code=getattr(exc, "code", None))
                logger.debug("Custom endpoint models fetch failed for provider %s: %s", provider, error)
                return [], error
            except Exception as exc:
                error = _custom_endpoint_error(provider, exc)
                logger.debug("Custom endpoint unreachable or misconfigured for provider %s: %s", provider, error)
                return [], error

        # 4. Fetch models from custom endpoint if base_url is configured
        auto_detected_models = []
        auto_detected_models_by_provider: dict[str, list[dict]] = {}
        if cfg_base_url:
            base_url = cfg_base_url.strip()
            configured_provider = _configured_provider_for_base_url(base_url)
            provider = configured_provider or "custom"
            provider_from_config = bool(configured_provider)
            parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
            host = (parsed.netloc or parsed.path).lower()

            if parsed.hostname and not provider_from_config:
                try:
                    import ipaddress

                    addr = ipaddress.ip_address(parsed.hostname)
                    if addr.is_private or addr.is_loopback or addr.is_link_local:
                        if "ollama" in host or "127.0.0.1" in host or "localhost" in host:
                            provider = "ollama"
                        elif "lmstudio" in host or "lm-studio" in host:
                            provider = "lmstudio"
                        else:
                            # Unknown loopback/private endpoint: route through
                            # the generic ``custom`` provider so the agent's
                            # auxiliary client (compression, vision, web
                            # extraction) takes the OpenAI-compat custom path
                            # with ``no-key-required`` semantics. Writing
                            # ``provider: local`` here used to break
                            # compression mid-conversation because ``local``
                            # is not a registered provider in
                            # ``hermes_cli.auth.PROVIDER_REGISTRY`` — see #1384.
                            provider = "custom"
                except ValueError:
                    pass

            api_key = ""
            if isinstance(model_cfg, dict):
                api_key = (model_cfg.get("api_key") or "").strip()
            if not api_key:
                providers_cfg = _config_module.cfg.get("providers", {})
                if isinstance(providers_cfg, dict):
                    for provider_key in filter(None, [active_provider, "custom"]):
                        provider_cfg = providers_cfg.get(provider_key, {})
                        if isinstance(provider_cfg, dict):
                            api_key = (provider_cfg.get("api_key") or "").strip()
                            if api_key:
                                break
            if not api_key:
                api_key_vars = (
                    "HERMES_API_KEY",
                    "HERMES_OPENAI_API_KEY",
                    "OPENAI_API_KEY",
                    "LOCAL_API_KEY",
                    "OPENROUTER_API_KEY",
                    "API_KEY",
                )
                for key in api_key_vars:
                    api_key = (all_env.get(key) or _config_module._thread_local_env_value(key) or "").strip()
                    if api_key:
                        break

            _trusted_custom_bases: list[object] = [cfg_base_url]
            _custom_providers_for_trust = _config_module.cfg.get("custom_providers", [])
            if isinstance(_custom_providers_for_trust, list):
                _trusted_custom_bases.extend(
                    _cp.get("base_url")
                    for _cp in _custom_providers_for_trust
                    if isinstance(_cp, dict) and _cp.get("base_url")
                )
            _active_endpoint_models, _active_endpoint_error = _read_custom_endpoint_models(
                base_url,
                provider,
                api_key=api_key,
                trusted_base_urls=tuple(_trusted_custom_bases),
            )
            for auto_model in _active_endpoint_models:
                auto_detected_models.append(auto_model)
                provider_key = provider.lower()
                auto_detected_models_by_provider.setdefault(provider_key, []).append(auto_model)
                detected_providers.add(provider_key)

        _custom_providers_cfg = _config_module.cfg.get("custom_providers", [])
        _named_custom_groups: dict = {}
        _named_custom_errors: dict[str, dict] = {}
        if isinstance(_custom_providers_cfg, list):
            _seen_custom_ids = set()
            for _cp in _custom_providers_cfg:
                if not isinstance(_cp, dict):
                    continue
                _cp_name = (_cp.get("name") or "").strip()
                _slug = _config_module._custom_provider_slug_from_name(_cp_name) if _cp_name else None
                if _slug and _slug not in _named_custom_groups:
                    _named_custom_groups[_slug] = (_cp_name, [])

                _cp_base_url = str(_cp.get("base_url") or "").strip()
                _cp_api_key = str(_cp.get("api_key") or "").strip()
                if not _cp_api_key:
                    _cp_key_env = str(_cp.get("key_env") or "").strip()
                    if _cp_key_env:
                        _cp_api_key = _config_module._thread_local_env_value(_cp_key_env).strip()
                # Fallback: check credential pool for both api_key and base_url
                if (not _cp_api_key or not _cp_base_url) and _slug:
                    try:
                        from api.config import _has_explicit_pool_credentials
                        if _has_explicit_pool_credentials(_slug):
                            from agent.credential_pool import load_pool
                            _resolved = _config_module._resolve_provider_alias(_slug)
                            _pool = load_pool(_resolved)
                            if _pool:
                                _entry = _pool.select()
                                if _entry:
                                    if not _cp_api_key:
                                        _cp_api_key = getattr(_entry, "runtime_api_key", "") or ""
                                    if not _cp_base_url:
                                        _cp_base_url = str(getattr(_entry, "base_url", "") or "").strip()
                    except ImportError:
                        pass

                if _slug and _cp_base_url:
                    # Check if user has configured models in config.yaml —
                    # configured models take priority over live /v1/models
                    # discovery (same as hermes-agent model_switch.py Section 4
                    # patch). Without this check, ZenMux and similar aggregator
                    # gateways would show hundreds of online models instead of
                    # the user's curated list.
                    _cp_configured_models = _cp.get("models")
                    _cp_has_configured_models = (
                        isinstance(_cp_configured_models, (dict, list))
                        and len(_cp_configured_models) > 0
                    )
                    _live_models = auto_detected_models_by_provider.get(_slug)
                    _live_error = None
                    if _cp_has_configured_models:
                        # Skip the live /v1/models probe when an allowlist
                        # exists — the curated list wins and probe failures
                        # should not surface as a user-facing diagnostic in
                        # that case. Still respect any pre-warm result that
                        # ``auto_detected_models_by_provider`` already
                        # populated (cheap to keep).
                        if _live_models is None:
                            _live_models = []
                    elif _live_models is None:
                        _live_models, _live_error = _read_custom_endpoint_models(
                            _cp_base_url,
                            _slug,
                            api_key=_cp_api_key,
                            trusted_base_urls=(_cp_base_url,),
                        )
                    if _live_error:
                        _named_custom_errors[_slug] = _live_error
                        detected_providers.add(_slug)
                    for _live_model in _live_models:
                        _live_id = str(_live_model.get("id") or "").strip()
                        if not _live_id:
                            continue
                        _dedup_key = f"{_slug}:{_live_id}"
                        if _dedup_key in _seen_custom_ids:
                            continue
                        _seen_custom_ids.add(_dedup_key)
                        detected_providers.add(_slug)
                        _cp_option_id = _live_id
                        if active_provider != _slug and not _cp_option_id.startswith("@"):
                            _cp_option_id = f"@{_slug}:{_cp_option_id}"
                        _named_custom_groups[_slug][1].append(
                            {"id": _cp_option_id, "label": _live_model.get("label") or _get_label_for_model(_live_id, [])}
                        )

                # Collect configured model IDs as a fallback/sticky entry after live discovery.
                _cp_model_ids: list[str] = []
                _cp_model = _cp.get("model", "")
                if _cp_model:
                    _cp_model_ids.append(_cp_model)
                for _cp_model_id in _config_module._configured_model_ids(_cp.get("models")):
                    if _cp_model_id not in _cp_model_ids:
                        _cp_model_ids.append(_cp_model_id)

                for _cp_model in _cp_model_ids:
                    _dedup_key = f"{_slug}:{_cp_model}" if _slug else _cp_model
                    if _cp_model and _dedup_key not in _seen_custom_ids:
                        _cp_label = _get_label_for_model(_cp_model, [])
                        _seen_custom_ids.add(_dedup_key)
                        if _slug:
                            detected_providers.add(_slug)
                            _cp_option_id = _cp_model
                            if active_provider != _slug and not _cp_option_id.startswith("@"):
                                _cp_option_id = f"@{_slug}:{_cp_option_id}"
                            _named_custom_groups[_slug][1].append(
                                {"id": _cp_option_id, "label": _cp_label}
                            )
                        else:
                            auto_detected_models.append({"id": _cp_model, "label": _cp_label})
                            detected_providers.add("custom")

        _has_custom_providers = isinstance(_custom_providers_cfg, list) and len(_custom_providers_cfg) > 0
        if active_provider and active_provider != "custom" and not _has_custom_providers:
            detected_providers.discard("custom")
            for _slug in list(detected_providers):
                if _slug.startswith("custom:") and not _has_custom_providers:
                    detected_providers.discard(_slug)
        elif active_provider == "custom" and _has_custom_providers:
            _has_unnamed = any(
                isinstance(_cp, dict) and not (_cp.get("name") or "").strip()
                for _cp in _custom_providers_cfg
            )
            if not _has_unnamed:
                detected_providers.discard("custom")

        _named_custom_slugs = _config_module._named_custom_provider_slugs(_config_module.cfg)
        _base_matched_named_slug = _config_module._named_custom_provider_slug_for_base_url(cfg_base_url, _config_module.cfg)
        if _base_matched_named_slug and _named_custom_slugs:
            for _pid in list(detected_providers):
                _pid_norm = str(_pid or "").strip().lower()
                if _pid_norm.startswith("custom:") and _pid_norm not in _named_custom_slugs:
                    detected_providers.discard(_pid)

        # Filter providers if providers.only_configured is set
        providers_cfg = _config_module.cfg.get("providers", {})
        only_show_configured = providers_cfg.get("only_configured", False) if isinstance(providers_cfg, dict) else False
        if only_show_configured:
            configured_providers = set()
            if active_provider:
                configured_providers.add(active_provider)
            cfg_providers = _config_module.cfg.get("providers", {})
            if isinstance(cfg_providers, dict):
                # Canonicalise here too — same rationale as #1568 detection
                # path. Without this, only_show_configured mode could
                # exclude detected ``opencode-go`` because configured_providers
                # only has the underscore-variant key from config.yaml.
                configured_providers.update(
                    _config_module._canonicalise_provider_id(k) or k for k in cfg_providers.keys()
                )
            # Only show providers that are both detected and configured
            detected_providers = detected_providers.intersection(configured_providers)

        # Post-collection dedup: re-canonicalise every entry so any path that
        # added a non-canonical id (mixed-case from auth-store, raw config-key,
        # legacy alias) gets folded onto the canonical key. Belt-and-braces for
        # #1568 — protects against future regressions in any of the ~25
        # `detected_providers.add(...)` callsites without auditing each one.
        # The fold is idempotent for already-canonical ids, so safe to run
        # unconditionally.
        if detected_providers:
            _canonicalised_detected = set()
            for _pid in detected_providers:
                _c = _config_module._canonicalise_provider_id(_pid) or _pid
                _canonicalised_detected.add(_c)
            detected_providers = _canonicalised_detected

        try:
            _moa_cfg = _config_module.cfg.get("moa") if isinstance(_config_module.cfg, dict) else None
            if isinstance(_moa_cfg, dict):
                _moa_enabled = bool(_moa_cfg.get("enabled", True))
                _moa_presets = _moa_cfg.get("presets")
                if _moa_enabled and isinstance(_moa_presets, dict) and _moa_presets:
                    detected_providers.add("moa")
        except Exception:
            logger.debug("Failed to inspect MoA presets for model picker", exc_info=True)

        # 5. Build model groups
        if detected_providers:
            _picker_selected_model_id = (
                (model_cfg.get("model") if isinstance(model_cfg, dict) else None)
                or default_model
                or None
            )

            def _append_picker_group(
                provider_label: str,
                provider_id: str,
                raw_models: list[dict] | None,
                *,
                models_endpoint_error: dict | None = None,
                apply_prefix: bool = True,
                decorate_overflow_label: bool = False,
                allow_empty: bool = False,
            ) -> None:
                picker_models = copy.deepcopy(raw_models or [])
                if _config_module._is_openai_family_provider(provider_id):
                    for _model in picker_models:
                        if not isinstance(_model, dict):
                            continue
                        _model_id = str(_model.get("id") or "").strip()
                        if not _model_id:
                            continue
                        _model["supports_fast_tier"] = (
                            str(
                                _config_module._resolve_main_model_fast_mode_overrides(_model_id, provider_id).get("service_tier", "")
                            ).strip().lower()
                            == "priority"
                        )
                if apply_prefix:
                    picker_models = _config_module._apply_provider_prefix(picker_models, provider_id, active_provider)
                visible_models, extra_models = _config_module._split_picker_overflow_models(
                    picker_models,
                    selected_model_id=_picker_selected_model_id,
                    provider_id=provider_id,
                )
                if not (visible_models or extra_models or models_endpoint_error or allow_empty):
                    return
                group_entry = {
                    "provider": provider_label,
                    "provider_id": provider_id,
                    "models": visible_models,
                }
                if decorate_overflow_label and extra_models:
                    group_entry["provider"] = (
                        f"{provider_label} ({len(visible_models)} of {len(visible_models) + len(extra_models)})"
                    )
                if extra_models:
                    group_entry["extra_models"] = extra_models
                if models_endpoint_error:
                    group_entry["models_endpoint_error"] = models_endpoint_error
                groups.append(group_entry)

            for pid in sorted(detected_providers):
                # Custom-provider PIDs are populated above via the
                # _named_custom_groups branch (or skipped intentionally).
                # They MUST NOT fall through to the auto_detected_models
                # fallback below, otherwise the active provider's models
                # get copied into a phantom Custom group with mismatched
                # provider prefixes (#1881).
                if pid.startswith("custom:"):
                    if pid in _named_custom_groups:
                        _nc_display, _nc_models = _named_custom_groups[pid]
                        # If all named-group models were deduped (already auto-detected
                        # from base_url /v1/models), fall back to auto-detected models
                        # instead of silently dropping the group (issue #1619).
                        #
                        # Per Opus advisor on stage-295: the load-bearing fix for the
                        # reporter's symptom is the api/routes.py:/api/models/live
                        # broadening to handle custom:* slugs. This block is defensive
                        # belt-and-braces — under current _named_custom_groups
                        # population logic (atomic add+append inside the same dedup
                        # guard at line ~2640), an empty list shouldn't reach here.
                        # Kept for future-proofing in case the population logic
                        # changes (e.g. supporting model-less custom_providers entries).
                        if not _nc_models:
                            _nc_models = auto_detected_models_by_provider.get(pid, [])
                        if _nc_models or pid in _named_custom_errors:
                            _append_picker_group(
                                _nc_display,
                                pid,
                                _nc_models,
                                models_endpoint_error=_named_custom_errors.get(pid),
                                apply_prefix=False,
                            )
                    continue
                provider_name = _config_module._effective_provider_display_name(pid, _config_module._PROVIDER_DISPLAY)
                if pid == "openrouter":
                    # OpenRouter has two model surfaces:
                    #   (1) curated tool-supporting catalog via hermes_cli.models.fetch_openrouter_models()
                    #       — the canonical agent-ready list, applies a tool-support filter
                    #       (Kilo-Org/kilocode#9068) that hides image/completion-only models
                    #   (2) free-tier `:free` variants — newly-added models OpenRouter ships
                    #       experimentally that may not yet advertise `tools` in supported_parameters
                    #       (see #1426). These get filtered out of (1) but users want them visible.
                    #
                    # Strategy: take the live curated list as the base, then augment with a
                    # separate live-fetch of OpenRouter's /v1/models filtered to free-tier-only.
                    # Free-tier entries get a "(free)" label suffix so the picker is honest about
                    # what the user is selecting. Falls back to the static _config_module._FALLBACK_MODELS list
                    # when both live fetches fail (offline, transient API error, test env).
                    raw_models = []
                    seen_ids = set()
                    try:
                        from hermes_cli.models import (
                            fetch_openrouter_models as _fetch_or_models,
                        )
                        live_curated = _fetch_or_models() or []
                        for mid, _desc in live_curated:
                            if mid and mid not in seen_ids:
                                seen_ids.add(mid)
                                raw_models.append({"id": mid, "label": mid})
                    except Exception:
                        logger.warning("Failed to load OpenRouter curated catalog from hermes_cli")

                    # Free-tier live fetch — bypasses the tool-support filter so models
                    # OpenRouter has flagged free but hasn't yet annotated with tools=[]
                    # (or that have tools=[] but the user explicitly wants to try) appear.
                    try:
                        import urllib.request as _urlreq
                        _req = _urlreq.Request(
                            "https://openrouter.ai/api/v1/models",
                            headers={"Accept": "application/json"},
                        )
                        free_tier_models = []
                        selected_free_tier_model = None
                        with _urlreq.urlopen(_req, timeout=8.0) as _resp:
                            _payload = json.loads(_resp.read().decode())
                        for _item in _payload.get("data", []) or []:
                            if not isinstance(_item, dict):
                                continue
                            _mid = str(_item.get("id") or "").strip()
                            if not _mid or _mid in seen_ids:
                                continue
                            _pricing = _item.get("pricing")
                            _is_free = False
                            if (
                                isinstance(_pricing, dict)
                                and "prompt" in _pricing
                                and "completion" in _pricing
                            ):
                                try:
                                    _is_free = (
                                        float(_pricing["prompt"]) == 0
                                        and float(_pricing["completion"]) == 0
                                    )
                                except (TypeError, ValueError):
                                    _is_free = False
                            # Also include explicit `:free` suffix variants
                            _is_free = _is_free or _mid.endswith(":free")
                            if not _is_free:
                                continue
                            _name = (
                                str(_item.get("name") or "").strip() or _mid
                            )
                            # Strip provider prefix from name for display, append (free)
                            _label = _name.split("/")[-1] if "/" in _name else _name
                            if "(free)" not in _label.lower():
                                _label = f"{_label} (free)"
                            _entry = {"id": _mid, "label": _label}
                            free_tier_models.append(_entry)
                            if _config_module._model_matches_picker_selection(
                                _mid,
                                _picker_selected_model_id,
                                "openrouter",
                            ):
                                selected_free_tier_model = _entry
                        if len(free_tier_models) > _config_module._OPENROUTER_FREE_TIER_AUGMENT_CAP:
                            free_tier_models = free_tier_models[:_config_module._OPENROUTER_FREE_TIER_AUGMENT_CAP]
                            if (
                                selected_free_tier_model
                                and not any(
                                    m.get("id") == selected_free_tier_model.get("id")
                                    for m in free_tier_models
                                )
                            ):
                                free_tier_models[-1] = selected_free_tier_model
                        for _entry in free_tier_models:
                            seen_ids.add(_entry["id"])
                            raw_models.append(_entry)
                    except Exception:
                        logger.debug("OpenRouter free-tier live fetch unavailable; using fallback")

                    if not raw_models:
                        # Both live fetches failed — fall back to the curated static list.
                        # Deepcopy so dedup/prefix mutation downstream does not bleed
                        # into the module-level catalog.
                        raw_models = [
                            {"id": m["id"], "label": m["label"]}
                            for m in _config_module._FALLBACK_MODELS
                            if m.get("provider") == "OpenRouter"
                        ]

                    _append_picker_group("OpenRouter", "openrouter", raw_models)
                elif pid == "ollama-cloud":
                    raw_models = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        raw_models = [
                            {"id": mid, "label": _config_module._format_ollama_label(mid)}
                            for mid in (_provider_model_ids("ollama-cloud") or [])
                        ]
                    except Exception:
                        logger.warning("Failed to load Ollama Cloud models from hermes_cli")

                    if raw_models:
                        _append_picker_group(provider_name, pid, raw_models)
                elif pid == "openai-codex":
                    # Codex account catalogs drift faster than WebUI releases
                    # (for example gpt-5.3-codex-spark in #1680). Ask the
                    # agent's Codex resolver first so /api/models inherits the
                    # live Codex API / local ~/.codex cache / static fallback
                    # chain instead of freezing the picker to WebUI's curated
                    # _config_module._PROVIDER_MODELS snapshot.
                    raw_models = []
                    codex_ids = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        codex_ids = [mid for mid in (_provider_model_ids("openai-codex") or []) if mid]
                    except Exception:
                        logger.warning("Failed to load OpenAI Codex models from hermes_cli")

                    for mid in _read_visible_codex_cache_model_ids():
                        if mid not in codex_ids:
                            codex_ids.append(mid)

                    raw_models = [
                        {"id": mid, "label": _get_label_for_model(mid, [])}
                        for mid in codex_ids
                    ]

                    if not raw_models:
                        raw_models = copy.deepcopy(_config_module._PROVIDER_MODELS.get("openai-codex", []))

                    if raw_models:
                        _append_picker_group(provider_name, pid, raw_models)
                elif pid == "nous":
                    # Nous Portal exposes a curated catalog (~30 models on most
                    # accounts, up to several hundred for enterprise tiers) via
                    # inference-api.nousresearch.com. Like ollama-cloud, we
                    # live-fetch through hermes_cli.models.provider_model_ids()
                    # rather than relying on the static four-entry list, which
                    # chronically drifts out of date (#1538).
                    #
                    # When the catalog exceeds _NOUS_FEATURED_THRESHOLD (~25)
                    # the picker dropdown gets a curated subset to stay
                    # scannable — the full list is still returned under
                    # "extra_models" for the slash-command autocomplete and
                    # the dynamic-label map (#1567). The optgroup label is
                    # decorated with the truncation count so users know more
                    # exists.
                    raw_models = []
                    live_fetch_failed = False
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids

                        live_ids = _provider_model_ids("nous") or []
                    except Exception:
                        logger.warning("Failed to load Nous Portal models from hermes_cli")
                        live_ids = []
                        live_fetch_failed = True

                    if live_ids:
                        featured_ids, extras_ids = _config_module._build_nous_featured_set(
                            live_ids,
                            selected_model_id=_picker_selected_model_id,
                        )
                        ordered_ids = featured_ids + extras_ids
                        raw_models = [
                            {"id": f"@nous:{mid}", "label": _config_module._format_nous_label(mid)}
                            for mid in ordered_ids
                        ]
                    elif not live_fetch_failed:
                        # Live-fetch returned an empty list AND did not raise —
                        # the user is gated as authenticated by detection above
                        # but the catalog endpoint replied with no models.
                        # Showing the static 4-entry curated list here would
                        # contradict the providers card (which always shows
                        # the live catalog) — exactly the asymmetry #1567
                        # reports. Omit the Nous group entirely; the providers
                        # card already tells the truth, and a transient empty
                        # response will self-heal on the next cache rebuild.
                        logger.warning(
                            "Nous Portal authenticated but live-fetch returned empty — "
                            "omitting from picker (will retry on next cache rebuild)"
                        )
                    else:
                        # hermes_cli unavailable / raised — fall back to the
                        # curated 4-entry static list so the picker is never
                        # empty in this degraded state. This matches pre-#1538
                        # behaviour for environments without hermes_cli (test
                        # envs, package mismatches, isolated WebUI builds).
                        raw_models = copy.deepcopy(_config_module._PROVIDER_MODELS.get("nous", []))

                    if raw_models:
                        _append_picker_group(
                            provider_name,
                            pid,
                            raw_models,
                            apply_prefix=False,
                            decorate_overflow_label=True,
                        )
                elif pid == "lmstudio":
                    # LM Studio is a local server — fetch live loaded models via
                    # the OpenAI-compatible /v1/models endpoint (#WebUI).
                    #
                    # Two-tier lookup, each in its own try so a failure in one
                    # does not abort the other (the bug pattern that broke
                    # tests/test_issue1527_lmstudio_base_url_classification on
                    # CI environments where hermes_cli isn't importable —
                    # ImportError in the cli tier was hijacking the whole
                    # branch and silently skipping the urlopen fallback).
                    raw_models = []
                    lm_ids: list[str] = []
                    try:
                        from hermes_cli.models import provider_model_ids as _provider_model_ids
                        lm_ids = _provider_model_ids("lmstudio") or []
                    except Exception:
                        logger.debug("hermes_cli LM Studio lookup unavailable; using urlopen fallback")

                    if lm_ids:
                        raw_models = [{"id": mid, "label": mid} for mid in lm_ids]
                    else:
                        # Fallback: fetch /models directly from the configured
                        # base URL. Looks for the URL in either
                        # `_config_module.cfg["providers"]["lmstudio"]["base_url"]` or
                        # `_config_module.cfg["model"]["base_url"]` (via _config_module._get_provider_base_url),
                        # so the historical model-block config shape still works.
                        lm_cfg = _config_module._get_provider_cfg("lmstudio")
                        lm_base_url = _config_module._get_provider_base_url("lmstudio") or ""
                        lm_api_key = str(lm_cfg.get("api_key") or "").strip()
                        if lm_base_url:
                            headers = {"User-Agent": "OpenAI/Python 1.0"}
                            if lm_api_key:
                                headers["Authorization"] = f"Bearer {lm_api_key}"
                            endpoint = (lm_base_url + "/models").rstrip("/")
                            try:
                                import urllib.request as _urlreq
                                req = _urlreq.Request(endpoint, method="GET", headers=headers)
                                with _urlreq.urlopen(req, timeout=5) as resp:
                                    lm_data = json.loads(resp.read().decode())
                                for m in (lm_data.get("data") or []):
                                    if isinstance(m, dict):
                                        mid = str(m.get("id") or "").strip()
                                        if mid and {"id": mid, "label": mid} not in raw_models:
                                            raw_models.append({"id": mid, "label": mid})
                            except Exception:
                                logger.debug("LM Studio /models fetch failed at %s", endpoint)

                    if raw_models:
                        _append_picker_group(provider_name, pid, raw_models)
                elif (
                    pid in _config_module._PROVIDER_MODELS
                    or pid in _config_module._PROVIDER_DISPLAY
                    or pid in _canonical_to_raw_provider_key
                    or _config_module._is_plugin_model_provider(pid)
                ):
                    # Look up provider_cfg using the original raw key from
                    # config.yaml so that mixed-case / underscore keys like
                    # ``CLIPpoxy`` or ``snake_case_provider`` still resolve
                    # (#2245).  Fall back to the canonical pid for providers
                    # that appear in _config_module._PROVIDER_MODELS but not in _config_module.cfg.
                    _raw_key = _canonical_to_raw_provider_key.get(pid, pid)
                    provider_cfg = _config_module._get_provider_cfg(_raw_key)
                    raw_models = []

                    # User-configured model allowlists are explicit local
                    # source-of-truth for custom/plugin providers, AND for most
                    # built-in Hermes providers (e.g. providers.anthropic.models
                    # is a real picker allowlist — see #644). Copilot is the
                    # exception: it uses providers.copilot.models as a per-model
                    # settings map (reasoning_effort, limits, etc.), so treating
                    # that as an allowlist collapsed the Copilot picker to
                    # whichever model had local settings. Only Copilot skips the
                    # config-models allowlist branch and asks Hermes CLI for the
                    # live catalog first (static _config_module._PROVIDER_MODELS is fallback only).
                    _uses_models_as_settings_map = pid == "copilot"
                    if (
                        not _uses_models_as_settings_map
                        and isinstance(provider_cfg, dict)
                        and "models" in provider_cfg
                    ):
                        raw_models = _config_module._configured_model_options(provider_cfg["models"])

                    if not raw_models:
                        if pid == "moa":
                            raw_models = _moa_preset_models_from_config(_config_module.cfg)
                        elif pid == "opencode-go":
                            # Skip live /v1/models probe for OpenCode Go — it
                            # returns models from the public catalog that are
                            # not enabled on the Go tier, causing 404 when
                            # selected. Use the curated static list only. (#5311)
                            pass
                        else:
                            raw_models = _models_from_live_provider_ids(
                                pid,
                                _read_live_provider_model_ids(pid),
                            )

                    if not raw_models:
                        raw_models = copy.deepcopy(_config_module._PROVIDER_MODELS.get(pid, []))

                    detected_models = auto_detected_models_by_provider.get(pid, [])
                    if detected_models and not raw_models:
                        raw_models = copy.deepcopy(detected_models)
                    _append_picker_group(provider_name, pid, raw_models)
                else:
                    detected_models = auto_detected_models_by_provider.get(pid)
                    if detected_models:
                        models_for_group = copy.deepcopy(detected_models)
                    elif auto_detected_models and (pid == "custom" or _config_module._is_known_model_provider(pid)):
                        # Don't fall back to the global auto_detected_models
                        # list for the bare "custom" PID when the active
                        # provider is something concrete (e.g. ai-gateway,
                        # openrouter). Those auto-detected entries already
                        # belong to the active provider's group — copying
                        # them into a Custom group too produces phantom
                        # duplicates with mismatched prefixes (#1881).
                        if pid == "custom" and active_provider and active_provider != "custom":
                            models_for_group = []
                        else:
                            models_for_group = copy.deepcopy(auto_detected_models)
                    else:
                        # An unrecognized provider id with no catalog of its
                        # own must NOT be painted with the global
                        # auto_detected_models list. Otherwise a non-model
                        # credential_pool key (the Photon plugin's
                        # photon/photon_project/photon_user entries, #4324) or
                        # any future unknown id renders as a phantom provider
                        # carrying the active endpoint's entire model catalog.
                        # Such ids are dropped upstream by
                        # _config_module._is_known_model_provider() in the pool-detection
                        # loop; this omission is belt-and-braces matching the
                        # #1572/#7372 "omit rather than misattribute" posture.
                        models_for_group = []
                    if models_for_group:
                        # Per-group deep copy so subsequent mutation by
                        # _config_module._deduplicate_model_ids() (which prefixes ids with
                        # @provider_id:) does not bleed into other groups
                        # that also fall through to this branch (#1511 root
                        # cause: multiple unconfigured providers all sharing
                        # the same auto_detected_models list reference would
                        # see every group's id rewritten to the FIRST
                        # provider's prefix, and labels accumulated every
                        # provider's name).
                        _append_picker_group(
                            provider_name,
                            pid,
                            models_for_group,
                            apply_prefix=False,
                        )
                    elif pid == "custom" and cfg_base_url:
                        # Anonymous custom endpoint: /v1/models probe may have
                        # failed (e.g. llama-server, lightweight relay), but the
                        # chat endpoint itself may still work. Add the group
                        # with an empty model list so the user can type a model
                        # ID manually rather than being blocked by a silent
                        # probe failure (#2542).
                        groups.append(
                            {
                                "provider": provider_name,
                                "provider_id": pid,
                                "models": [],
                            }
                        )
        else:
            if default_model:
                label = _get_label_for_model(default_model, groups)
                groups.append(
                    {"provider": "Default", "provider_id": "default", "models": [{"id": default_model, "label": label}]}
                )

        if default_model:
            # Guard against provider-id values mistakenly stored in
            # ``model.default``. The injection logic below puts ANY string
            # into the picker as a fake option, so a stray provider id
            # surfaces as a self-referential phantom model labelled e.g.
            # ``Opencode GO`` — a 15th entry under the OpenCode Go group
            # (#1568). The user's misconfig is real, but the picker is
            # the wrong surface to surface it; we'd rather skip injection
            # and emit a warning so the underlying config issue is logged.
            _looks_like_provider_id = (
                str(default_model).strip().lower().replace("_", "-") in _config_module._PROVIDER_DISPLAY
                or _config_module._canonicalise_provider_id(default_model) in _config_module._PROVIDER_DISPLAY
            )
            if _looks_like_provider_id:
                logger.warning(
                    "Suspicious model.default value %r — looks like a provider id, "
                    "not a model id. Skipping picker injection. Check `model.default` "
                    "in config.yaml.",
                    default_model,
                )
            else:
                all_ids_norm = {
                    _norm_model_id(m["id"])
                    for g in groups
                    for bucket_name in ("models", "extra_models")
                    for m in g.get(bucket_name, [])
                }
                if _norm_model_id(default_model) not in all_ids_norm:
                    label = _get_label_for_model(default_model, groups)
                    target_display = (
                        _config_module._PROVIDER_DISPLAY.get(active_provider, active_provider or "").lower()
                        if active_provider
                        else ""
                    )
                    injected = False
                    for g in groups:
                        if target_display and g.get("provider", "").lower() == target_display:
                            g["models"].insert(0, {"id": default_model, "label": label})
                            injected = True
                            break
                    if not injected and groups:
                        groups.append(
                            {
                                "provider": "Default",
                                "provider_id": active_provider or "default",
                                "models": [{"id": default_model, "label": label}],
                            }
                        )

        # Post-process: ensure model IDs are globally unique across groups.
        # When multiple providers expose the same bare model ID, prefix
        # collisions with @provider_id: so the frontend can distinguish them.
        _config_module._deduplicate_model_ids(groups)

        # Defense-in-depth: drop any optgroup that ended up with zero models
        # — those are pure UI noise. A zero-model group typically means a
        # detection path added an id that has no static catalog AND the
        # live-fetch returned empty (#1568 — the user's
        # ``providers.opencode_go`` config-key path produced an empty
        # ``Opencode_Go`` group at the end of the picker before this fix).
        # Custom providers from ``custom_providers`` config are exempt —
        # they may legitimately render with zero entries when the user
        # hasn't filled in models yet but wants the card visible.
        groups = [
            g for g in groups
            if g.get("models")
            or (g.get("provider_id") or "").startswith("custom:")
        ]

        # Sort groups: active provider first, then custom:* providers,
        # then providers with configured keys, then the rest alphabetically.
        _providers_with_keys: set[str] = set()
        try:
            _pool = auth_store.get("credential_pool", {}) if isinstance(auth_store, dict) else {}
            if isinstance(_pool, dict):
                for _pid in _pool:
                    _providers_with_keys.add(_config_module._resolve_provider_alias(str(_pid)))
        except Exception:
            pass
        try:
            _cfg_providers = _config_module.cfg.get("providers", {})
            if isinstance(_cfg_providers, dict):
                for _pk, _pv in _cfg_providers.items():
                    if isinstance(_pv, dict) and (_pv.get("api_key") or _pv.get("key_env")):
                        _providers_with_keys.add(_config_module._resolve_provider_alias(str(_pk)))
        except Exception:
            pass

        def _group_sort_key(g):
            pid = g.get("provider_id") or ""
            if pid == active_provider:
                return (0, pid)
            if pid.startswith("custom:"):
                return (1, pid)
            if pid in _providers_with_keys:
                return (2, pid)
            return (3, pid)
        groups.sort(key=_group_sort_key)

        # 12. Include model aliases so the WebUI frontend can resolve them.
        model_aliases: dict[str, str] = {}
        try:
            raw_aliases = _config_module.cfg.get("model", {}).get("aliases", {})
            if isinstance(raw_aliases, dict):
                model_aliases = {str(k).strip(): str(v).strip() for k, v in raw_aliases.items() if k and v}
        except Exception:
            pass

        return {
            "active_provider": active_provider,
            "default_model": default_model,
            "configured_model_badges": _build_configured_model_badges(),
            "groups": groups,
            "aliases": model_aliases,
        }

    # ── FAST PATH ─────────────────────────────────────────────────────────────
    # Mark that a build may be in progress BEFORE acquiring the lock.
    # If another thread has already started the cold path, we will wait for
    # its result rather than running the cold path concurrently.
    should_wait = MODEL_CATALOG_STATE.cache_build_in_progress
    force_refresh_started_at = time.monotonic() if force_refresh else None

    # Check config mtime OUTSIDE the lock so this cheap check doesn't serialize
    # concurrent requests.  Must come before any config reads in the cold path.
    try:
        _current_mtime = Path(_config_module._get_config_path()).stat().st_mtime
    except OSError:
        _current_mtime = 0.0
    _cfg_changed = _current_mtime != _config_module._cfg_mtime

    # Disk load BEFORE lock: ~0.1ms, lets concurrent requests skip entirely.
    # Then acquire lock and check memory cache.  Cold path runs inside the lock
    # so only one thread rebuilds while others wait.
    disk_groups = None
    stale_disk_groups = None
    if MODEL_CATALOG_STATE.available_models_cache is None and not force_refresh:
        disk_groups = _config_module._load_models_cache_from_disk()
        if disk_groups is None:
            stale_disk_groups = _config_module._load_stale_models_cache_from_disk()
    elif force_refresh:
        stale_disk_groups = _config_module._load_stale_models_cache_from_disk()

    with MODEL_CATALOG_STATE.available_models_cache_lock:
        # If another thread is already building, wait for its result instead
        # of re-entering the cold path (avoids duplicate 10s zai load_pool calls).
        if should_wait:
            wait_timeout = 60.0
            if force_refresh and force_refresh_started_at is not None:
                if MODEL_CATALOG_STATE.live_rebuild_budget_seconds <= 0:
                    # The legacy synchronous path is explicitly unbounded. A
                    # forced refresh follower should keep coalescing behind
                    # that live rebuild instead of giving up after 60s and
                    # duplicating it.
                    wait_timeout = None
                else:
                    wait_timeout = max(
                        0.0,
                        MODEL_CATALOG_STATE.live_rebuild_budget_seconds - (time.monotonic() - force_refresh_started_at),
                    )
            MODEL_CATALOG_STATE.cache_build_cv.wait_for(
                lambda: not MODEL_CATALOG_STATE.cache_build_in_progress,
                timeout=wait_timeout
            )
            cached = _config_module._get_fresh_memory_models_cache(time.monotonic())
            if (
                cached is not None
                and (
                    not force_refresh
                    or (
                        force_refresh_started_at is not None
                        and MODEL_CATALOG_STATE.available_models_live_rebuild_ts >= force_refresh_started_at
                    )
                )
            ):
                return cached
            if force_refresh and MODEL_CATALOG_STATE.live_rebuild_budget_seconds > 0 and MODEL_CATALOG_STATE.cache_build_in_progress:
                if stale_disk_groups is not None:
                    return copy.deepcopy(stale_disk_groups)
                return copy.deepcopy(_static_models_catalog_without_live_probes())

        # Reload config if changed
        if _cfg_changed:
            _config_module.reload_config()
            MODEL_CATALOG_STATE.available_models_cache = None
            MODEL_CATALOG_STATE.available_models_cache_ts = 0.0
            MODEL_CATALOG_STATE.available_models_live_rebuild_ts = 0.0
            MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = None
            _sync_models_cache_provenance()
            _invalidate_models_build_locked()
            disk_groups = None
            stale_disk_groups = None

        # Serve from memory cache if fresh
        now = time.monotonic()
        cached = _config_module._get_fresh_memory_models_cache(now)
        if cached is not None:
            if not force_refresh:
                return cached
            if (
                force_refresh_started_at is not None
                and MODEL_CATALOG_STATE.available_models_live_rebuild_ts >= force_refresh_started_at
            ):
                return cached

        # A concurrent forced refresh may have started after this caller sampled
        # should_wait but before it acquired the lock. Reuse that in-flight build
        # instead of launching another one, and preserve this caller's budget.
        if (
            force_refresh
            and force_refresh_started_at is not None
            and MODEL_CATALOG_STATE.cache_build_in_progress
        ):
            remaining_budget = None
            if MODEL_CATALOG_STATE.live_rebuild_budget_seconds > 0:
                remaining_budget = max(
                    0.0,
                    MODEL_CATALOG_STATE.live_rebuild_budget_seconds - (time.monotonic() - force_refresh_started_at),
                )
            if remaining_budget is None or remaining_budget > 0:
                MODEL_CATALOG_STATE.cache_build_cv.wait_for(
                    lambda: not MODEL_CATALOG_STATE.cache_build_in_progress,
                    timeout=remaining_budget,
                )
                cached = _config_module._get_fresh_memory_models_cache(time.monotonic())
                if (
                    cached is not None
                    and MODEL_CATALOG_STATE.available_models_live_rebuild_ts >= force_refresh_started_at
                ):
                    return cached
            if MODEL_CATALOG_STATE.cache_build_in_progress and MODEL_CATALOG_STATE.live_rebuild_budget_seconds > 0:
                if stale_disk_groups is not None:
                    return copy.deepcopy(stale_disk_groups)
                return copy.deepcopy(_static_models_catalog_without_live_probes())

        # Cold path: disk cache hit — use it (fast, no lock contention)
        if disk_groups is not None and not force_refresh:
            MODEL_CATALOG_STATE.available_models_cache = disk_groups
            MODEL_CATALOG_STATE.available_models_cache_ts = now
            MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = _config_module._models_cache_source_fingerprint()
            _sync_models_cache_provenance()
            return copy.deepcopy(disk_groups)

        # ── prefer_cache: NEVER run the live provider rebuild ────────────────
        # Server-initiated wakeup turns (Option Z) reach here with a cold
        # cache (the drain thread fires while idle; the catalog warmed by a
        # human's /api/models has expired or was never built). The live
        # rebuild does a Copilot token-exchange HTTPS call per the proven
        # thread-stack; on this WSL/corp network it stalls the wakeup
        # chat/start indefinitely. A wakeup turn does NOT need the full live
        # catalog — _resolve_compatible_session_model_state only needs
        # default_model/active_provider and trusts the persisted session
        # model. Serve a network-free minimal catalog instead and let a later
        # human request do the real live rebuild.
        if prefer_cache:
            # NOTE (Greptile P1): do NOT touch MODEL_CATALOG_STATE.cache_build_in_progress here.
            # This branch never set the flag (only the cold path below does),
            # and `should_wait` is sampled outside the lock (line ~4964). A
            # concurrent cold-path caller can flip the flag to True after our
            # sample but before we acquire the lock; clearing it here would
            # prematurely release that rebuild's serialization, waking waiters
            # to an empty cache and triggering a second live rebuild. Just
            # serve the network-free minimal catalog and leave the flag alone.
            return copy.deepcopy(_minimal_static_models_catalog())

        # Cold path: full rebuild — only one thread reaches here at a time
        with MODEL_CATALOG_STATE.cache_build_cv:
            MODEL_CATALOG_STATE.models_cache_build_generation += 1
            build_generation = MODEL_CATALOG_STATE.models_cache_build_generation
            MODEL_CATALOG_STATE.active_models_cache_build_generation = build_generation
            MODEL_CATALOG_STATE.cache_build_in_progress = True
            publish_legacy_model_catalog_state(_config_module)

        # Capture the active per-request profile (#3957). The live provider
        # probe inside the rebuild resolves credentials from os.environ /
        # HERMES_HOME and the disk-cache path/fingerprint from the profile TLS;
        # the detached worker thread below inherits NEITHER, so it must be
        # captured here (on the request thread, where the TLS is valid) and
        # re-bound on the worker. Empty / default for single-profile installs.
        from api.config.hooks import get_config_runtime_hooks

        _runtime_hooks = get_config_runtime_hooks()
        _active_profile_name = (_runtime_hooks.active_profile_name() or "").strip()

        # Legacy synchronous (unbounded) rebuild — opt-in via budget<=0.
        if MODEL_CATALOG_STATE.live_rebuild_budget_seconds <= 0:
            try:
                # Foreground thread already carries the request-profile TLS;
                # apply the mirrored profile env (no-op for default) for the
                # live probe because provider_model_ids() still has raw
                # os.getenv()/HERMES_HOME readers on this synchronous path.
                _sync_scope = _runtime_hooks.active_profile_scope(
                    "models rebuild (sync)"
                )
                with _sync_scope:
                    result = _config_module._invoke_models_rebuild(
                        _build_available_models_uncached
                    )
            except BaseException:
                # Always reset the flag so waiting threads don't block for 60s
                with MODEL_CATALOG_STATE.cache_build_cv:
                    if MODEL_CATALOG_STATE.active_models_cache_build_generation == build_generation:
                        MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                        MODEL_CATALOG_STATE.cache_build_in_progress = False
                        MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                        publish_legacy_model_catalog_state(_config_module)
                raise
            with MODEL_CATALOG_STATE.cache_build_cv:
                if MODEL_CATALOG_STATE.active_models_cache_build_generation == build_generation:
                    published_at = time.monotonic()
                    MODEL_CATALOG_STATE.available_models_cache = result
                    MODEL_CATALOG_STATE.available_models_cache_ts = published_at
                    MODEL_CATALOG_STATE.available_models_live_rebuild_ts = published_at
                    MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = (
                        _config_module._models_cache_source_fingerprint()
                    )
                    _sync_models_cache_provenance()
                    try:
                        _config_module._save_models_cache_to_disk(result)
                    finally:
                        if MODEL_CATALOG_STATE.active_models_cache_build_generation == build_generation:
                            MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                            MODEL_CATALOG_STATE.cache_build_in_progress = False
                            MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                publish_legacy_model_catalog_state(_config_module)
            return copy.deepcopy(result)

        # ── Bounded rebuild (defense-in-depth) ───────────────────────────────
        # The live rebuild does a network probe per provider (Copilot token
        # exchange over HTTPS, OpenRouter/Nous /models, ...). On a flaky / corp
        # / WSL network any single probe can stall for its full per-call
        # timeout and, summed across providers, pin a foreground request
        # thread for tens of seconds (the wakeup-turn / chat-start hang).
        #
        # Run the rebuild on a daemon worker; the foreground waits at most
        # MODEL_CATALOG_STATE.live_rebuild_budget_seconds.
        #
        # WITHIN budget (the normal fast case): the FOREGROUND publishes the
        # result synchronously and only then returns — preserving the exact
        # pre-existing contract (cache + on-disk file populated by the time
        # get_available_models() returns). The worker stays hands-off.
        #
        # OVER budget (a provider probe is slow/hung): the foreground returns
        # the best fallback immediately and the still-running worker publishes
        # its result out-of-band when it finally finishes, so the next caller
        # gets a warm cache instead of paying the cold rebuild again.
        #
        # ``_publish_models_result`` / ``box["published"]`` ensure exactly one
        # publisher even at the budget boundary (no double write, no lost
        # refresh). The worker only touches MODEL_CATALOG_STATE.cache_build_cv after the
        # foreground releases the RLock by returning, so no lock inversion.
        build_done = threading.Event()
        budget_exceeded = threading.Event()
        publish_lock = threading.Lock()
        box: dict = {}

        def _publish_models_result(result):
            with MODEL_CATALOG_STATE.cache_build_cv:
                if MODEL_CATALOG_STATE.active_models_cache_build_generation != build_generation:
                    return
                published_at = time.monotonic()
                MODEL_CATALOG_STATE.available_models_cache = result
                MODEL_CATALOG_STATE.available_models_cache_ts = published_at
                MODEL_CATALOG_STATE.available_models_live_rebuild_ts = published_at
                MODEL_CATALOG_STATE.available_models_cache_source_fingerprint = (
                    _config_module._models_cache_source_fingerprint()
                )
                _sync_models_cache_provenance()
                try:
                    _config_module._save_models_cache_to_disk(result)
                except Exception:
                    logger.debug("models cache disk save failed", exc_info=True)
                finally:
                    MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                    MODEL_CATALOG_STATE.cache_build_in_progress = False
                    MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                    publish_legacy_model_catalog_state(_config_module)

        def _clear_build_in_progress():
            with MODEL_CATALOG_STATE.cache_build_cv:
                if MODEL_CATALOG_STATE.active_models_cache_build_generation != build_generation:
                    return
                MODEL_CATALOG_STATE.active_models_cache_build_generation = None
                MODEL_CATALOG_STATE.cache_build_in_progress = False
                MODEL_CATALOG_STATE.cache_build_cv.notify_all()
                publish_legacy_model_catalog_state(_config_module)

        def _claim_publish() -> bool:
            """Return True iff the caller won the right to publish."""
            with publish_lock:
                if box.get("published"):
                    return False
                box["published"] = True
                return True

        def _rebuild_worker():
            # Re-bind the captured per-request profile on THIS worker thread
            # (#3957): the daemon inherits neither the request-profile TLS nor
            # os.environ, so without this it would probe the default profile's
            # credentials and, over budget, publish the rebuilt catalog to the
            # DEFAULT profile's disk cache. No-op for the default profile.
            _worker_scope = _runtime_hooks.detached_profile_scope(
                _active_profile_name, "models rebuild (worker)"
            )
            with _worker_scope:
                try:
                    box["result"] = _config_module._invoke_models_rebuild(
                        _build_available_models_uncached
                    )
                except Exception as exc:  # noqa: BLE001 — propagated to caller
                    box["error"] = exc
                finally:
                    build_done.set()
                    # Only publish out-of-band if the foreground already gave up
                    # (over budget). Within budget the foreground publishes
                    # synchronously, so the worker must NOT touch the cache.
                    # NOTE: the publish (and its disk write + fingerprint) runs
                    # INSIDE this profile scope so the over-budget path writes
                    # the correct profile's cache file.
                    if budget_exceeded.is_set() and _claim_publish():
                        if "result" in box:
                            _publish_models_result(box["result"])
                        else:
                            _clear_build_in_progress()

        _worker = threading.Thread(
            target=_rebuild_worker,
            name="models-catalog-rebuild",
            daemon=True,
        )
        _worker.start()

        if build_done.wait(timeout=MODEL_CATALOG_STATE.live_rebuild_budget_seconds):
            # Build finished within budget — foreground publishes
            # synchronously, exactly like the legacy path.
            if "error" in box:
                _clear_build_in_progress()
                raise box["error"]
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Budget elapsed. Mark it so the worker knows it owns out-of-band
        # publication. Handle the tiny race where the build completed between
        # wait() returning False and here: if so, still publish synchronously
        # so this caller honours the cache contract.
        budget_exceeded.set()
        if build_done.is_set() and "error" not in box and "result" in box:
            if _claim_publish():
                _publish_models_result(box["result"])
            return copy.deepcopy(box["result"])

        # Genuinely slow/hung probe: serve the best fallback now; the worker
        # keeps going and refreshes the cache for the next caller.
        # Rate-limit the warning per Q-2979-A3 — see _should_warn_budget; a
        # sustained budget breach demotes to info after the first emit in
        # each cooldown window so log volume stays bounded.
        _budget_log_msg = (
            "live provider-catalog rebuild exceeded %.1fs budget — serving "
            "fallback, refreshing catalog out-of-band"
        )
        if _should_warn_budget("live_rebuild_budget_exceeded"):
            logger.warning(_budget_log_msg, MODEL_CATALOG_STATE.live_rebuild_budget_seconds)
        else:
            logger.info(_budget_log_msg, MODEL_CATALOG_STATE.live_rebuild_budget_seconds)
        # ``stale_disk_groups`` is shape-valid but failed the strict metadata
        # checks required for authoritative cold-path use. It was read before
        # acquiring MODEL_CATALOG_STATE.available_models_cache_lock so this over-budget fallback
        # does not extend the lock hold while the worker is ready to publish.
        if stale_disk_groups is not None:
            return copy.deepcopy(stale_disk_groups)
        return copy.deepcopy(_static_models_catalog_without_live_probes())

__config_exports__ = (
    "_invalidate_models_build_locked",
    "_sync_models_cache_provenance",
    "_endpoint_advertised_model_ids",
    "_should_warn_budget",
    "_invoke_models_rebuild",
    "_configured_model_badges_from_static_catalog",
    "_minimal_static_models_catalog",
    "_static_models_catalog_without_live_probes",
    "_credential_pool_profile_tag",
    "_pool_entry_payloads",
    "_has_explicit_pool_credentials",
    "_current_webui_version",
    "_get_label_for_model",
    "_read_live_provider_model_ids",
    "_models_from_live_provider_ids",
    "_moa_preset_models_from_config",
    "_read_visible_codex_cache_model_ids",
    "get_available_models",
)

__all__ = __config_exports__
