"""Compatibility imports for the :mod:`api.agent_ops` watcher interface."""

from api.agent_ops import (
    GatewayWatcher,
    get_watcher,
    restart_watcher_for_profile,
    start_watcher,
    stop_watcher,
)

__all__ = [
    "GatewayWatcher",
    "get_watcher",
    "restart_watcher_for_profile",
    "start_watcher",
    "stop_watcher",
]
