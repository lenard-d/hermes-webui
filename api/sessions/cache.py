"""Stable in-process session-cache Interface.

Freshness checks, safe eviction, cache publication/loading, and new-session
creation are separate owners.  This module remains the compatibility seam for
legacy callers while new code imports the narrow owner directly.
"""

# This module deliberately re-exports the former cache surface while callers
# migrate to the narrower owner modules named below.
# ruff: noqa: F401

import logging

from .session_cache_eviction import (
    _UNSAVED_SHELL_GRACE_S,
    _evict_sessions_over_cap,
    _session_is_evictable,
)
from .session_cache_freshness import (
    _anchor_scene_record_keys,
    _anchor_scene_records_updated_at,
    _cache_has_stale_unsaved_user_tail,
    _cached_session_lags_disk,
    _inactive_cache_tail_needs_disk_check,
    _last_non_tool_message,
    _last_non_tool_role,
    _persisted_message_count,
    _persisted_session_meta_prefix,
    _session_scene_keys,
    _session_scene_updated_at,
    _session_sidecar_exists,
)
from .session_cache_repository import cache_full_session, get_session
from .session_creation import (
    _COMPRESSION_RECOVERY_PROFILE_UNSET,
    _compression_recovery_child_matches,
    _profile_default_model_state,
    find_compression_recovery_session,
    new_session,
)
from .state_db_messages import get_state_db_session_messages, get_state_db_session_summary

logger = logging.getLogger(__name__)
