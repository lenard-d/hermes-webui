"""Public interface for Hermes Agent session and Gateway operations.

The package owns read-only Agent session discovery, lineage projection,
profile-scoped watcher lifecycle, Gateway health, and Gateway restart control.
HTTP routing and run execution remain outside this interface.
"""

from .session_discovery import open_state_db_readonly, read_importable_agent_session_rows
from .session_lineage import read_session_lineage_metadata, read_session_lineage_report
from .session_sources import (
    MESSAGING_SOURCES,
    _is_continuation_session as is_continuation_session,
    _looks_like_default_cli_title as looks_like_default_cli_title,
    is_cli_session_row,
    is_cli_session_row_visible,
    normalize_agent_session_source,
)
from .session_watcher import (
    GatewayWatcher,
    get_watcher,
    restart_watcher_for_profile,
    start_watcher,
    stop_watcher,
)
from .gateway_status import get_active_profile_gateway_running_pid
from .health import build_agent_health_payload


def __getattr__(name: str):
    """Load the profile-aware control implementation only when requested."""
    if name == "restart_active_profile_gateway":
        from .gateway_control import restart_active_profile_gateway

        return restart_active_profile_gateway
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "GatewayWatcher",
    "MESSAGING_SOURCES",
    "build_agent_health_payload",
    "get_active_profile_gateway_running_pid",
    "get_watcher",
    "is_cli_session_row",
    "is_cli_session_row_visible",
    "is_continuation_session",
    "looks_like_default_cli_title",
    "normalize_agent_session_source",
    "open_state_db_readonly",
    "read_importable_agent_session_rows",
    "read_session_lineage_metadata",
    "read_session_lineage_report",
    "restart_active_profile_gateway",
    "restart_watcher_for_profile",
    "start_watcher",
    "stop_watcher",
]
