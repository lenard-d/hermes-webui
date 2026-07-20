"""Durable conversation sessions and their lifecycle.

The package interface stays intentionally small. Specialized callers import
the owning module (for example ``api.sessions.recovery``) instead of depending
on a broad compatibility facade.
"""

from importlib import import_module
from typing import Any

from .channels import (
    SESSION_CHANNELS,
    SESSION_CHANNELS_LOCK,
    SessionChannel,
    active_stream_id_for_session,
    collect_expired_session_channels,
    get_or_create_session_channel,
    get_session_channel,
    persisted_message_count_for_session,
    should_emit_session_updated,
    subscribe_to_session_channel,
)
from .events import publish_session_list_changed
from .lifecycle import mark_turn_completed, register_agent
from .operations import (
    apply_session_title_rename,
    retry_last,
    session_status,
    session_usage,
    truncate_context_for_display_keep,
    truncate_session_at_keep,
    undo_last,
)
from .recovery import audit_session_recovery, repair_safe_session_recovery
from .repository import (
    SessionActiveError,
    SessionBusyError,
    SessionWriteRejected,
    admission_write_owner,
    cleanup_session_store,
    commit_stream_writeback,
    compensate_failed_admission,
    delete_session_state,
    edit_session,
    get_full_session,
    session_write_owner,
)
from .cache import get_session, new_session
from .anchor_scene import AnchorSceneMessageNotFound, persist_anchor_activity_scene
from .compression_recovery_projection import start_or_get_focused_continuation
from .external import clear_cli_sessions_cache
from .pending_recovery import _REPAIR_STALE_PENDING_GRACE_SECONDS
from .process_wakeup import clear_process_wakeup_pause
from .projects import load_projects, title_from
from .reconciliation import merge_session_messages_append_only
from .records import (
    SESSION_DIR,
    Session,
    is_safe_session_id,
    model_explicit_pick_signature,
)
from .sidebar import all_sessions
from .sources import is_messaging_session_record, requires_external_metadata_lookup
from .state_db import _active_state_db_path, get_session_for_file_ops

REPAIR_STALE_PENDING_GRACE_SECONDS = _REPAIR_STALE_PENDING_GRACE_SECONDS


def active_state_db_path():
    """Return the active profile's Hermes state database path."""
    return _active_state_db_path()


_LAZY_PUBLIC = {
    "foreign_session_access": (
        ".materialization",
        "foreign_session_access",
    ),
    "session_detail_projection": (
        ".detail_projection",
        "session_detail_projection",
    ),
    "session_sidebar_projection": (
        ".sidebar_projection",
        "sidebar_projection",
    ),
}


def __getattr__(name: str) -> Any:
    """Load specialized session operations without widening import cycles."""
    try:
        module_name, attribute = _LAZY_PUBLIC[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def commit_session_memory(
    session_id: str,
    agent=None,
    *,
    wait: bool = False,
    timeout: float | None = None,
) -> bool:
    """Commit session memory through the live lifecycle implementation."""
    from . import lifecycle

    if not wait and timeout is None:
        return lifecycle.commit_session_memory(session_id, agent=agent)
    return lifecycle.commit_session_memory(
        session_id, agent=agent, wait=wait, timeout=timeout
    )


def register_background_commit_thread(thread) -> bool:
    """Register a lifecycle worker through the public sessions interface."""
    from . import lifecycle

    return lifecycle._register_background_commit_thread(thread)


def unregister_background_commit_thread(thread) -> None:
    """Unregister a lifecycle worker through the public sessions interface."""
    from . import lifecycle

    lifecycle._unregister_background_commit_thread(thread)


__all__ = [
    "AnchorSceneMessageNotFound",
    "Session",
    "SessionActiveError",
    "SessionBusyError",
    "SessionChannel",
    "SessionWriteRejected",
    "SESSION_DIR",
    "SESSION_CHANNELS",
    "SESSION_CHANNELS_LOCK",
    "REPAIR_STALE_PENDING_GRACE_SECONDS",
    "active_stream_id_for_session",
    "active_state_db_path",
    "admission_write_owner",
    "all_sessions",
    "apply_session_title_rename",
    "audit_session_recovery",
    "clear_cli_sessions_cache",
    "clear_process_wakeup_pause",
    "cleanup_session_store",
    "collect_expired_session_channels",
    "commit_stream_writeback",
    "commit_session_memory",
    "compensate_failed_admission",
    "delete_session_state",
    "edit_session",
    "get_full_session",
    "get_or_create_session_channel",
    "get_session",
    "get_session_for_file_ops",
    "get_session_channel",
    "foreign_session_access",
    "is_safe_session_id",
    "is_messaging_session_record",
    "load_projects",
    "mark_turn_completed",
    "merge_session_messages_append_only",
    "model_explicit_pick_signature",
    "new_session",
    "persisted_message_count_for_session",
    "persist_anchor_activity_scene",
    "publish_session_list_changed",
    "register_agent",
    "register_background_commit_thread",
    "repair_safe_session_recovery",
    "requires_external_metadata_lookup",
    "retry_last",
    "session_status",
    "session_detail_projection",
    "session_usage",
    "session_write_owner",
    "should_emit_session_updated",
    "subscribe_to_session_channel",
    "start_or_get_focused_continuation",
    "session_sidebar_projection",
    "title_from",
    "truncate_context_for_display_keep",
    "truncate_session_at_keep",
    "undo_last",
    "unregister_background_commit_thread",
]
