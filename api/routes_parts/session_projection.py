"""HTTP adapters and compatibility imports for session projections."""

# Compatibility imports are the route facade's explicit public surface.
# ruff: noqa: F401

from __future__ import annotations

from api.helpers import bad
from api.profiles import is_valid_profile_id
from api.sessions.cache import get_session
from api.sessions.detail_projection import (
    _COMPRESSION_RECOVERY_START_LOCK,
    _LIMITED_TOOL_CONTENT_MAX_CHARS,
    _LIMITED_TOOL_CONTENT_NOTICE,
    _MAX_MSG_LIMIT,
    _SESSION_DETAIL_TAIL_CACHE,
    _SESSION_DETAIL_TAIL_CACHE_BYTES,
    _SESSION_DETAIL_TAIL_CACHE_LOCK,
    _SESSION_DETAIL_TAIL_CACHE_MAX_BYTES,
    _SESSION_DETAIL_TAIL_CACHE_MAX_ENTRIES,
    _SESSION_DETAIL_TAIL_CACHE_MAX_ENTRY_BYTES,
    _SESSION_DETAIL_TAIL_CACHE_VERSION,
    _SIDECAR_BYTE_TAIL_THRESHOLD,
    _STATE_DB_DISPLAY_ROW_BACKSTOP,
    _clear_session_detail_tail_cache,
    _limited_webui_messages_for_display,
    _limited_webui_messages_for_display_with_sidecar,
    _merged_session_messages_for_display,
    _merged_webui_lineage_messages_for_display,
    _message_counts_as_renderable_for_window,
    _message_summary,
    _message_window_for_display,
    _messages_for_limited_payload,
    _messages_include_tool_metadata,
    _messages_start_with_visible_prefix,
    _metadata_only_message_summary,
    _numeric_count,
    _parse_msg_limit,
    _pre_compression_continuation_session_id,
    _session_detail_tail_cache_eligible,
    _session_detail_tail_cache_get,
    _session_detail_tail_cache_key,
    _session_detail_tail_cache_set,
    _session_detail_tail_path_stamp,
    _session_detail_tail_source_stamp,
    _sidecar_file_exceeds_threshold,
    _state_db_backstop_limit_for_display,
    _state_db_since_timestamp_for_limited_display,
    _tool_call_ids_in_messages,
    _tool_calls_for_message_window,
    _tool_message_for_limited_payload,
    _tool_result_matches_call_ids,
    _webui_sidecar_lineage_messages_for_display,
)
from api.sessions.materialization import (
    _claim_or_synthesize_cli_session,
    _is_claimable_cli_source,
    _is_subagent_child_session_id,
    _lookup_cli_session_metadata,
    _lookup_gateway_session_identity,
    _publish_materialized_session,
    _resolve_cli_import_metadata,
    _session_deleted_tombstone_marks_was_webui,
    _session_index_marks_was_webui,
    _session_is_subagent_view_only,
    _state_db_session_source,
)
from api.sessions.sidebar_projection import (
    CLI_VISIBLE_SESSION_CAP,
    _SIDEBAR_SESSION_RESPONSE_FIELDS,
    _cap_recent_cli_sessions,
    _dedupe_cli_sidebar_sessions_for_api,
    _has_durable_messaging_identity,
    _is_api_server_sidecar_row,
    _is_cli_session_for_settings,
    _is_duplicate_webui_state_projection,
    _is_messaging_session_id,
    _is_pre_compression_continuation_row,
    _is_pre_compression_snapshot_id,
    _keep_latest_messaging_session_per_source,
    _merge_cli_sidebar_metadata,
    _messaging_session_identity,
    _messaging_source_key,
    _normalize_sidebar_source_flags,
    _normalized_source_marker,
    _reconcile_session_detail_source_flags,
    _redact_sidebar_title_fields,
    _session_attention_summary,
    _session_lineage_ids,
    _session_messaging_raw_source,
    _session_sort_timestamp,
    _session_source_is_webui,
    _should_hide_stale_messaging_session,
    _sidebar_session_response_item,
)
from api.sessions.sources import (
    is_messaging_session_record as _is_messaging_session_record,
    requires_external_metadata_lookup as _session_requires_cli_metadata_lookup,
)


def _request_wants_all_profiles_import(body) -> bool:
    """Return whether an import request explicitly allows cross-profile lookup."""
    if not isinstance(body, dict):
        return False
    if body.get("all_profiles") is True:
        return True
    scope = str(body.get("profile_scope") or "").strip().lower()
    return scope in {"all", "all_profiles"}


def _normalize_import_profile_value(value):
    """Return a validated profile id or None for an omitted profile."""
    if value is None:
        return None
    profile = str(value).strip()
    if not profile:
        return None
    if not is_valid_profile_id(profile):
        raise ValueError("Invalid profile")
    return profile


def _load_branch_source_or_refuse(handler, sid: str):
    """Resolve a branch source and serialize only HTTP refusal outcomes."""
    if _session_is_subagent_view_only(sid):
        bad(
            handler,
            "Subagent sessions are view-only and cannot be branched from WebUI",
            400,
        )
        return None
    try:
        source = get_session(sid)
    except KeyError:
        source, reason = _claim_or_synthesize_cli_session(sid)
        if source is None:
            bad(handler, "Session not found", 404)
            return None
        source_kind = str(
            getattr(source, "source_tag", None)
            or getattr(source, "raw_source", None)
            or getattr(source, "source", None)
            or ""
        ).strip().lower()
        if reason == "not_claimable" and source_kind == "cron":
            source._branch_source_readonly = True
            return source
        if reason == "not_claimable":
            bad(handler, "Read-only sessions cannot be branched from WebUI", 403)
            return None
    if getattr(source, "read_only", False):
        source_kind = str(
            getattr(source, "source_tag", None)
            or getattr(source, "raw_source", None)
            or getattr(source, "source", None)
            or ""
        ).strip().lower()
        if source_kind == "cron":
            source._branch_source_readonly = True
            return source
        bad(handler, "Read-only sessions cannot be branched from WebUI", 403)
        return None
    return source


# api.routes imports these names explicitly. Domain functions retain their owner
# globals; the three functions above are flat HTTP adapters with explicit deps.
__routes_exports__ = ()
