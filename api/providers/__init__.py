"""Hermes Web UI -- provider management endpoints.

Provides CRUD operations for configuring provider API keys post-onboarding.
Closes #586 (allow provider key update) and part of #604 (model picker
multi-provider support).
"""

# The entrypoint intentionally re-exports the historical provider API from its
# credential, usage, and cost-history owners.
# ruff: noqa: F401, F405

from __future__ import annotations

import atexit
import base64  # noqa: F401 -- credential-store facade dependency
import copy
import hashlib
import json
import logging
import os  # noqa: F401 -- account-usage and credential-store facade dependency
import signal  # noqa: F401 -- account-usage facade dependency
import subprocess  # noqa: F401 -- account-usage facade dependency
import sys  # noqa: F401 -- account-usage facade dependency
import threading
import time
import urllib.error
import urllib.request  # noqa: F401 -- account-usage and cost-history facade dependency
from contextlib import contextmanager, nullcontext  # noqa: F401 -- provider-part facade dependencies
from datetime import datetime, timedelta, timezone  # noqa: F401 -- credential-store facade dependency
from pathlib import Path
from types import SimpleNamespace  # noqa: F401 -- account-usage and credential-store facade dependency
from typing import TYPE_CHECKING, Any

try:  # POSIX-only; Windows-style environments fall back to process-local locking.
    import fcntl
except ImportError:  # pragma: no cover - exercised only where fcntl is unavailable
    fcntl = None  # type: ignore[assignment]

from api.config import (
    PROVIDER_DISPLAY as _PROVIDER_DISPLAY,
    PROVIDER_MODELS as _PROVIDER_MODELS,
    build_nous_featured_models as _build_nous_featured_set,
    coerce_provider_cost_budget as _coerce_provider_cost_budget,
    configured_provider_base_url as _get_provider_base_url,
    credential_pool_entries as _pool_entry_payloads,
    custom_provider_slug_from_name as _custom_provider_slug_from_name,
    effective_provider_display_name,
    effective_provider_env_var,  # noqa: F401 -- credential-store facade dependency
    get_config,
    invalidate_models_cache,
    is_plugin_model_provider,
    live_provider_model_ids as _read_live_provider_model_ids,
    model_label as _get_label_for_model,  # noqa: F401 -- compatibility re-export
    models_from_live_provider_ids as _models_from_live_provider_ids,
    plugin_model_provider_ids,
    profile_env_value as _thread_local_env_value,
    provider_has_explicit_pool_credentials as _has_explicit_pool_credentials,
    format_nous_model_label as _format_nous_label,
    install_config_runtime_hooks,
    visible_codex_cache_model_ids as _read_visible_codex_cache_model_ids,
)
from api.providers.account_usage import *  # noqa: F403 - compatibility exports
from api.providers.cost_history import *  # noqa: F403 - compatibility exports
from api.providers.credentials import *  # noqa: F403 - compatibility exports

write_env_file = _write_env_file  # noqa: F405 - public credential-persistence owner

logger = logging.getLogger(__name__)


atexit.register(_close_account_usage_probe_workers)  # noqa: F405

install_config_runtime_hooks(
    provider_has_credential=_provider_has_key,  # noqa: F405
    credential_cache_invalidated=invalidate_account_usage_status_cache,  # noqa: F405
)

_PROVIDERS_CACHE_TTL_SECONDS = 30.0
_providers_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_providers_cache_lock = threading.Lock()




def _get_hermes_home() -> Path:
    """Return the active Hermes home directory."""
    try:
        from api.profiles import get_active_hermes_home
        return get_active_hermes_home()
    except ImportError:
        return Path.home() / ".hermes"


def _providers_file_mtime_ns(path: Path) -> int:
    """Best-effort file mtime for providers-cache invalidation."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _providers_config_fingerprint(cfg: Any) -> str:
    """Stable fingerprint for config fields that shape the Providers response."""
    try:
        return hashlib.sha256(
            json.dumps(cfg, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    except Exception:
        return repr(cfg)


def _providers_cache_key(cfg: Any) -> tuple[Any, ...]:
    """Return a profile-scoped cache key for ``get_providers()`` (#6010).

    The endpoint reads provider state from the active Hermes home plus the
    current config.  Include the home path and the two files users commonly
    mutate from Settings so a short TTL never crosses profile boundaries or
    masks immediate credential/config changes.
    """
    home = _get_hermes_home()
    try:
        home_key = str(home.resolve())
    except OSError:
        home_key = str(home)
    return (
        home_key,
        _providers_file_mtime_ns(home / ".env"),
        _providers_file_mtime_ns(home / "config.yaml"),
        _providers_config_fingerprint(cfg),
    )


def _get_cached_providers(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _providers_cache_lock:
        cached = _providers_cache.get(cache_key)
        if cached is None:
            return None
        ts, payload = cached
        if now - ts >= _PROVIDERS_CACHE_TTL_SECONDS:
            _providers_cache.pop(cache_key, None)
            return None
        return copy.deepcopy(payload)


def _store_cached_providers(cache_key: tuple[Any, ...], payload: dict[str, Any]) -> dict[str, Any]:
    with _providers_cache_lock:
        # Single-entry by design: /api/providers is cacheable only for the
        # active profile/config snapshot, so clear older snapshots to avoid
        # retaining unbounded provider metadata across profile switches.
        _providers_cache.clear()
        _providers_cache[cache_key] = (time.monotonic(), copy.deepcopy(payload))
    return payload


def invalidate_providers_cache() -> None:
    """Clear cached ``GET /api/providers`` responses."""
    with _providers_cache_lock:
        _providers_cache.clear()










# SECTION: Public API


def get_providers() -> dict[str, Any]:
    """Return a list of all known providers with their configuration status.

    Each entry contains:
    - ``id``: canonical provider slug
    - ``display_name``: human-readable name
    - ``has_key``: whether an API key is configured
    - ``configurable``: whether the key can be set from the WebUI
    - ``key_source``: where the key was found (``env_file``, ``env_var``,
      ``config_yaml``, ``oauth``, ``none``)
    - ``models``: list of known model IDs for this provider
    """
    # Collect all known provider IDs from multiple sources
    known_ids = set(_PROVIDER_DISPLAY.keys()) | set(_PROVIDER_MODELS.keys())
    known_ids.update(plugin_model_provider_ids())

    # Also detect providers from config.yaml providers section
    cfg = get_config()
    cache_key = _providers_cache_key(cfg)
    cached = _get_cached_providers(cache_key)
    if cached is not None:
        return cached

    providers = []
    providers_cfg = cfg.get("providers") or {}
    if isinstance(providers_cfg, dict):
        known_ids.update(providers_cfg.keys())

    # Add OAuth providers even if not in _PROVIDER_DISPLAY
    known_ids.update(_OAUTH_PROVIDERS)

    for pid in sorted(known_ids):
        display_name = effective_provider_display_name(pid, _PROVIDER_DISPLAY)
        is_oauth = _provider_is_oauth(pid)
        has_key = _provider_has_key(pid)
        plugin_auth_status: dict[str, Any] | None = None
        if not has_key and is_plugin_model_provider(pid):
            try:
                from hermes_cli.auth import get_auth_status as _gas_plugin
                _plugin_status = _gas_plugin(pid)
                if isinstance(_plugin_status, dict) and (
                    _plugin_status.get("logged_in") or _plugin_status.get("configured")
                ):
                    has_key = True
                    plugin_auth_status = _plugin_status
            except Exception:
                logger.debug("Plugin provider auth check failed for %s", pid, exc_info=True)

        # Determine key source
        key_source = "none"
        auth_error = None
        if is_oauth:
            key_source = "oauth"
            # Check if actually authenticated via hermes_cli.
            # IMPORTANT: do not unconditionally overwrite has_key from _provider_has_key().
            # A token in config.yaml is a valid credential even when get_auth_status()
            # returns logged_in=False (e.g. token not in the hermes credential pool,
            # or refresh token consumed by native Codex CLI / VS Code extension).
            try:
                from hermes_cli.auth import get_auth_status as _gas
                status = _gas(pid)
                if isinstance(status, dict) and status.get("logged_in"):
                    has_key = True
                    key_source = status.get("key_source", "oauth")
                elif has_key:
                    # _provider_has_key() found a token in config.yaml — respect it
                    # rather than hiding a working credential from the Settings UI.
                    key_source = "config_yaml"
                    auth_error = status.get("error") if isinstance(status, dict) else None
                else:
                    has_key = False
                    auth_error = status.get("error") if isinstance(status, dict) else None
            except Exception:
                # Import failed or auth check errored — don't override a known-good
                # key just because the hermes_cli auth module is unavailable.
                logger.debug("hermes_cli auth check failed for %s", pid, exc_info=True)
                # keep has_key from _provider_has_key()
        elif has_key:
            env_var = _provider_env_var_for(pid)
            if env_var:
                env_path = _get_hermes_home() / ".env"
                env_values = _load_env_file(env_path)
                if _provider_value_counts_as_api_key(pid, env_values.get(env_var)):
                    key_source = "env_file"
                elif _provider_value_counts_as_api_key(pid, _thread_local_env_value(env_var)):
                    key_source = "env_var"
                else:
                    # Canonical name not set; check legacy aliases (e.g. lmstudio's
                    # pre-#1500 LMSTUDIO_API_KEY) so existing users see "env_file"
                    # instead of being misreported as "config_yaml" when the key
                    # actually lives in .env under the old name.
                    aliased = False
                    for alias in _PROVIDER_ENV_VAR_ALIASES.get(pid, ()) or ():
                        if _provider_value_counts_as_api_key(pid, env_values.get(alias)):
                            key_source = "env_file"
                            aliased = True
                            break
                        if _provider_value_counts_as_api_key(pid, _thread_local_env_value(alias)):
                            key_source = "env_var"
                            aliased = True
                            break
                    if not aliased:
                        _plugin_ks = (
                            str(plugin_auth_status.get("key_source") or "").strip()
                            if isinstance(plugin_auth_status, dict)
                            else ""
                        )
                        key_source = _plugin_ks or "config_yaml"
            else:
                _plugin_ks = (
                    str(plugin_auth_status.get("key_source") or "").strip()
                    if isinstance(plugin_auth_status, dict)
                    else ""
                )
                key_source = _plugin_ks or "config_yaml"
        elif not _provider_env_var_for(pid):
            # Fallback: provider is not a known API-key provider and not in
            # the hardcoded _OAUTH_PROVIDERS set.  It may be a custom or
            # newly-added OAuth provider (e.g. Anthropic connected via OAuth).
            # Check live auth status so the Providers tab agrees with the
            # model picker (#1212).
            #
            # IMPORTANT: we skip providers with a known API-key env var because
            # they are pure API-key providers — calling get_auth_status() for
            # every unconfigured API-key provider would add unnecessary latency
            # (network round-trip per provider) on the Settings page.
            # Validate pid looks like a real provider before probing
            import re as _re
            if _re.match(r'^[a-z][a-z0-9_-]{0,63}$', pid):
                try:
                    from hermes_cli.auth import get_auth_status as _gas
                    status = _gas(pid)
                    if isinstance(status, dict) and status.get("logged_in"):
                        has_key = True
                        # Constrain key_source to a known-safe closed set
                        _raw_ks = status.get("key_source", "")
                        key_source = _raw_ks if _raw_ks in {"oauth", "env", "config", "token"} else "oauth"
                        is_oauth = True
                except Exception:
                    pass

        if pid == "openai" and not has_key and _provider_has_shadowed_codex_oauth_value(pid):
            continue

        models = list(_PROVIDER_MODELS.get(pid, []))
        models_total = len(models)
        # OpenAI Codex account catalogs drift independently from WebUI releases.
        # The model picker already prefers hermes_cli + Codex local cache for
        # this provider (the agent's `provider_model_ids("openai-codex")` filters
        # IDs with `supported_in_api: false`, but Codex CLI still surfaces some
        # of those — notably `gpt-5.3-codex-spark` from #1680 — in its picker).
        # Merge both sources here so the providers card matches the picker
        # exactly. Static entries remain the offline fallback when live
        # discovery and the local Codex cache are both unavailable. (#1807
        # follow-up to v0.51.19 #1812.)
        if pid == "openai-codex":
            live_ids = _read_live_provider_model_ids("openai-codex")
            live_id_set = set(live_ids)
            for mid in _read_visible_codex_cache_model_ids():
                if mid not in live_id_set:
                    live_id_set.add(mid)
                    live_ids.append(mid)
            live_models = _models_from_live_provider_ids(pid, live_ids)
            if live_models:
                models = live_models
                models_total = len(models)
        if pid == "xai-oauth":
            live_models = _models_from_live_provider_ids(
                pid,
                _read_live_provider_model_ids("xai-oauth"),
            )
            if live_models:
                models = live_models
                models_total = len(models)
        # Nous Portal: prefer the live catalog so the providers card matches
        # the dropdown picker (#1538). Same fallback shape as the static-only
        # case below — when hermes_cli is unavailable or its lookup raises,
        # we keep the four-entry curated list.
        #
        # On large-tier accounts (#1567 reporter Deor saw 396 entries), we
        # render the same featured subset the picker uses so the providers
        # card body doesn't become a 396-pill wall. The full count is still
        # reported via models_total — surfaced in the header line as
        # "396 models · OAuth" by static/panels.js — so the user knows the
        # complete catalog is reachable (via /model autocomplete or a future
        # "show all" disclosure if added).
        if pid == "nous":
            try:
                from hermes_cli.models import provider_model_ids as _provider_model_ids

                live_ids = _provider_model_ids("nous") or []
                if live_ids:
                    # Lazy-import to avoid circular dep with api.config.
                    featured_ids, _extras = _build_nous_featured_set(live_ids)
                    models = [
                        {"id": f"@nous:{mid}", "label": _format_nous_label(mid)}
                        for mid in featured_ids
                    ]
                    models_total = len(live_ids)
            except Exception:
                logger.debug("Failed to load Nous Portal models from hermes_cli")
        # LM Studio: fetch live locally-loaded models so the providers card
        # matches what's actually available on the user's server (#WebUI).
        if pid == "lmstudio":
            try:
                from hermes_cli.models import provider_model_ids as _pmi

                lm_live = _pmi("lmstudio") or []
                if lm_live:
                    models = [{"id": mid, "label": mid} for mid in lm_live]
                    models_total = len(models)
            except Exception:
                logger.debug("Failed to load LM Studio models from hermes_cli")
        if is_plugin_model_provider(pid):
            try:
                live_models = _models_from_live_provider_ids(
                    pid,
                    _read_live_provider_model_ids(pid),
                )
                if live_models:
                    models = live_models
                    models_total = len(models)
            except Exception:
                logger.debug(
                    "Failed to load plugin model-provider catalog for %s",
                    pid,
                    exc_info=True,
                )
        # Also include models from config.yaml providers section
        if isinstance(providers_cfg, dict):
            provider_cfg = providers_cfg.get(pid, {})
            if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                cfg_models = provider_cfg["models"]
                if isinstance(cfg_models, dict):
                    models = models + [{"id": k, "label": k} for k in cfg_models.keys()]
                elif isinstance(cfg_models, list):
                    models = models + [{"id": k, "label": k} for k in cfg_models]
                # Recompute models_total when config.yaml contributes additional
                # entries on top of the live/static catalog. For non-Nous
                # providers models_total still equals len(models); for Nous
                # we keep the live count (which already includes any models
                # surfaced in the curated featured slice).
                if pid != "nous":
                    models_total = len(models)

        is_self_hosted = pid in _SELF_HOSTED_PROVIDER_IDS
        try:
            provider_base_url = _get_provider_base_url(pid) if is_self_hosted else None
        except Exception:
            provider_base_url = None
        _is_plugin = is_plugin_model_provider(pid)
        providers.append({
            "id": pid,
            "display_name": display_name,
            "has_key": has_key,
            "configurable": not is_oauth and bool(_provider_env_var_for(pid)),
            "is_self_hosted": is_self_hosted,
            "base_url": provider_base_url,
            "is_plugin_provider": _is_plugin,
            "is_oauth": is_oauth,
            "key_source": key_source,
            "auth_error": auth_error,
            "models": models,
            # models_total reflects the complete catalog size (e.g. 396 for
            # an enterprise Nous Portal account), even when "models" is
            # trimmed to a featured subset for UI scannability. The frontend
            # uses this for the header text "396 models · OAuth" so users
            # know the full catalog exists and is reachable via the slash
            # command. For providers that don't trim, models_total ==
            # len(models) and the frontend behaves identically to before.
            "models_total": models_total,
        })

    # Scan custom_providers from config.yaml (e.g. glmcode, timicc)
    custom_providers_cfg = cfg.get("custom_providers", [])
    if isinstance(custom_providers_cfg, list):
        for cp in custom_providers_cfg:
            if not isinstance(cp, dict) or not cp.get("name"):
                continue
            cp_name = str(cp["name"]).strip()
            cp_id = _custom_provider_slug_from_name(cp_name)
            if not cp_id:
                logger.warning(
                    "Custom provider entry %r produced empty slug; skipping",
                    cp_name,
                )
                continue
            # Collect models from `models` list or `model` single
            cp_models = []
            if isinstance(cp.get("models"), list):
                cp_models = [{"id": str(m), "label": str(m)} for m in cp["models"]]
            elif cp.get("model"):
                cp_models = [{"id": cp["model"], "label": cp["model"]}]
            # Check for env var reference (${VAR_NAME} pattern)
            cp_api_key = str(cp.get("api_key") or "")
            cp_has_key = bool(cp_api_key.strip())
            # Replace env var reference to check actual value
            if cp_api_key.startswith("${") and cp_api_key.endswith("}"):
                env_var = cp_api_key[2:-1]
                cp_has_key = bool(_thread_local_env_value(env_var).strip())
            # Fallback: check credential pool (key added via hermes auth add)
            if not cp_has_key:
                try:
                    if _has_explicit_pool_credentials(cp_id):
                        cp_has_key = True
                except ImportError:
                    pass
            providers.append({
                "id": cp_id,
                "display_name": cp_name,
                "has_key": cp_has_key,
                "configurable": False,  # custom providers managed via config.yaml
                "is_custom": True,
                "key_source": "config_yaml" if cp_has_key else "none",
                "models": cp_models,
                "models_total": len(cp_models),
            })

    # Determine active provider
    active_provider = None
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        active_provider = model_cfg.get("provider")

    # Sort providers: active first, then custom:*, then has_key, then rest.
    def _provider_sort_key(p):
        pid = p.get("id") or ""
        if pid == active_provider:
            return (0, pid)
        if pid.startswith("custom:"):
            return (1, pid)
        if p.get("has_key"):
            return (2, pid)
        return (3, pid)
    providers.sort(key=_provider_sort_key)

    result = {
        "providers": providers,
        "active_provider": active_provider,
    }
    return _store_cached_providers(cache_key, result)


def set_provider_key(provider_id: str, api_key: str | None) -> dict[str, Any]:
    """Set or update the API key for a provider.

    Writes the key to ``~/.hermes/.env`` using the standard env var name.
    If ``api_key`` is None or empty, the key is removed.

    Returns a status dict with the operation result.
    """
    provider_id = provider_id.strip().lower()

    if not provider_id:
        return {"ok": False, "error": "Provider ID is required."}

    if _provider_is_oauth(provider_id):
        return {
            "ok": False,
            "error": f"'{_PROVIDER_DISPLAY.get(provider_id, provider_id)}' uses OAuth authentication. "
                     f"Use `hermes model` in the terminal to configure it.",
        }

    env_var = _provider_env_var_for(provider_id)
    if not env_var:
        return {
            "ok": False,
            "error": f"Cannot configure API key for '{effective_provider_display_name(provider_id, _PROVIDER_DISPLAY)}'. "
                     f"This provider does not have a known env var mapping.",
        }

    # Validate API key format (basic sanity check)
    if api_key:
        api_key = api_key.strip()
        if "\n" in api_key or "\r" in api_key:
            return {"ok": False, "error": "API key must not contain newline characters."}
        if len(api_key) < 8:
            return {"ok": False, "error": "API key appears too short."}

    env_path = _get_hermes_home() / ".env"
    try:
        _write_env_file(env_path, {env_var: api_key})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Failed to write env file for provider %s", provider_id)
        return {"ok": False, "error": f"Failed to save API key: {exc}"}

    # Invalidate the model cache so the dropdown refreshes on next request.
    # Using invalidate_models_cache() instead of reload_config() to avoid
    # disrupting active streaming sessions that may be reading config.cfg.
    invalidate_models_cache()
    invalidate_account_usage_status_cache(provider_id)
    invalidate_providers_cache()

    return {
        "ok": True,
        "provider": provider_id,
        "display_name": _PROVIDER_DISPLAY.get(provider_id, provider_id),
        "action": "updated" if api_key else "removed",
    }


def remove_provider_key(provider_id: str) -> dict[str, Any]:
    """Remove the API key for a provider.

    Removes the key from ``~/.hermes/.env`` (via ``set_provider_key``)
    and also cleans up ``config.yaml`` if the key is stored there
    (``providers.<id>.api_key`` or top-level ``model.api_key`` when this
    provider is the active one).

    Returns a status dict with the operation result.
    """
    result = set_provider_key(provider_id, None)

    # Even if the .env removal succeeded, the key might also live in
    # config.yaml (e.g. providers.<id>.api_key or model.api_key).
    # Clean those up so _provider_has_key() returns False after removal.
    if result.get("ok"):
        _clean_provider_key_from_config(provider_id)

    return result


def _clean_provider_key_from_config(provider_id: str) -> None:
    """Remove provider API key entries from config.yaml.

    Handles three storage locations:
    1. ``providers.<id>.api_key`` — per-provider key
    2. ``model.api_key`` — top-level key (only if provider is active)
    3. ``custom_providers[].api_key`` — custom provider entries

    Writes back to config.yaml only if something was actually removed. Uses the
    config package's atomic mutation boundary to prevent TOCTOU races.
    """
    try:
        # Resolve through api.config at call time instead of the function imported
        # at module load. Several tests (and some profile flows) monkeypatch the
        # config module's path resolver after api.providers has already been
        # imported; using the stale imported reference can clean the wrong
        # config.yaml.
        import api.config as _config
        config_path = _config._get_config_path()
    except Exception:
        return

    if not config_path.exists():
        return

    try:
        from api.config import update_config

        def remove_keys(cfg):
            changed = False
            if not isinstance(cfg, dict):
                return False

            # 1. Clean providers.<id>.api_key
            providers_cfg = cfg.get("providers") or {}
            if isinstance(providers_cfg, dict):
                provider_cfg = providers_cfg.get(provider_id, {})
                if isinstance(provider_cfg, dict) and provider_cfg.get("api_key"):
                    del provider_cfg["api_key"]
                    changed = True

            # 2. Clean model.api_key — only if this provider is the active one
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("api_key"):
                active_provider = model_cfg.get("provider")
                if active_provider and str(active_provider).strip().lower() == provider_id.lower():
                    del model_cfg["api_key"]
                    changed = True

            # 3. Clean custom_providers[].api_key
            custom_providers = cfg.get("custom_providers", [])
            if isinstance(custom_providers, list):
                for cp in custom_providers:
                    if isinstance(cp, dict):
                        if _custom_provider_name_matches(provider_id, cp.get("name")):
                            if cp.get("api_key"):
                                del cp["api_key"]
                                changed = True

            return changed

        changed = update_config(remove_keys)
        if changed:
            invalidate_providers_cache()
    except Exception:
        logger.exception("Failed to clean provider key from config.yaml for %s", provider_id)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
