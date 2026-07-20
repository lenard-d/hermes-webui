"""Durable conversation sessions and their lifecycle.

The package interface stays intentionally small. Specialized callers import
the owning module (for example ``api.sessions.recovery``) instead of depending
on a broad compatibility facade.
"""

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
from .store import (
    SESSION_DIR,
    Session,
    _REPAIR_STALE_PENDING_GRACE_SECONDS,
    all_sessions,
    clear_process_wakeup_pause,
    get_session,
    get_session_for_file_ops,
    is_safe_session_id,
    merge_session_messages_append_only,
    model_explicit_pick_signature,
    new_session,
    title_from,
)

REPAIR_STALE_PENDING_GRACE_SECONDS = _REPAIR_STALE_PENDING_GRACE_SECONDS

__all__ = [
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
    "admission_write_owner",
    "all_sessions",
    "clear_process_wakeup_pause",
    "cleanup_session_store",
    "collect_expired_session_channels",
    "commit_stream_writeback",
    "compensate_failed_admission",
    "delete_session_state",
    "edit_session",
    "get_full_session",
    "get_or_create_session_channel",
    "get_session",
    "get_session_for_file_ops",
    "get_session_channel",
    "is_safe_session_id",
    "mark_turn_completed",
    "merge_session_messages_append_only",
    "model_explicit_pick_signature",
    "new_session",
    "persisted_message_count_for_session",
    "publish_session_list_changed",
    "register_agent",
    "session_write_owner",
    "should_emit_session_updated",
    "subscribe_to_session_channel",
    "title_from",
]
