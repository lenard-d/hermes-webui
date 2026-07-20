"""Provider-specific quota status adapters and response contracts."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from api.providers.usage_projection import (
    _sanitize_openrouter_quota,
    _serialize_account_usage_snapshot,
)

_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_PROVIDER_QUOTA_TIMEOUT_SECONDS = 3.0
_ACCOUNT_USAGE_PROVIDERS = frozenset({"openai-codex", "anthropic"})


def provider_account_usage_status(
    provider: str,
    display_name: str,
    *,
    refresh: bool,
    fetch_usage: Callable[..., Any],
) -> dict[str, Any]:
    snapshot = fetch_usage(provider, refresh=refresh)
    account_limits = _serialize_account_usage_snapshot(snapshot)
    if account_limits and account_limits.get("available"):
        return {
            "ok": True,
            "provider": provider,
            "display_name": display_name,
            "supported": True,
            "status": "available",
            "label": account_limits.get("title") or "Account limits",
            "quota": None,
            "account_limits": account_limits,
            "message": f"{display_name} account limits loaded.",
        }
    reason = ""
    if account_limits:
        reason = str(account_limits.get("unavailable_reason") or "").strip()
    message = (
        f"{display_name} account limits are unavailable. {reason}"
        if reason
        else f"{display_name} account limits are unavailable. "
        "Confirm provider authentication and try again."
    )
    return {
        "ok": False,
        "provider": provider,
        "display_name": display_name,
        "supported": True,
        "status": "unavailable",
        "quota": None,
        "account_limits": account_limits,
        "message": message,
    }


def _openrouter_status(
    display_name: str,
    *,
    api_key: str | None,
) -> dict[str, Any]:
    if not api_key:
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "no_key",
            "quota": None,
            "message": (
                "OpenRouter quota status needs an OPENROUTER_API_KEY "
                "configured on the server."
            ),
        }
    request = urllib.request.Request(
        _OPENROUTER_KEY_URL,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=_PROVIDER_QUOTA_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
        payload = json.loads(
            raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        )
        return {
            "ok": True,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "available",
            "label": "OpenRouter credits",
            "quota": _sanitize_openrouter_quota(payload),
            "message": "OpenRouter quota status loaded.",
        }
    except urllib.error.HTTPError as exc:
        status = "invalid_key" if exc.code in (401, 403) else "unavailable"
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": status,
            "quota": None,
            "message": (
                "OpenRouter rejected the configured API key."
                if status == "invalid_key"
                else "OpenRouter quota status is temporarily unavailable."
            ),
        }
    except (
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ):
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "unavailable",
            "quota": None,
            "message": "OpenRouter quota status is temporarily unavailable.",
        }


def get_provider_quota_status(
    provider: str,
    *,
    refresh: bool,
    display_name: str,
    get_api_key: Callable[[str], str | None],
    get_account_status: Callable[..., dict[str, Any]],
    get_local_pool_snapshot: Callable[[str], Any],
) -> dict[str, Any]:
    """Dispatch a normalized provider id to its quota/status adapter."""
    if provider in _ACCOUNT_USAGE_PROVIDERS:
        return get_account_status(provider, display_name, refresh=refresh)
    if provider == "openrouter":
        return _openrouter_status(display_name, api_key=get_api_key("openrouter"))

    local_snapshot = get_local_pool_snapshot(provider)
    if local_snapshot is not None:
        account_limits = _serialize_account_usage_snapshot(local_snapshot)
        if account_limits and account_limits.get("available"):
            return {
                "ok": True,
                "provider": provider,
                "display_name": display_name,
                "supported": True,
                "status": "available",
                "label": account_limits.get("title") or "Credential pool",
                "quota": None,
                "account_limits": account_limits,
                "message": f"{display_name} credential pool status loaded.",
            }
        return {
            "ok": False,
            "provider": provider,
            "display_name": display_name,
            "supported": True,
            "status": "unavailable",
            "quota": None,
            "account_limits": account_limits,
            "message": f"{display_name} credential pool: all credentials are unavailable.",
        }

    detail = (
        "OpenAI/Anthropic rate-limit headers are a follow-up once WebUI captures "
        "provider response metadata."
    )
    return {
        "ok": False,
        "provider": provider,
        "display_name": display_name,
        "supported": False,
        "status": "unsupported",
        "quota": None,
        "message": f"Quota status is not available for {display_name}. {detail}",
    }


__all__ = (
    "_OPENROUTER_KEY_URL",
    "_PROVIDER_QUOTA_TIMEOUT_SECONDS",
    "_ACCOUNT_USAGE_PROVIDERS",
    "provider_account_usage_status",
    "get_provider_quota_status",
)
