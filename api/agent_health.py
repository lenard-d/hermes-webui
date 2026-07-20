"""Compatibility imports for the :mod:`api.agent_ops` health interface."""

from api.agent_ops import (
    build_agent_health_payload,
    get_active_profile_gateway_running_pid,
)

__all__ = [
    "build_agent_health_payload",
    "get_active_profile_gateway_running_pid",
]
