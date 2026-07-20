"""Network-free model catalog construction from local configuration and auth."""

from __future__ import annotations

import copy
import json
import logging

from api import config as _config_module
from api.config.catalog_normalization import normalize_catalog_model_id
from api.config.catalog_sources import _get_label_for_model

logger = logging.getLogger(__name__)

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

    norm_lookup: dict[str, list[str]] = {}
    for opt_id in option_ids:
        norm_lookup.setdefault(normalize_catalog_model_id(opt_id), []).append(opt_id)

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
                normalized = normalize_catalog_model_id(candidate)
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

__all__ = (
    "_configured_model_badges_from_static_catalog",
    "_minimal_static_models_catalog",
    "_static_models_catalog_without_live_probes",
)
