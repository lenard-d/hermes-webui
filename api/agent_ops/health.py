"""Compose fail-closed local or remote Hermes Agent health status."""

from __future__ import annotations

from typing import Any

from . import gateway_status as _local
from . import remote_health as _remote


def build_agent_health_payload() -> dict[str, Any]:
    """Return the tri-state health payload for the configured Hermes Gateway."""
    checked_at = _local._checked_at()
    remote_base = _remote._remote_gateway_base_url()
    if remote_base is not None:
        return _remote._probe_remote_gateway(remote_base)

    try:
        gateway_status = _local._gateway_status_module()
    except Exception as exc:
        return {
            "alive": None,
            "checked_at": checked_at,
            "details": {
                "state": "unknown",
                "reason": "gateway_status_unavailable",
                "error": type(exc).__name__,
            },
        }

    gateway_pid_path = _local._gateway_root_pid_path()
    try:
        runtime_status = _local._read_gateway_runtime_status(gateway_status, gateway_pid_path)
    except Exception:
        runtime_status = None
    try:
        running_pid = _local._gateway_running_pid(gateway_status, gateway_pid_path)
    except Exception:
        running_pid = None

    safe_details = _local._runtime_detail_subset(runtime_status)
    if running_pid is not None:
        return {
            "alive": True,
            "checked_at": checked_at,
            "details": {"state": "alive", **safe_details},
        }
    if _local._runtime_status_is_fresh(runtime_status):
        return {
            "alive": True,
            "checked_at": checked_at,
            "details": {
                "state": "alive",
                "reason": "cross_container_freshness",
                **safe_details,
            },
        }
    if _local._runtime_status_is_stale_stopped(runtime_status):
        return {
            "alive": None,
            "checked_at": checked_at,
            "details": {
                "state": "unknown",
                "reason": "gateway_stale_stopped_state",
                **safe_details,
            },
        }
    if _local._runtime_status_is_stale_running(runtime_status):
        return {
            "alive": None,
            "checked_at": checked_at,
            "details": {
                "state": "unknown",
                "reason": "gateway_stale_running_state",
                **safe_details,
            },
        }
    if isinstance(runtime_status, dict):
        return {
            "alive": False,
            "checked_at": checked_at,
            "details": {
                "state": "down",
                "reason": "gateway_not_running",
                **safe_details,
            },
        }
    return {
        "alive": None,
        "checked_at": checked_at,
        "details": {
            "state": "unknown",
            "reason": "gateway_not_configured",
        },
    }
