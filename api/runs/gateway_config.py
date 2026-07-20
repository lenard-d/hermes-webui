"""Configuration and operator-facing errors for Gateway-backed chat."""

from __future__ import annotations

import os
import urllib.error

from api.config import coerce_reasoning_effort_for_model
from api.helpers import _redact_text


_WEBUI_CHAT_BACKEND_ENV = "HERMES_WEBUI_CHAT_BACKEND"
_WEBUI_GATEWAY_BASE_URL_ENV = "HERMES_WEBUI_GATEWAY_BASE_URL"
_WEBUI_GATEWAY_API_KEY_ENV = "HERMES_WEBUI_GATEWAY_API_KEY"
_WEBUI_GATEWAY_USE_RUNS_API_ENV = "HERMES_WEBUI_GATEWAY_USE_RUNS_API"
_GATEWAY_CHAT_BACKENDS = {"gateway", "api_server", "api-server"}


def webui_chat_backend_mode(
    config_data=None,
    environ: dict[str, str] | None = None,
) -> str:
    """Return the explicitly selected browser chat backend."""
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_CHAT_BACKEND_ENV)
        or cfg.get("webui_chat_backend")
        or ""
    ).strip().lower()
    return "gateway" if raw in _GATEWAY_CHAT_BACKENDS else "legacy"


def webui_gateway_chat_enabled(
    config_data=None,
    environ: dict[str, str] | None = None,
) -> bool:
    return webui_chat_backend_mode(config_data, environ) == "gateway"


def gateway_base_url(
    config_data=None,
    environ: dict[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_GATEWAY_BASE_URL_ENV)
        or cfg.get("webui_gateway_base_url")
        or "http://127.0.0.1:8642"
    ).strip()
    return raw.rstrip("/") or "http://127.0.0.1:8642"


def gateway_api_key(environ: dict[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    return str(
        source.get(_WEBUI_GATEWAY_API_KEY_ENV)
        or source.get("API_SERVER_KEY")
        or ""
    ).strip()


def gateway_use_runs_api_enabled(
    config_data=None,
    environ: dict[str, str] | None = None,
) -> bool:
    """Return true only for the explicit Gateway Runs API opt-in."""
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_GATEWAY_USE_RUNS_API_ENV)
        or cfg.get("webui_gateway_use_runs_api")
        or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def gateway_reasoning_effort_for_request(
    cfg,
    *,
    model=None,
    model_provider=None,
):
    """Read and coerce configured reasoning effort for one Gateway request."""
    try:
        cfg_data = cfg if isinstance(cfg, dict) else {}
        effort_cfg = cfg_data.get("agent", {})
        effort_raw = (
            effort_cfg.get("reasoning_effort")
            if isinstance(effort_cfg, dict)
            else None
        )
        coerced = coerce_reasoning_effort_for_model(
            effort_raw,
            model,
            provider_id=model_provider,
        )
        return None if not coerced else str(coerced)
    except Exception:
        return None


def gateway_chat_config_status(
    config_data=None,
    environ: dict[str, str] | None = None,
) -> dict:
    """Return redacted Gateway-backed chat configuration status."""
    mode = webui_chat_backend_mode(config_data, environ)
    base_url = gateway_base_url(config_data, environ)
    return {
        "enabled": mode == "gateway",
        "backend": mode,
        "base_url_configured": bool(base_url),
        "api_key_configured": bool(gateway_api_key(environ)),
    }


def gateway_http_error_event(
    exc: urllib.error.HTTPError,
    err_body: str,
    *,
    api_key_configured: bool,
) -> dict:
    """Project a Gateway HTTP failure into the safe browser error contract."""
    safe = _redact_text(err_body or str(exc))[:500]
    if exc.code == 401:
        return {
            "label": "Gateway authentication failed",
            "type": "gateway_auth_error",
            "message": "Gateway rejected the WebUI API key (HTTP 401).",
            "hint": (
                "Set HERMES_WEBUI_GATEWAY_API_KEY to the same value as the "
                "Hermes Gateway API_SERVER_KEY, or disable "
                "HERMES_WEBUI_CHAT_BACKEND=gateway."
                if not api_key_configured
                else "Check that HERMES_WEBUI_GATEWAY_API_KEY matches the "
                "Hermes Gateway API_SERVER_KEY."
            ),
        }
    return {
        "label": "Gateway request failed",
        "type": "gateway_http_error",
        "message": f"Gateway returned HTTP {exc.code}.",
        "hint": safe or "Check the configured Gateway API server.",
    }
