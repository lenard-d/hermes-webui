"""
Hermes Web UI -- Route handlers for GET and POST endpoints.
Extracted from server.py (Sprint 11) so server.py is a thin shell.
"""

# The explicit per-request HTTP compatibility context consumes many facade
# exports dynamically.  Keep these imports visible until their legacy callers
# migrate to the public domain interfaces.
# ruff: noqa: F401, F811

import html as _html
import copy
import hashlib
import inspect
import errno
import io
import gzip
import json
from api.sse_chunked import end_sse_headers
import logging
import os
import queue
import re
import platform
import shlex
import shutil
import sqlite3
import stat as _stat
import subprocess
import sys
import threading
import time
import uuid
import http.client
import socket as _socket
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from contextlib import closing
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from api.runs.agent_runtime import (
    AgentRuntimeChangedError,
    ensure_agent_runtime_current,
    require_ai_agent_class,
)
from api.agent_ops import (
    MESSAGING_SOURCES,
    is_cli_session_row,
    is_cli_session_row_visible,
    looks_like_default_cli_title as _looks_like_default_cli_title,
    read_session_lineage_report,
)
from api.compression_anchor import visible_messages_for_anchor
from api.compression_recovery import (
    COMPRESSION_RECOVERY_ACTION_START_FOCUSED,
    clear_compression_recovery,
    compression_recovery_payload_for_session,
    is_generic_continuation_intent,
)
from api.sessions.events import (
    add_session_list_changed_listener,
    publish_session_list_changed,
    subscribe_session_events,
    unsubscribe_session_events,
)
from api.shares import create_or_refresh_share, load_share, revoke_share
from api.sessions.repository import (
    SessionActiveError,
    SessionBusyError,
    cleanup_session_store,
    delete_session_state,
    edit_session,
    get_full_session,
)
from api.sessions.sources import apply_cli_source_metadata
from api.routes_parts._binding import install_routes_part as _install_routes_part

logger = logging.getLogger(__name__)


from api.sessions import title_publication as _session_title_publication
from api.sessions.title_publication import (
    _publish_session_list_changed,
    _sync_session_title_to_insights,
    _persist_generated_session_title,
    _queue_generated_title_for_imported_session,
)

_install_routes_part(globals(), _session_title_publication)
del _session_title_publication


def _on_session_list_changed(profile: str | None = None) -> None:
    """Invalidate in-process /api/sessions cache when sidebar state mutates."""
    _clear_session_list_cache(profile)
    # #4842: also drop the inner CLI/cron projection cache. While a turn streams
    # that cache is frozen on a stable streaming marker (so per-token message
    # writes don't bust it), which means it no longer self-invalidates via the
    # state.db content fingerprint mid-stream. In-app structural mutations
    # (session create/rename/archive/delete/branch/pin/move/import, attention)
    # fire this listener — and never fire per streamed token — so clearing here
    # restores prompt freshness for those without reintroducing the per-poll
    # rebuild the freeze removed. Note: externally-driven changes that do NOT go
    # through this listener (a scheduled cron job completing, or an external CLI
    # writing rows directly) are not cleared here mid-stream; for those the 30s
    # streaming TTL is the backstop — they surface within one streaming-TTL
    # window (≤30s) rather than instantly. That bound is the deliberate
    # latency/CPU trade-off of the freeze.
    try:
        from api.sessions.store import clear_cli_sessions_cache
        clear_cli_sessions_cache()
    except Exception:
        logger.debug("Failed to clear CLI sessions cache on session list change", exc_info=True)


try:
    add_session_list_changed_listener(_on_session_list_changed)
except Exception:
    logger.debug("Failed to register session list cache invalidation listener", exc_info=True)


# ── Cron run tracking ────────────────────────────────────────────────────────
# Track job IDs currently being executed so the frontend can poll status.
from api.routes_parts import cron as _cron_routes_part
from api.routes_parts.cron import (
    _RUNNING_CRON_JOBS,
    _RUNNING_CRON_LOCK,
    _CRON_CREATE_SNAPSHOT_LOCK,
    _CRON_OUTPUT_CONTENT_LIMIT,
    _CRON_OUTPUT_HEADER_CONTEXT,
    _normalize_cron_job_ids,
    _latest_cron_session_info_for_jobs,
    _mark_cron_running,
    _mark_cron_done,
    _is_cron_running,
    _cron_response_marker_index,
    _cron_output_content_window,
    _cron_job_for_api,
    _cron_jobs_for_api,
    _AGENT_CRON_IMPORT_PATH_LOCK,
    _AGENT_CRON_IMPORT_PATH_READY,
    _ensure_agent_cron_import_path,
    _cron_jobs_cross_profile,
    _available_cron_profile_names,
    _normalize_cron_profile_value,
    _profile_home_for_cron_job,
    _event_profile_for_cron_job,
    _cron_job_subprocess_main,
    _cron_subprocess_result_timeout_seconds,
    _run_cron_job_in_profile_subprocess,
    _run_cron_tracked,
    _handle_cron_history,
    _handle_cron_run_detail,
    _cron_output_usage_metadata,
    _cron_output_snippet,
    _handle_cron_output,
    _handle_cron_status,
    _handle_cron_recent,
    _selected_profile_snapshot_updates,
    _handle_cron_create,
    _handle_cron_delivery_options,
    _handle_cron_update,
    _handle_cron_delete,
    _handle_cron_run,
    _handle_cron_pause,
    _handle_cron_resume,
)

_install_routes_part(globals(), _cron_routes_part)
del _cron_routes_part
_MESSAGING_RAW_SOURCES = {str(s).strip().lower() for s in MESSAGING_SOURCES}
_MESSAGING_SESSION_METADATA_CACHE: dict[str, object] = {
    "path": None,
    "mtime": None,
    "identity": {},
}
_MESSAGING_SESSION_METADATA_LOCK = threading.Lock()
_STALE_MESSAGING_END_REASONS = {"session_reset", "session_switch"}









# ── Profile-scoped session/project filtering (#1611, #1614) ────────────────
#
# Sessions and projects are stored in the WebUI sidecar without per-row
# isolation by default — they're tagged with a `profile` field but every
# query saw all rows. The fix scopes both endpoints to the active profile
# by default, with `?all_profiles=1` opting into aggregate mode.
#
# Renamed-root profile handling (#1612): a row tagged `profile='default'`
# matches the active root regardless of the root's display name, and a row
# tagged with the renamed-root display name (e.g. 'kinni') likewise matches
# when the active profile is `'default'`. _is_root_profile() is the
# canonical check.

# Canonical helper now lives in api.profiles so out-of-process consumers
# (mcp_server.py) can import it without duplicating the visibility model.
# Re-exported here so existing `_profiles_match(...)` call sites in this
# module keep resolving without per-call-site refactors.
from api.profiles import (  # noqa: F401, E402  (re-export)
    _profiles_match,
    _is_isolated_profile_mode,
    is_root_profile as _is_root_profile,
    _SKILLS_STATS_CACHE,
    get_active_profile_name,
    get_active_profile_name as _get_active_profile_name,
    get_active_hermes_home,
    is_valid_profile_id,
    list_profiles_api,
    profile_scope_for_detached_worker,
)


from api.http import session_visibility as _session_visibility_http
from api.http.session_visibility import (
    _all_profiles_query_flag,
    _all_profiles_enabled,
    _query_flag,
    _query_positive_int,
    _session_visible_to_active_profile,
    _request_session_visibility_exempt,
    _session_id_visible_to_request_profile,
    _stream_id_owner_session_id,
    _stream_id_visible_to_request_profile,
    _guard_request_session_visibility,
)

_install_routes_part(globals(), _session_visibility_http)
del _session_visibility_http


from api.routes_parts import skills as _skills_routes_part
from api.routes_parts.skills import (
    _active_skills_dir,
    _skill_path_within,
    _skill_category_from_path,
    _active_skill_search_dirs,
    _worktree_retained_payload,
    _worktree_retained_payload_for_session_id,
    _active_profile_config_path,
    _get_disabled_skill_names_for_profile,
    _normalize_disabled_set,
    _skills_list_from_dir,
    _find_skill_in_dirs,
    _find_skill_in_dir,
    _skill_not_found_payload,
    _linked_files_for_skill,
    _skill_view_from_file,
    _skill_view_from_active_dir,
    _handle_skill_save,
    _handle_skill_delete,
    _normalize_names_list,
    _toggle_name_in_list,
    _handle_skill_toggle,
)

_install_routes_part(globals(), _skills_routes_part)
del _skills_routes_part


from api.streaming.transport import (
    SSE_HEARTBEAT_INTERVAL_SECONDS as _SSE_HEARTBEAT_INTERVAL_SECONDS,
)
_SESSION_SSE_SENT_EVENT_ID_LIMIT = 4096


from api.routes_parts import gateway as _gateway_routes_part
from api.routes_parts.gateway import (
    _GATEWAY_ACTION_LOCK,
    _GATEWAY_LIFECYCLE_TIMEOUT_SECONDS,
    _gateway_session_metadata_path,
    _gateway_status_payload,
    _handle_gateway_lifecycle,
    _is_known_messaging_source,
    _load_gateway_session_identity_map,
    _normalize_messaging_source,
    _run_gateway_lifecycle_command,
    _safe_first,
)

_install_routes_part(globals(), _gateway_routes_part)
del _gateway_routes_part








































_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "gpt": "openai",
    "gemini": "google",
    "openai-codex": "openai",
    "openai-api": "openai",
    "google-gemini": "google",
    "google-ai-studio": "google",
    "claude-code": "anthropic",
}
from api.routes_parts import live_models as _live_models_routes_part
from api.routes_parts.live_models import (  # noqa: F401 - compatibility facade re-exports
    _OPENAI_COMPAT_ENDPOINTS,
    _LIVE_MODELS_CACHE_TTL,
    _LIVE_MODELS_CACHE,
    _LIVE_MODELS_CACHE_LOCK,
    _LIVE_MODELS_LOOPBACK_HOSTS,
    _live_models_address_is_global,
    _live_models_address_is_loopback,
    _resolve_live_models_addresses,
    _prepare_live_models_target,
    _NoRedirectLiveModelsHandler,
    _PinnedLiveModelsHTTPConnection,
    _PinnedLiveModelsHTTPSConnection,
    _PinnedLiveModelsHTTPHandler,
    _PinnedLiveModelsHTTPSHandler,
    _open_live_models_request,
    _fetch_live_models_payload,
    _active_profile_for_live_models_cache,
    _live_models_cache_key,
    _get_cached_live_models,
    _set_cached_live_models,
    _clear_live_models_cache,
    _handle_live_models,
)

_install_routes_part(globals(), _live_models_routes_part)
del _live_models_routes_part
for _live_models_compat_class in (
    _NoRedirectLiveModelsHandler,
    _PinnedLiveModelsHTTPConnection,
    _PinnedLiveModelsHTTPSConnection,
    _PinnedLiveModelsHTTPHandler,
    _PinnedLiveModelsHTTPSHandler,
):
    _live_models_compat_class.__module__ = __name__
del _live_models_compat_class


from api.sessions import sidebar_cache as _route_session_list_cache

_SESSIONS_CACHE = _route_session_list_cache._SESSIONS_CACHE
_SESSIONS_CACHE_INFLIGHT = _route_session_list_cache._SESSIONS_CACHE_INFLIGHT
_SESSIONS_CACHE_LOCK = _route_session_list_cache._SESSIONS_CACHE_LOCK
_SESSIONS_CACHE_MAX_ENTRIES = _route_session_list_cache._SESSIONS_CACHE_MAX_ENTRIES
_SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION = (
    _route_session_list_cache._SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION
)
_SESSIONS_CACHE_STALE_WAIT_SECONDS = _route_session_list_cache._SESSIONS_CACHE_STALE_WAIT_SECONDS
_SESSIONS_CACHE_STREAMING_TTL_SECONDS = (
    _route_session_list_cache._SESSIONS_CACHE_STREAMING_TTL_SECONDS
)
_SESSIONS_CACHE_TTL_SECONDS = _route_session_list_cache._SESSIONS_CACHE_TTL_SECONDS
_SESSIONS_CACHE_WAIT_SECONDS = _route_session_list_cache._SESSIONS_CACHE_WAIT_SECONDS
_clear_session_list_cache = _route_session_list_cache._clear_session_list_cache
_session_list_cache_clear = _route_session_list_cache._session_list_cache_clear
_session_list_cache_claim_rebuild = _route_session_list_cache._session_list_cache_claim_rebuild
_session_list_cache_done = _route_session_list_cache._session_list_cache_done
_session_list_cache_get = _route_session_list_cache._session_list_cache_get
_session_list_cache_invalidation_stamp = _route_session_list_cache._session_list_cache_invalidation_stamp
_route_session_list_cache_key = _route_session_list_cache._session_list_cache_key
_session_list_cache_overlay_runtime_rows = _route_session_list_cache._session_list_cache_overlay_runtime_rows
_session_list_cache_path_stamp = _route_session_list_cache._session_list_cache_path_stamp
_session_list_cache_profile_scope = _route_session_list_cache._session_list_cache_profile_scope
_session_list_row_is_runtime_active = _route_session_list_cache._session_list_row_is_runtime_active
_session_list_row_numeric_value = _route_session_list_cache._session_list_row_numeric_value
_session_list_row_timestamp = _route_session_list_cache._session_list_row_timestamp
_session_list_runtime_sort_key = _route_session_list_cache._session_list_runtime_sort_key
_session_list_cache_set = _route_session_list_cache._session_list_cache_set
_session_list_cache_source_stamp = _route_session_list_cache._session_list_cache_source_stamp
_session_list_cache_state_db_fingerprint = _route_session_list_cache._session_list_cache_state_db_fingerprint
_session_list_cache_stale_reason = _route_session_list_cache._session_list_cache_stale_reason
_session_list_cache_streaming_freeze_marker = _route_session_list_cache._session_list_cache_streaming_freeze_marker


from api.sessions import sidebar_listing as _session_sidebar_listing
from api.sessions.sidebar_listing import (
    _session_field,
    _session_counts_toward_pin_quota,
    _session_row_lineage_root_id,
    _visible_pinned_lineage_ids,
    _callable_accepts_kwarg,
    _session_list_cache_key,
    _prune_orphaned_webui_zero_message_sessions,
    _build_session_list_cache_payload,
    _session_list_payload_to_response,
    _hidden_archived_sidebar_reference_sessions,
    _get_cached_session_list_payload,
)

_install_routes_part(globals(), _session_sidebar_listing)
del _session_sidebar_listing

_ROUTE_SESSION_LIST_CACHE_DYNAMIC_EXPORTS = {
    "_SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION",
    "_SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION",
    "_session_list_cache_settings_write_version",
}


def __getattr__(name):
    if name in _ROUTE_SESSION_LIST_CACHE_DYNAMIC_EXPORTS:
        return getattr(_route_session_list_cache, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



from api.config import (
    STATE_DIR,
    SESSION_DIR,
    DEFAULT_WORKSPACE,
    DEFAULT_MODEL,
    SESSIONS,
    SESSIONS_MAX,
    LOCK,
    _resolve_cli_toolsets,
    get_available_models,
    get_available_models_for_session_visit,
    _provider_is_known_or_configured,
    IMAGE_EXTS,
    MD_EXTS,
    MIME_MAP,
    MAX_FILE_BYTES,
    register_runtime_stream,
    blocking_runtime_stream,
    runtime_stream_alive,
    runtime_worker_alive,
    runtime_transport,
    runtime_run_session_id,
    runtime_last_event_id,
    CHAT_LOCK,
    _get_session_agent_lock,
    CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS,
    load_settings,
    persisted_speech_settings_keys,
    save_settings,
    SETTINGS_FILE,
    set_hermes_default_model,
    canonical_model_provider_lane,
    model_with_provider_context,
    get_reasoning_status,
    set_reasoning_display,
    set_reasoning_effort,
    create_stream_channel,
    _get_config_path,
    _load_yaml_config_file,
    _save_yaml_config_file,
    reload_config,
    get_config_for_profile_home,
    _cfg_lock,
    PENDING_BG_TASK_COMPLETIONS,
)
from api import config as api_config
from api.helpers import (
    require,
    bad,
    safe_resolve,
    j,
    t,
    read_body,
    MAX_BODY_BYTES,
    _security_headers,
    _sanitize_error,
    redact_session_data,
    _redact_text,
    _CLIENT_DISCONNECT_ERRORS,
)
from api.agent_ops import build_agent_health_payload
from api.runs.gateway import gateway_chat_config_status
from api.request_diagnostics import RequestDiagnostics
from api.system_health import build_system_health_payload
from api.runs import (
    LocalTurnRequest,
    start_local_turn,
)


def _kanban_unknown_endpoint(handler, parsed, method: str) -> bool:
    """Return a Kanban-specific 404 for stale clients/obsolete endpoint shapes."""
    return bad(
        handler,
        (
            f"unknown Kanban endpoint: {method} {parsed.path}. "
            "If this appeared after a WebUI update, your browser may be running "
            "a stale cached bundle; use Hard refresh now, then reopen Kanban."
        ),
        status=404,
    ) or True


from api.sessions import runtime_recovery as _session_runtime_recovery
from api.sessions.runtime_recovery import (
    _clear_session_stream_fields,
    _clear_stale_stream_state,
    _reconcile_stale_stream_state_for_session_rows,
)

_install_routes_part(globals(), _session_runtime_recovery)
del _session_runtime_recovery


from api.routes_parts.anchor_scene import _handle_session_anchor_scene
from api.sessions.anchor_scene.hydration import _hydrate_anchor_activity_scenes
from api.sessions.anchor_scene.journal_projection import (
    _run_journal_live_snapshot,
    _run_journal_status_payload,
)


from api.sessions.materialization import (
    _get_or_materialize_session,
)


from api.http import share_sessions as _share_sessions_http
from api.http.share_sessions import (
    _share_snapshot_messages_for_session,
    _build_share_metadata_sidecar,
    _resolve_share_session_pair,
)

_install_routes_part(globals(), _share_sessions_http)
del _share_sessions_http



# ── CSRF: validate Origin/Referer on POST ────────────────────────────────────
import re as _re


from api.routes_parts import security as _security_routes_part
from api.routes_parts.security import (
    _CSP_REPORT_LOGGER,
    _CSP_REPORT_RATE_LIMIT,
    _CSP_REPORT_RATE_LIMIT_LOCK,
    _CSP_REPORT_RATE_LIMIT_WINDOW_SECONDS,
    _CSP_REPORT_RATE_LIMIT_MAX,
    _CSP_REPORT_MAX_BODY_BYTES,
    _CLIENT_EVENT_LOGGER,
    _CLIENT_EVENT_RATE_LIMIT,
    _CLIENT_EVENT_RATE_LIMIT_LOCK,
    _CLIENT_EVENT_RATE_LIMIT_WINDOW_SECONDS,
    _CLIENT_EVENT_RATE_LIMIT_MAX,
    _CLIENT_EVENT_MAX_BODY_BYTES,
    _EXTENSION_SIDECAR_PROXY_MAX_RESPONSE_BYTES,
    _CLIENT_EVENT_ALLOWED_FIELDS,
    _normalize_host_port,
    _ports_match,
    _allowed_public_origins,
    _is_browser_unsafe_request,
    _check_same_origin_browser_request,
    apply_cors_preflight_headers,
    _csrf_exempt_path,
    _CSRF_FAILURE_ATTR,
    _set_csrf_failure_reason,
    _clear_csrf_failure_reason,
    _csrf_rejection_error,
    _check_csrf,
    _EXTENSION_SIDECAR_PROXY_RE,
    _HOP_BY_HOP_HEADERS,
    _connection_bound_header_names,
    _match_extension_sidecar_proxy_path,
    _read_body_bytes,
    _extension_sidecar_proxy_request_headers,
    _send_extension_sidecar_proxy_response,
    _read_extension_sidecar_proxy_body,
    _extension_sidecar_proxy_redirect_url,
    _extension_sidecar_proxy_same_origin_opener,
    _handle_extension_sidecar_proxy,
    _client_ip_for_rate_limit,
    _truthy_env,
    _request_client_ip,
    _ip_is_loopback_or_private,
    _trusted_proxy_networks,
    _ip_in_networks,
    _raw_peer_is_trusted_proxy,
    _forwarded_client_ip_from_trusted_proxy,
    _onboarding_request_is_local,
    _onboarding_gate_allows,
    _EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE,
    _embedded_terminal_gate_allows,
    _RATE_LIMIT_MAP_SWEEP_THRESHOLD,
    _prune_stale_rate_limit_keys,
    _csp_report_rate_limited,
    _client_event_rate_limited,
    _send_no_content,
    _safe_content_length,
    _read_csp_report_payload,
    _handle_csp_report,
    _bounded_client_event_string,
    _sanitize_client_event_url_path,
    _sanitize_client_event_payload,
    _read_client_event_payload,
    _handle_client_event_log,
)

_install_routes_part(globals(), _security_routes_part)
del _security_routes_part



from api.routes_parts import session_models as _session_models_routes_part
from api.routes_parts.session_models import (
    _starts_token,
    _normalize_provider_id,
    _catalog_provider_id_sets,
    _catalog_has_provider,
    _model_matches_active_provider_family,
    _catalog_model_id_matches,
    _catalog_group_owns_exact_model,
    _repair_foreign_session_model_provider,
    _clean_session_model_provider,
    _split_provider_qualified_model,
    _model_matches_configured_default,
    _ContextLengthLookupInputs,
    _positive_context_length,
    _model_lookup_candidates,
    _models_config_context_length,
    _canonical_context_provider,
    _custom_provider_slug_for_context,
    _providers_match_for_context,
    _custom_provider_api_key_for_context,
    _context_length_config_api_key_for_provider,
    _context_length_lookup_inputs_for_model,
    _should_attach_codex_provider_context,
    _read_profile_model_config,
    _PROFILE_CONFIG_CACHE,
    _PROFILE_CONFIG_CACHE_TTL_SECONDS,
    _PROFILE_CONFIG_CACHE_LOCK,
    _read_profile_config_cached,
    _load_profile_config_dict,
    _ordered_custom_provider_model_ids,
    _repair_bare_custom_provider_model,
    _moa_fast_path_model_state,
    _resolve_compatible_session_model_state,
    _resolve_compatible_session_model,
    _normalize_session_model_in_place,
    _resolve_effective_session_model_for_display,
    _resolve_effective_session_model_provider_for_display,
    _resolve_context_length_for_session_model,
    _session_context_length_lookup_state,
    _session_model_identity_matches,
    _should_accept_session_context_length_refresh,
    _rescale_threshold_tokens_for_context_window,
    _worktree_default_from_config,
    _session_model_state_from_request,
)

_install_routes_part(globals(), _session_models_routes_part)
_ContextLengthLookupInputs.__module__ = __name__
del _session_models_routes_part


from api.sessions import (
    foreign_session_access,
    is_messaging_session_record,
    requires_external_metadata_lookup,
    session_detail_projection,
    session_sidebar_projection as sidebar_projection,
    start_or_get_focused_continuation,
)


from api.sessions.store import (
    Session,
    cache_full_session,
    get_session,
    find_compression_recovery_session,
    get_session_for_file_ops,
    new_session,
    all_sessions,
    title_from,
    _write_session_index,
    SESSION_INDEX_FILE,
    _active_state_db_path,
    load_projects,
    save_projects,
    import_cli_session,
    get_cli_sessions,
    get_cli_session_messages,
    get_state_db_session_messages,
    get_state_db_session_message_prefix_summary,
    get_state_db_session_message_keys_before_timestamp,
    get_state_db_session_summary,
    merge_session_messages_append_only,
    _enrich_sidebar_lineage_metadata,
    _active_stream_ids,
    _evict_sessions_over_cap,
    _merge_session_display_metadata,
    _session_message_merge_key,
    _session_messages_have_prefix,
    _session_message_visible_key,
    _message_timestamp_as_float,
    _is_empty_partial_activity_message,
    _hide_from_default_sidebar,
    prune_session_from_index,
    agent_session_rows_existing,
    agent_session_zero_message_sids,
    _load_webui_zero_message_orphan_tombstone,
    _record_webui_zero_message_orphan_tombstone,
    _clear_webui_zero_message_orphan_tombstone,
    _load_webui_deleted_session_tombstone,
    ensure_cron_project,
    _profile_has_user_projects,
    is_cron_session,
    is_safe_session_id,
    PROCESS_WAKEUP_PAUSE_ERROR,
    clear_process_wakeup_pause,
    clear_process_wakeup_pause_if_model_changed,
    process_wakeup_pause_matches,
    process_wakeup_credential_state_fingerprint,
    process_wakeup_pause_credential_state_changed,
    suppress_process_wakeup_for_provider_pause,
)



from api.workspace import (
    load_workspaces,
    save_workspaces,
    get_last_workspace,
    get_profile_default_workspace,
    set_last_workspace,
    authorize_escape_target,
    EscapeAuthorizationExpiredError,
    list_dir,
    list_authorized_escape_dir,
    dir_signature,
    list_workspace_suggestions,
    read_file_content,
    read_authorized_escape_file_content,
    safe_resolve_ws,
    raw_authorized_escape_target,
    resolve_trusted_workspace,
    open_anchored_fd,
    open_anchored_create_fd,
    open_anchored_write_fd,
    unlink_anchored,
    rmtree_anchored,
    rename_anchored,
    make_anchored_dir,
    validate_workspace_to_add,
    _is_blocked_system_path,
    _home_path,
    _is_within,
    _strip_surrounding_quotes,
    _is_remote_terminal_backend,
    _workspace_blocked_roots,
)
from api.routes_parts.media_uploads import (
    handle_upload,
    handle_upload_extract,
    handle_workspace_upload,
)
from api.streaming import (
    _sse,
    _sse_set_write_deadline,
    _run_agent_streaming,
    cancel_stream,
    _materialize_pending_user_turn_before_error,
    generate_session_title_for_session,
    _compact_for_echo_compare,
    _strip_compact_echo_suffix,
)
from api.runs.gateway import (  # noqa: F401 - compatibility facade re-exports
    _run_gateway_chat_streaming,
    webui_gateway_chat_enabled,
)
from api.runs.journal import (
    _parse_run_journal_event_id as _shared_parse_run_journal_event_id,
    bound_run_journal_snapshot_args,
    find_run_summary,
    read_run_events,
    read_session_run_events,
    session_journal_fingerprint,
    stale_interrupted_event,
)
from api.todo_state import attach_todo_state
from api.providers import (
    get_providers,
    get_provider_quota,
    get_provider_cost_history,
    provider_has_process_wakeup_recovery_credential,
    set_provider_key,
    remove_provider_key,
)
from api.onboarding import (
    apply_onboarding_setup,
    get_onboarding_status,
    complete_onboarding,
    probe_provider_endpoint,
)
from api.auth import (
    cancel_onboarding_oauth_flow,
    poll_onboarding_oauth_flow,
    start_onboarding_oauth_flow,
)

# Approval system -- state and helpers live in api.route_approvals; imported
# here for backward compatibility so existing call sites continue to resolve.
from api.route_approvals import (  # noqa: F401 — re-exports for backward compat
    _submit_pending_raw,
    approve_session,
    approve_permanent,
    save_permanent_allowlist,
    is_approved,
    _pending,
    _lock,
    _permanent_approved,
    _gateway_queues,
    resolve_gateway_approval,
    enable_session_yolo,
    disable_session_yolo,
    is_session_yolo_enabled,
    _approval_sse_subscribers,
    _approval_sse_subscribe,
    _approval_sse_unsubscribe,
    _approval_sse_notify_locked,
    _approval_sse_notify,
    _GATEWAY_MIRROR_FLAG,
    _gateway_mirrored_pending_run_id,
    reconcile_gateway_pending_mirror_locked,
    submit_gateway_pending_mirror,
    submit_pending,
)

# Clarify prompts (optional -- graceful fallback if agent not available)
try:
    from api.clarify import (
        submit_pending as submit_clarify_pending,
        get_pending as get_clarify_pending,
        pending_count as get_clarify_pending_count,
        resolve_clarify,
        resolve_clarify_by_id,
        sse_subscribe as clarify_sse_subscribe,
        sse_unsubscribe as clarify_sse_unsubscribe,
    )
except ImportError:
    submit_clarify_pending = lambda *a, **k: None
    get_clarify_pending = lambda *a, **k: None
    get_clarify_pending_count = lambda *a, **k: 0
    clarify_sse_subscribe = None
    resolve_clarify = lambda *a, **k: 0
    resolve_clarify_by_id = lambda *a, **k: False




from api.routes_parts import login as _login_routes_part
from api.routes_parts.login import (  # noqa: F401 - compatibility facade re-exports
    _LOGIN_LOCALE,
    _LOGIN_PAGE_HTML,
    _oidc_login_html,
    _request_base_url,
    _resolve_login_locale_key,
    _safe_login_redirect_path,
)

_install_routes_part(globals(), _login_routes_part)

from api.http.observability import (
    handle_logs as _handle_logs,
    normalize_logs_tail as _normalize_logs_tail,
)

# ── LLM Wiki status and filesystem owner ──────────────────────────────────────

from api.routes_parts.llm_wiki import (  # noqa: F401 - compatibility facade re-exports
    _LLM_WIKI_DOCS_URL,
    _LLM_WIKI_PAGE_DIRS,
    _LLM_WIKI_MAX_FILES,
    _LLM_WIKI_MAX_PAGE_BYTES,
    _LLM_WIKI_FORBIDDEN_ROOTS,
    _WIKI_ALLOWLIST_TTL,
    _wiki_allowlist_cache,
    _wiki_allowlist_cache_lock,
    _llm_wiki_active_hermes_home,
    _llm_wiki_env_file_path,
    _llm_wiki_get_config_path_value,
    _llm_wiki_config_path,
    _llm_wiki_resolve_path,
    _llm_wiki_safe_iso,
    _llm_wiki_count_files,
    _llm_wiki_page_files_cache_signature,
    _llm_wiki_page_files_uncached,
    _llm_wiki_page_files,
    _llm_wiki_clear_page_files_cache,
    _llm_wiki_allowlisted_entries,
    _llm_wiki_status_file_entry_stat,
    _llm_wiki_verified_status_file_stat,
    _llm_wiki_last_writer,
    _build_llm_wiki_status,
    _handle_llm_wiki_status,
    _handle_llm_wiki_browse,
    _handle_llm_wiki_page,
)


from api.http.observability import handle_insights as _handle_insights
from api.http.project_os import (
    candidate_repo_roots as _project_os_candidate_repo_roots,
    goal_summary as _project_os_goal_summary,
    handle_dashboard as _handle_project_os_dashboard,
    onboarding_context as _project_os_onboarding_context,
    repo_matches_board as _project_os_repo_matches_board,
    resolve_repo_root_for_board as _project_os_resolve_repo_root_for_board,
    truth_board_slugs as _project_os_truth_board_slugs,
    workspace_json as _project_os_workspace_json,
    workspace_read as _project_os_workspace_read,
)

# ── GET routes ────────────────────────────────────────────────────────────────


from api.http.observability import (
    accept_loop_health as _accept_loop_health,
    deep_health_checks as _deep_health_checks,
    handle_health as _handle_health,
    run_lifecycle_health as _run_lifecycle_health,
    stream_runtime_diagnostics as _stream_runtime_diagnostics,
    streams_lock_health as _streams_lock_health,
)
from api.http.plugins import (
    clean_visibility_text as _clean_plugin_visibility_text,
    dashboard_plugin_enabled as _dashboard_plugin_enabled,
    get_plugin_manager_for_visibility as _get_plugin_manager_for_visibility,
    handle_plugins as _handle_plugins,
    selected_provider as _plugin_visibility_selected_provider,
    visibility_category_from_key as _plugin_visibility_category_from_key,
    visibility_payload as _plugin_visibility_payload,
    webui_plugin_payload as _webui_plugin_payload,
)
from api.http.shell import (
    handle_health_restart as _handle_health_restart,
    handle_shutdown as _handle_shutdown,
    load_saved_prompts as _load_saved_prompts,
    render_index_shell_base as _render_index_shell_base,
    save_saved_prompts as _save_saved_prompts,
    saved_prompts_path as _saved_prompts_path,
    serve_manifest as _serve_manifest,
    serve_unavailable as _serve_shell_unavailable,
    shutdown_log_value as _shutdown_log_value,
)


def handle_get(handler, parsed) -> bool:
    """Dispatch GET requests through the HTTP composition root."""
    from api.http.router import handle_get as dispatch_get

    return dispatch_get(handler, parsed, globals())


# ── POST auth helpers

def _require_passkey_registration_auth(handler) -> tuple[bool, str, int]:
    """Require auth, or the existing local-only first-run bootstrap gate.

    Registering additional passkeys is an auth-factor enrollment action and
    requires a valid WebUI session.  The first passkey can still bootstrap a
    passkey-only instance, but only through the same local/private-network
    onboarding gate used for first password setup.
    """
    from api.auth import is_auth_enabled, parse_cookie, verify_session

    auth_enabled = is_auth_enabled()
    if not auth_enabled:
        if _onboarding_gate_allows(handler, auth_enabled):
            return True, "", 200
        return False, "Authentication required", 401
    cookie_val = parse_cookie(handler)
    if not cookie_val or not verify_session(cookie_val):
        return False, "Authentication required", 401
    return True, "", 200

def _validate_session_toolsets_shape(toolsets):
    """Validate per-session toolset override shape without catalog lookup."""
    if toolsets is None:
        return None
    if not isinstance(toolsets, list) or not toolsets:
        raise ValueError("toolsets must be a non-empty list or null")
    if not all(isinstance(t, str) and t for t in toolsets):
        raise ValueError("each toolset must be a non-empty string")
    return toolsets

def handle_post(handler, parsed) -> bool:
    """Dispatch POST requests through the HTTP composition root."""
    from api.http.router import handle_post as dispatch_post

    return dispatch_post(handler, parsed, globals())


def handle_patch(handler, parsed) -> bool:
    """Dispatch PATCH requests through the HTTP composition root."""
    from api.http.router import handle_patch as dispatch_patch

    return dispatch_patch(handler, parsed, globals())


def handle_delete(handler, parsed) -> bool:
    """Dispatch DELETE requests through the HTTP composition root."""
    from api.http.router import handle_delete as dispatch_delete

    return dispatch_delete(handler, parsed, globals())


def handle_put(handler, parsed) -> bool:
    """Dispatch PUT requests through the HTTP composition root."""
    from api.http.router import handle_put as dispatch_put

    return dispatch_put(handler, parsed, globals())

from api.http import static_assets as _static_assets_http
from api.http.static_assets import (
    _STATIC_MIME,
    _TEXT_MIME_TYPES,
    _COMPRESSIBLE_MIME,
    _STATIC_CACHE,
    _STATIC_CACHE_LOCK,
    _serve_static,
)

_install_routes_part(globals(), _static_assets_http)
del _static_assets_http


from api.http import session_export_search as _session_export_search_http
from api.http.session_export_search import (
    _handle_session_export,
    _session_search_message_text,
    _session_search_preview,
    _handle_sessions_search,
)

_install_routes_part(globals(), _session_export_search_http)
del _session_export_search_http


from api.http import workspace_navigation as _workspace_navigation_http
from api.http.workspace_navigation import (
    _handle_list_dir,
    _read_json_request_body,
    _handle_escape_authorize,
    _handle_escape_list_dir,
    _handle_escape_file_read,
    _handle_escape_file_raw,
)

_install_routes_part(globals(), _workspace_navigation_http)
del _workspace_navigation_http


from api.routes_parts import stream_transport as _stream_transport_routes_part
from api.routes_parts.stream_transport import (
    _sse_with_id,
    _session_events_path_session_id,
    _session_events_resume_event_id,
    _session_snapshot_payload,
    _parse_run_journal_event_id,
    _parse_run_journal_after_seq,
    _replay_run_journal,
    _run_journal_same_run_seq,
    _run_journal_covers_offline_gap,
    _sse_replay_run_journal_gap_checked,
    _sse_offline_gap_recovery,
    _runner_stream_cursor_from_query,
    _runner_event_name,
    _runner_event_payload,
    _runner_event_id,
    _stream_runner_run_events,
    _handle_sse_stream,
    _handle_session_run_journal_stream_for_session,
    _handle_session_sse_stream_for_session,
    _gateway_sse_probe_payload,
    _handle_gateway_sse_stream,
    _handle_session_events_stream,
)

_install_routes_part(globals(), _stream_transport_routes_part)
del _stream_transport_routes_part


from api import terminal as _terminal_domain
from api.routes_parts import terminal as _terminal_http


_REMOTE_TERMINAL_BACKEND_UNSUPPORTED_ERROR = (
    _terminal_domain.REMOTE_BACKEND_UNSUPPORTED_ERROR
)
_REMOTE_TERMINAL_BACKEND_UNSUPPORTED_MESSAGE = (
    _terminal_domain.REMOTE_BACKEND_UNSUPPORTED_MESSAGE
)


def _terminal_session_lookup(body_or_query):
    """Compatibility adapter for callers that still import this route helper."""
    return _terminal_domain.lookup_terminal_session(body_or_query.get("session_id"))


def _terminal_remote_backend_enabled() -> bool:
    return _terminal_domain.terminal_remote_backend_enabled()


def _handle_terminal_start(handler, body):
    return _terminal_http.handle_terminal_start(
        handler,
        body,
        gate=_embedded_terminal_gate_allows,
        gate_denied_message=_EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE,
    )


def _handle_terminal_input(handler, body):
    return _terminal_http.handle_terminal_input(
        handler,
        body,
        gate=_embedded_terminal_gate_allows,
        gate_denied_message=_EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE,
    )


def _handle_terminal_resize(handler, body):
    return _terminal_http.handle_terminal_resize(
        handler,
        body,
        gate=_embedded_terminal_gate_allows,
        gate_denied_message=_EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE,
    )


def _handle_terminal_close(handler, body):
    return _terminal_http.handle_terminal_close(
        handler,
        body,
        gate=_embedded_terminal_gate_allows,
        gate_denied_message=_EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE,
    )


def _handle_terminal_output(handler, parsed):
    return _terminal_http.handle_terminal_output(
        handler,
        parsed,
        gate=_embedded_terminal_gate_allows,
        gate_denied_message=_EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE,
        heartbeat_seconds=_SSE_HEARTBEAT_INTERVAL_SECONDS,
        send_event=_sse,
        set_write_deadline=_sse_set_write_deadline,
    )




from api.routes_parts import media_files as _media_http
from api.routes_parts.media_files import (
    _content_disposition_value,
    _parse_range_header,
    _open_file_read_fd,
    _close_fd_quietly,
    _serve_file_bytes,
    _html_preview_with_blank_base,
    _serve_inline_html_preview,
    _MEDIA_TOKEN_RE,
    _message_content_text,
    _session_media_token_allows_path,
    _session_media_token_allows_image_path,
    _path_is_within_root,
    _handle_media,
    _file_raw_target,
    _folder_zip_max_bytes,
    _folder_zip_max_files,
    _folder_download_collect,
    _handle_folder_download,
    _handle_file_raw,
    _handle_file_read,
    _read_anchored_file_bytes,
)


def _serve_file_bytes(
    handler,
    target,
    mime,
    disposition,
    cache_control,
    *,
    csp=None,
    anchor_root=None,
):
    return _media_http._serve_file_bytes(
        handler,
        target,
        mime,
        disposition,
        cache_control,
        csp=csp,
        anchor_root=anchor_root,
        anchored_open=open_anchored_fd,
    )


def _serve_inline_html_preview(
    handler,
    target,
    cache_control,
    *,
    csp,
    anchor_root=None,
):
    return _media_http._serve_inline_html_preview(
        handler,
        target,
        cache_control,
        csp=csp,
        anchor_root=anchor_root,
        anchored_open=open_anchored_fd,
    )


def _session_media_token_allows_path(session_id, target, allowed_mimes):
    return _media_http._session_media_token_allows_path(
        session_id,
        target,
        allowed_mimes,
        session_loader=get_session,
    )


def _session_media_token_allows_image_path(session_id, target, image_mimes):
    return _session_media_token_allows_path(session_id, target, image_mimes)


def _handle_media(handler, parsed):
    return _media_http._handle_media(
        handler,
        parsed,
        workspace_getter=get_last_workspace,
        session_loader=get_session,
        file_sender=_serve_file_bytes,
        html_sender=_serve_inline_html_preview,
    )


def _handle_folder_download(handler, parsed):
    return _media_http._handle_folder_download(
        handler,
        parsed,
        session_lookup=get_session_for_file_ops,
        anchored_open=open_anchored_fd,
    )


def _handle_file_raw(handler, parsed):
    return _media_http._handle_file_raw(
        handler,
        parsed,
        session_lookup=get_session_for_file_ops,
        file_sender=_serve_file_bytes,
        html_sender=_serve_inline_html_preview,
    )


def _handle_file_read(handler, parsed):
    return _media_http._handle_file_read(
        handler,
        parsed,
        session_lookup=get_session_for_file_ops,
        file_reader=read_file_content,
    )


from api.routes_parts import tts as _tts_routes_part
from api.routes_parts.tts import (  # noqa: F401 - compatibility facade re-exports
    _speech_api,
    _speech_normalize_tts_prosody,
    _speech_tts_addr_is_blocked,
    _speech_tts_host_is_blocked_target,
    _speech_tts_resolve_pinned_addresses,
    _speech_tts_resolve_pinned_address,
    _speech_normalized_openai_tts_base_url,
    _speech_buffer_tts_audio_response,
    _speech_tts_open,
    _TtsRateLimiter,
    _write_audio_response,
    _normalize_tts_prosody,
    _TTS_PROXY_MAX_BYTES,
    _TTS_LOCALHOST_HOSTS,
    _tts_addr_is_blocked,
    _tts_host_is_blocked_target,
    _tts_resolve_pinned_addresses,
    _tts_resolve_pinned_address,
    _normalized_openai_tts_base_url,
    _buffer_tts_audio_response,
    _NoRedirectTtsHandler,
    _PinnedHTTPSConnection,
    _PinnedHTTPSHandler,
    _tts_open,
    _handle_tts,
    _stt_provider_capability_from_module,
    _stt_provider_capability,
    handle_transcribe,
    handle_transcribe_capability,
)

_install_routes_part(globals(), _tts_routes_part)
del _tts_routes_part




from api.http import interactive_streams as _interactive_streams_http
from api.http.interactive_streams import (
    _handle_approval_pending,
    _handle_approval_sse_stream,
    _handle_approval_inject,
    _handle_clarify_pending,
    _handle_clarify_sse_stream,
    _handle_session_sse_stream,
    _handle_clarify_inject,
)

_install_routes_part(globals(), _interactive_streams_http)
del _interactive_streams_http


















from api.http import project_context as _project_context_http
from api.http.project_context import (
    candidates as _project_context_candidates,
    git_root as _project_context_git_root,
    handle_memory_read as _handle_memory_read,
    _handle_memory_write,
    read_active as _read_active_project_context,
    strip_frontmatter as _strip_project_context_frontmatter,
    workspace_for_request as _memory_project_context_workspace,
)

_install_routes_part(globals(), _project_context_http)
del _project_context_http

# ── POST route helpers ────────────────────────────────────────────────────────


from api.routes_parts import chat_runs as _chat_runs_routes_part
from api.routes_parts.chat_runs import (
    _handle_sessions_cleanup,
    _handle_btw,
    _handle_background,
    _active_run_stream_for_session,
    _agent_runtime_barrier_response,
    _start_chat_stream_for_session,
    _runtime_runner_client_factory,
    _chat_start_response_from_run_start,
    _runtime_adapter_goal_action,
    _start_run,
    _process_wakeup_revalidation_provider,
    _process_wakeup_provider_has_recovery_credential,
    _refresh_process_wakeup_pause_credential_fingerprint,
    start_session_turn,
    _handle_bg_task_complete_ack,
    _handle_session_compression_recovery_start,
    _handle_goal_command,
    _handle_chat_start,
    _resolve_chat_workspace_with_recovery,
    _normalize_chat_attachments,
    _handle_chat_sync,
)

_install_routes_part(globals(), _chat_runs_routes_part)
del _chat_runs_routes_part

# Compose the server-side run entry point after the route-owned compatibility
# function has been installed. The late global lookup preserves existing
# monkeypatch and reload behavior without making background domains import the
# HTTP facade.
from api.runs.server_turn import configure_start_session_turn

configure_start_session_turn(
    lambda session_id, message, *, source="process_wakeup": globals()[
        "start_session_turn"
    ](session_id, message, source=source)
)


















from api.routes_parts import git as _git_routes_part
from api.routes_parts.git import (  # noqa: F401 - compatibility facade re-exports
    _git_session,
    _git_session_workspace,
    _git_session_and_workspace,
    _git_locked_by_active_stream,
    _git_reject_destructive_if_unsafe,
    _handle_git_status,
    _handle_git_branches,
    _handle_git_diff,
    _git_bad,
    _git_paths_from_body,
    _handle_git_stage,
    _handle_git_unstage,
    _handle_git_discard,
    _llm_git_commit_message,
    _handle_git_commit_message,
    _handle_git_commit_message_selected,
    _handle_git_commit,
    _handle_git_commit_selected,
    _handle_git_remote_action,
    _handle_git_checkout,
    _handle_git_stash_checkout,
)

_install_routes_part(globals(), _git_routes_part)
del _git_routes_part

from api.routes_parts import workspace_files as _workspace_files_routes_part
from api.routes_parts.workspace_files import (  # noqa: F401 - compatibility facade re-exports
    _handle_file_delete,
    _handle_file_save,
    _handle_office_file_save,
    _handle_file_create,
    _handle_file_rename,
    _handle_file_move,
    _handle_create_dir,
    _handle_file_reveal,
    _handle_file_path,
    _handle_file_open_vscode,
)

_install_routes_part(globals(), _workspace_files_routes_part)
del _workspace_files_routes_part

from api.routes_parts import workspace_management as _workspace_management_routes_part
from api.routes_parts.workspace_management import (  # noqa: F401 - compatibility facade re-exports
    _handle_workspace_add,
    _handle_workspace_remove,
    _handle_workspace_rename,
    _handle_workspace_reorder,
)

_install_routes_part(globals(), _workspace_management_routes_part)
del _workspace_management_routes_part


from api.routes_parts import interactive_responses as _interactive_responses_routes_part
from api.routes_parts.interactive_responses import (  # noqa: F401 - compatibility facade re-exports
    _resolve_approval_legacy,
    _GATEWAY_APPROVAL_RELAY_UNAVAILABLE,
    _gateway_pending_approval_without_run_id,
    _session_has_pending_approval,
    _handle_approval_respond,
    _resolve_clarify_legacy,
    _handle_clarify_respond,
)

_install_routes_part(globals(), _interactive_responses_routes_part)
del _interactive_responses_routes_part

from api.routes_parts import manual_compression as _manual_compression_routes_part
from api.routes_parts.manual_compression import (  # noqa: F401 - compatibility facade re-exports
    _MANUAL_COMPRESSION_JOBS,
    _MANUAL_COMPRESSION_JOBS_LOCK,
    _MANUAL_COMPRESSION_JOB_TTL_SECONDS,
    _ManualCompressionMemoryHandler,
    _manual_compression_cleanup_locked,
    _manual_compression_status_payload,
    _run_manual_compression_job,
    _handle_session_compress_start,
    _handle_session_compress_status,
    _handle_session_compress,
)

_install_routes_part(globals(), _manual_compression_routes_part)
_ManualCompressionMemoryHandler.__module__ = __name__
del _manual_compression_routes_part


def _handle_conversation_rounds(handler, body):
    """Return conversation-round count for a gateway session.

    Request body::

        { "session_id": "...", "since": <unix_ts_or_iso> }

    Response::

        { "ok": true, "rounds": 12, "threshold": 10, "should_show": true }
    """
    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))

    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return bad(handler, "session_id is required")

    since = body.get("since")
    if since is not None:
        try:
            since = float(since)
        except (TypeError, ValueError):
            return bad(handler, "since must be a unix timestamp (number)")

    from api.sessions.store import count_conversation_rounds, CONVERSATION_ROUND_THRESHOLD

    rounds = count_conversation_rounds(sid, since=since)
    return j(handler, {
        "ok": True,
        "rounds": rounds,
        "threshold": CONVERSATION_ROUND_THRESHOLD,
        "should_show": rounds >= CONVERSATION_ROUND_THRESHOLD,
    })


from api.routes_parts import handoff_summary as _handoff_summary_routes_part
from api.routes_parts.handoff_summary import (  # noqa: F401 - compatibility facade re-exports
    _build_handoff_summary_tool_message,
    _extract_handoff_summary_payload,
    _is_matching_handoff_summary_message,
    _is_matching_handoff_summary_content,
    _persist_handoff_summary_locally,
    _persist_handoff_summary_to_state_db,
    _persist_handoff_summary,
    _handle_handoff_summary,
)

_install_routes_part(globals(), _handoff_summary_routes_part)
del _handoff_summary_routes_part
































from api.http import session_imports as _session_imports_http
from api.http.session_imports import (
    _normalize_message_for_import_refresh,
    _message_has_cli_tool_metadata,
    _strip_cli_tool_metadata_for_refresh,
    _is_cli_tool_metadata_enrichment,
    _is_messages_refresh_prefix_match,
    _request_wants_all_profiles_import,
    _normalize_import_profile_value,
    _handle_session_import_cli,
    _handle_session_import,
)

_install_routes_part(globals(), _session_imports_http)
del _session_imports_http


# ── MCP Server helpers ──
from api.config import get_config, _save_yaml_config_file, _get_config_path, reload_config

from api.routes_parts import mcp_inventory as _mcp_inventory_routes_part
from api.routes_parts.mcp_inventory import (  # noqa: F401 - compatibility facade re-exports
    _mask_secrets,
    _parse_mcp_enabled,
    _mcp_runtime_status_by_name,
    _server_summary,
    _mcp_safe_display_text,
    _mcp_schema_type,
    _mcp_schema_summary,
    _mcp_tool_schema_from_payload,
    _mcp_tool_summary,
    _mcp_tools_from_runtime_status,
    _mcp_tools_from_registry,
    _handle_mcp_tools_list,
    _handle_mcp_servers_list,
    _handle_mcp_server_delete,
    _handle_mcp_server_toggle,
    _MASKED_PLACEHOLDER,
    _strip_masked_values,
    _handle_mcp_server_update,
)

_install_routes_part(globals(), _mcp_inventory_routes_part)
del _mcp_inventory_routes_part

from api.routes_parts.notes_sources import (  # noqa: F401 - compatibility facade re-exports
    _webui_truthy,
    _external_notes_sources_enabled,
    _NOTES_SOURCE_SERVER_HINTS,
    _NOTES_SOURCE_TOOL_HINTS,
    _NOTES_SOURCE_CONFIGURED_TOOL_HINTS,
    _note_source_label,
    _looks_like_notes_source,
    _configured_note_tool_hints,
    _notes_sources_from_mcp_inventory,
    _handle_notes_sources_list,
    _notes_configured_server,
    _joplin_connection_from_config,
    _joplin_api_get,
    _note_snippet,
    _joplin_search_notes,
    _joplin_get_note,
    _JOPLIN_AI_RECALL_NOTE_PRIORITY,
    _script_path_from_config_value,
    _joplin_prefill_script_path,
    _joplin_recall_note_refs,
    _joplin_recent_ai_notes,
    _handle_notes_search,
    _handle_notes_item,
)
