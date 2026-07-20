"""Compatibility interface for provider account usage and quota reporting.

Provider dispatch, safe projections, profile environment construction, and
probe-process lifecycle have separate owners. This module keeps the historical
``api.providers`` import surface and its deliberate high-level monkeypatch seams.
"""

# ruff: noqa: F401 -- named compatibility exports are consumed by api.providers

from __future__ import annotations

import urllib  # compatibility: tests and callers patch urllib.request globally
from pathlib import Path
from typing import Any

from api.config import PROVIDER_DISPLAY as _PROVIDER_DISPLAY, get_config
from api.providers.account_usage_environment import (
    _account_usage_preexec_fn,
    _account_usage_subprocess_env,
)
from api.providers.account_usage_runtime import (
    _ACCOUNT_USAGE_CACHE_MAX_ENTRIES,
    _ACCOUNT_USAGE_CACHE_TTL_SECONDS,
    _ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS,
    _ACCOUNT_USAGE_WORKERS_PER_HOME,
    _ACCOUNT_USAGE_WORKER_IDLE_SECONDS,
    _MAX_CONCURRENT_ACCOUNT_USAGE_PROBES,
    _AccountUsageProbeWorker,
    _account_usage_cache_key,
    _account_usage_probe_semaphore,
    _account_usage_status_cache,
    _account_usage_status_cache_lock,
    _account_usage_worker_pool,
    _account_usage_worker_pool_lock,
    _agent_fetch_account_usage,
    _agent_fetch_account_usage_for_home,
    _cleanup_account_usage_probe_workers,
    _close_account_usage_probe_worker_list,
    _close_account_usage_probe_workers,
    _fetch_account_usage_once_for_home,
    _get_account_usage_probe_semaphore,
    _get_account_usage_probe_worker,
    _get_cached_account_usage,
    _launch_account_usage_worker_process,
    _set_cached_account_usage,
    fetch_account_usage_with_profile_context,
)
from api.providers import account_usage_runtime as _runtime
from api.providers.credentials import (
    _get_provider_api_key,
    _local_pool_snapshot,
)
from api.providers.quota_status import (
    _ACCOUNT_USAGE_PROVIDERS,
    _OPENROUTER_KEY_URL,
    _PROVIDER_QUOTA_TIMEOUT_SECONDS,
    get_provider_quota_status,
    provider_account_usage_status,
)
from api.providers.usage_projection import (
    _account_usage_payload_to_snapshot,
    _isoformat_utc,
    _quota_number,
    _sanitize_openrouter_quota,
    _serialize_account_usage_snapshot,
)


def _get_hermes_home() -> Path:
    from api import profiles

    return profiles.get_active_hermes_home()


def _active_provider_id() -> str | None:
    model_cfg = get_config().get("model", {})
    if not isinstance(model_cfg, dict):
        return None
    provider = str(model_cfg.get("provider") or "").strip().lower()
    return provider or None


def _fetch_account_usage_with_profile_context(
    provider: str,
    *,
    refresh: bool = False,
) -> Any:
    """Compatibility seam around the profile-scoped runtime owner."""
    return fetch_account_usage_with_profile_context(
        provider,
        refresh=refresh,
        home_resolver=_get_hermes_home,
        key_resolver=_get_provider_api_key,
        fetcher=_agent_fetch_account_usage_for_home,
    )


def _provider_account_usage_status(
    provider: str,
    display_name: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    return provider_account_usage_status(
        provider,
        display_name,
        refresh=refresh,
        fetch_usage=_fetch_account_usage_with_profile_context,
    )


def get_provider_quota(
    provider_id: str | None = None,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return sanitized quota/rate-limit status for one normalized provider."""
    provider = (provider_id or _active_provider_id() or "").strip().lower()
    if not provider:
        return {
            "ok": False,
            "provider": None,
            "display_name": None,
            "supported": False,
            "status": "unavailable",
            "quota": None,
            "message": "No active provider is configured.",
        }
    display_name = _PROVIDER_DISPLAY.get(provider, provider.replace("-", " ").title())
    return get_provider_quota_status(
        provider,
        refresh=refresh,
        display_name=display_name,
        get_api_key=_get_provider_api_key,
        get_account_status=_provider_account_usage_status,
        get_local_pool_snapshot=_local_pool_snapshot,
    )


def _close_account_usage_probe_workers_async(
    *,
    provider_id: str | None = None,
) -> None:
    _runtime._close_account_usage_probe_workers_async(
        provider_id=provider_id,
        active_home=_get_hermes_home() if provider_id else None,
    )


def invalidate_account_usage_status_cache(provider_id: str | None = None) -> None:
    _runtime.invalidate_account_usage_status_cache(
        provider_id,
        active_home=_get_hermes_home() if provider_id else None,
    )


__provider_exports__ = (
    "_OPENROUTER_KEY_URL",
    "_PROVIDER_QUOTA_TIMEOUT_SECONDS",
    "_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS",
    "_ACCOUNT_USAGE_CACHE_TTL_SECONDS",
    "_ACCOUNT_USAGE_CACHE_MAX_ENTRIES",
    "_ACCOUNT_USAGE_WORKER_IDLE_SECONDS",
    "_ACCOUNT_USAGE_PROVIDERS",
    "_MAX_CONCURRENT_ACCOUNT_USAGE_PROBES",
    "_account_usage_probe_semaphore",
    "_account_usage_status_cache",
    "_account_usage_status_cache_lock",
    "_account_usage_worker_pool",
    "_account_usage_worker_pool_lock",
    "_ACCOUNT_USAGE_WORKERS_PER_HOME",
    "_get_account_usage_probe_semaphore",
    "_account_usage_preexec_fn",
    "_active_provider_id",
    "_quota_number",
    "_sanitize_openrouter_quota",
    "_isoformat_utc",
    "_serialize_account_usage_snapshot",
    "_agent_fetch_account_usage",
    "_account_usage_subprocess_env",
    "_account_usage_payload_to_snapshot",
    "_AccountUsageProbeWorker",
    "_launch_account_usage_worker_process",
    "_fetch_account_usage_once_for_home",
    "_get_account_usage_probe_worker",
    "_cleanup_account_usage_probe_workers",
    "_close_account_usage_probe_workers",
    "_close_account_usage_probe_worker_list",
    "_close_account_usage_probe_workers_async",
    "_account_usage_cache_key",
    "_get_cached_account_usage",
    "invalidate_account_usage_status_cache",
    "_set_cached_account_usage",
    "_agent_fetch_account_usage_for_home",
    "_fetch_account_usage_with_profile_context",
    "_provider_account_usage_status",
    "get_provider_quota",
)

__all__ = __provider_exports__
