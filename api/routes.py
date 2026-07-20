"""
Hermes Web UI -- Route handlers for GET and POST endpoints.
Extracted from server.py (Sprint 11) so server.py is a thin shell.
"""

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
from api.agent_sessions import (
    MESSAGING_SOURCES,
    _looks_like_default_cli_title,
    is_cli_session_row,
    is_cli_session_row_visible,
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
from api.gateway_restart import restart_active_profile_gateway
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


def _publish_session_list_changed(
    reason: str,
    *,
    profile: str | None = None,
    session_id: str | None = None,
) -> None:
    """Publish scoped session changes while tolerating legacy test doubles."""
    if not profile and not session_id:
        publish_session_list_changed(reason)
        return
    try:
        publish_session_list_changed(reason, profile=profile, session_id=session_id)
    except TypeError:
        # Some focused tests monkeypatch the route-level publisher with the
        # historical one-argument or profile-only shape. Preserve the old signal instead of
        # turning unrelated session mutations into 500s.
        if profile:
            try:
                publish_session_list_changed(reason, profile=profile)
                return
            except TypeError:
                pass
        publish_session_list_changed(reason)


def _sync_session_title_to_insights(session) -> None:
    """Write title-only session metadata updates through to state.db when enabled."""
    try:
        if not load_settings().get("sync_to_insights"):
            return
        from api.state_sync import sync_session_usage

        messages = getattr(session, "messages", None) or []
        sync_session_usage(
            session_id=session.session_id,
            input_tokens=getattr(session, "input_tokens", None) or 0,
            output_tokens=getattr(session, "output_tokens", None) or 0,
            estimated_cost=getattr(session, "estimated_cost", 0.0),
            model=getattr(session, "model", ""),
            title=session.title,
            message_count=len(messages),
            profile=getattr(session, "profile", None),
            cache_read_tokens=getattr(session, "cache_read_tokens", None) or 0,
            cache_write_tokens=getattr(session, "cache_write_tokens", None) or 0,
        )
    except Exception:
        logger.debug("Failed to update session title in state.db", exc_info=True)


def _persist_generated_session_title(
    session,
    next_title: str,
    *,
    event_reason: str,
    require_default_title: bool = False,
) -> str:
    normalized_title = str(next_title or "").strip()[:80] or "Untitled"
    sid = str(getattr(session, "session_id", "") or "")
    original_session = session
    should_save = False
    with edit_session(
        sid,
        touch_updated_at=False,
        save_when=lambda _session: should_save,
    ) as session:
        if getattr(session, "read_only", False):
            raise PermissionError(f"Session {sid} is read-only")
        if require_default_title:
            latest_meta = {
                "title": getattr(session, "title", None),
                "source_tag": getattr(session, "source_tag", None),
                "raw_source": getattr(session, "raw_source", None),
                "session_source": getattr(session, "session_source", None),
                "source_label": getattr(session, "source_label", None),
            }
            if not _looks_like_default_cli_title(latest_meta):
                return session.title
        session.title = normalized_title
        from api.sessions.operations import mark_session_title_generated

        # mark_session_title_generated sets s.llm_title_generated = True and clears manual_title.
        mark_session_title_generated(session)
        should_save = True
    _sync_session_title_to_insights(session)
    _publish_session_list_changed(
        event_reason,
        profile=getattr(session, "profile", None),
        session_id=sid,
    )
    if original_session is not session:
        original_session.title = session.title
        original_session.llm_title_generated = session.llm_title_generated
        original_session.manual_title = session.manual_title
    return session.title


def _queue_generated_title_for_imported_session(session, cli_meta: dict | None) -> None:
    try:
        cli_meta = dict(cli_meta or {})
        if not session or cli_meta.get("read_only") or not _looks_like_default_cli_title(cli_meta):
            return
        sid = str(getattr(session, "session_id", "") or "")
        if not sid:
            return

        def _run() -> None:
            try:
                current = Session.load(sid)
                if not current:
                    return
                current = get_full_session(sid, session=current)
                if getattr(current, "read_only", False):
                    return
                current_meta = {
                    "title": getattr(current, "title", None),
                    "source_tag": getattr(current, "source_tag", None),
                    "raw_source": getattr(current, "raw_source", None),
                    "session_source": getattr(current, "session_source", None),
                    "source_label": getattr(current, "source_label", None),
                }
                if not _looks_like_default_cli_title(current_meta):
                    return
                next_title, _reason, _raw_preview = generate_session_title_for_session(current)
                normalized_current = str(getattr(current, "title", "") or "").strip()
                normalized_next = str(next_title or "").strip()
                if not normalized_next or normalized_next == normalized_current:
                    return
                _persist_generated_session_title(
                    current,
                    normalized_next,
                    event_reason="session_title_regenerate",
                    require_default_title=True,
                )
            except Exception:
                logger.debug("Failed to generate imported session title for %s", sid, exc_info=True)

        threading.Thread(target=_run, daemon=True, name=f"imported-title-{sid}").start()
    except Exception:
        logger.debug(
            "Failed to queue imported session title generation for %s",
            getattr(session, "session_id", None),
            exc_info=True,
        )


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







def _session_field(session, field, default=None):
    if isinstance(session, dict):
        return session.get(field, default)
    return getattr(session, field, default)


def _session_counts_toward_pin_quota(session) -> bool:
    """Return True when a pinned session should consume visible pin quota."""
    if not _session_field(session, "pinned", False):
        return False
    if _session_field(session, "archived", False):
        return False
    if isinstance(session, dict):
        row = session
    elif hasattr(session, "compact"):
        row = session.compact()
    else:
        row = {
            "pre_compression_snapshot": _session_field(session, "pre_compression_snapshot", False),
            "source_tag": _session_field(session, "source_tag", None),
            "default_hidden": _session_field(session, "default_hidden", False),
        }
    return not _hide_from_default_sidebar(row)


def _session_row_lineage_root_id(session, sessions_by_id) -> str:
    sid = str(_session_field(session, "session_id", "") or "")
    explicit = _session_field(session, "_lineage_root_id", None)
    if explicit:
        return str(explicit)
    # A branch/fork is an independent, separately-visible session (it carries a
    # parent_session_id purely for provenance), so it must count as its OWN pin
    # lineage — only compression/continuation rows should collapse to a shared
    # root. Without this, two pinned forks of the same parent would collapse to a
    # single quota lineage and let the user exceed pinned_sessions_limit (#3288).
    if _session_field(session, "session_source", None) == "fork":
        return sid
    current = sid
    seen = {sid} if sid else set()
    parent = _session_field(session, "parent_session_id", None)
    while parent:
        parent = str(parent)
        if parent in seen:
            break
        current = parent
        seen.add(parent)
        parent_row = sessions_by_id.get(parent)
        if not parent_row:
            break
        parent = _session_field(parent_row, "parent_session_id", None)
    return current or sid


def _visible_pinned_lineage_ids(session_rows) -> set[str]:
    sessions_by_id = {}
    for row in session_rows:
        sid = str(_session_field(row, "session_id", "") or "")
        if sid:
            sessions_by_id[sid] = row
    roots: set[str] = set()
    for row in session_rows:
        if not _session_counts_toward_pin_quota(row):
            continue
        root = _session_row_lineage_root_id(row, sessions_by_id)
        if root:
            roots.add(root)
    return roots


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
    _is_root_profile,
    _SKILLS_STATS_CACHE,
    get_active_profile_name,
    get_active_profile_name as _get_active_profile_name,
    get_active_hermes_home,
    list_profiles_api,
    profile_scope_for_detached_worker,
)


def _all_profiles_query_flag(parsed_url) -> bool:
    """Return True if the request URL has `?all_profiles=1` (or true/yes).

    Centralizes the opt-in parsing so /api/sessions and /api/projects use
    the same shape. Accepts 1/true/yes (case-insensitive) for ergonomics.
    """
    qs = parse_qs(parsed_url.query)
    raw = qs.get('all_profiles', [''])[0].strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _all_profiles_enabled(parsed_url) -> bool:
    """Enable aggregate profile reads only when the request asks and mode allows it."""
    return _all_profiles_query_flag(parsed_url) and not _is_isolated_profile_mode()


def _query_flag(parsed_url, name: str) -> bool:
    """Return True for a truthy query flag value."""
    qs = parse_qs(parsed_url.query)
    raw = qs.get(name, [''])[0].strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _query_positive_int(parsed_url, name: str, *, default=None, maximum: int | None = None):
    """Return a non-negative integer query parameter, or default when absent/invalid."""
    qs = parse_qs(parsed_url.query)
    raw = qs.get(name, [''])[0]
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    if maximum is not None:
        value = min(value, int(maximum))
    return value


def _session_visible_to_active_profile(session_profile, handler=None) -> bool:
    """Return whether a detail-load session belongs to the active profile.

    Real request handlers must enforce the same profile boundary as
    /api/sessions, even when the request has no hermes_profile cookie and the
    process-level active profile is the default/root profile. Direct unit-callers
    without a request handler keep the historical metadata-load behavior.
    """
    if handler is None:
        return True
    active_profile = _get_active_profile_name()
    if not isinstance(session_profile, str):
        session_profile = None
    return _profiles_match(session_profile, active_profile)


def _request_session_visibility_exempt(method: str, path: str | None) -> bool:
    if not path:
        return False
    if method == "GET" and path == "/api/session":
        # Detail-load owns profile mismatch handling so the frontend can switch
        # to the session's profile instead of treating a valid cross-profile
        # deep link as a deleted/stale session.
        return True
    if method != "POST":
        return False
    # Import routes create/claim sessions before normal ownership exists, and
    # chat/start has inline placeholder-retag rules that must run before the
    # generic request-session guard.
    return path in {
        "/api/session/import",
        "/api/session/import_cli",
        "/api/chat/start",
    }


def _session_id_visible_to_request_profile(handler, sid, *, emit_error: bool = True) -> bool:
    """Return whether ``sid`` belongs to the active profile."""
    if not isinstance(sid, str) or not sid:
        return True
    if not is_safe_session_id(sid):
        return True
    try:
        session = get_session(sid, metadata_only=True)
    except KeyError:
        return True
    if not _session_visible_to_active_profile(getattr(session, "profile", None), handler):
        if emit_error:
            bad(handler, "Session not found", 404)
        return False
    return True


def _stream_id_owner_session_id(stream_id: str | None) -> str | None:
    """Resolve stream owner session_id via active-run registry first, fallback to journal."""
    stream_id = str(stream_id or "").strip()
    if not stream_id:
        return None
    try:
        owner = runtime_run_session_id(stream_id)
        if owner:
            return owner
    except Exception:
        logger.debug("Failed reading runtime owner for stream %s", stream_id, exc_info=True)
    if not is_safe_session_id(stream_id):
        return None
    try:
        summary = find_run_summary(stream_id)
        if isinstance(summary, dict):
            owner = str(summary.get("session_id") or "").strip()
            return owner or None
    except Exception:
        logger.debug("Failed reading run summary for stream %s", stream_id, exc_info=True)
    return None


def _stream_id_visible_to_request_profile(
    handler,
    stream_id: str | None,
    *,
    emit_error: bool = True,
) -> bool:
    """Return whether the stream owner is visible to the request's profile."""
    owner_session_id = _stream_id_owner_session_id(stream_id)
    if not owner_session_id:
        return True
    return _session_id_visible_to_request_profile(handler, owner_session_id, emit_error=emit_error)


def _guard_request_session_visibility(handler, parsed, body=None, method="GET") -> bool:
    """Apply request session-profile visibility check to request-supplied IDs.

    Covers top-level `session_id` in the query/body. Routes that accept session
    IDs under other keys must enforce their own visibility checks.
    """
    method = str(method).upper()
    if _request_session_visibility_exempt(method, getattr(parsed, "path", "")):
        return True
    sid = parse_qs(getattr(parsed, "query", "") or "").get("session_id", [None])[0]
    if not _session_id_visible_to_request_profile(handler, sid):
        return False
    if isinstance(body, dict) and not _session_id_visible_to_request_profile(handler, body.get("session_id")):
        return False
    return True


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
)

_install_routes_part(globals(), _skills_routes_part)
del _skills_routes_part


# ── SSE app-level heartbeat (#1623) ────────────────────────────────────────
#
# Kernel TCP keepalive (server.py setsockopt block) declares a peer dead at
# KEEPIDLE (10s) + KEEPINTVL (5s) * KEEPCNT (3) = 25s in the worst case. The
# app-level SSE heartbeat must fire well below that window so flaky-network
# probes never get the chance to kill an idle stream during long LLM thinking
# phases. 5s gives the kernel ~5x headroom: probe at 10s, heartbeat byte at
# every 5s of idle keeps the socket warm.
#
# Cost: ~12 bytes per heartbeat * 12 extra heartbeats/min = ~150B/min idle.
# Trivial; many production SSE deployments run 5-15s heartbeats specifically
# to handle proxies and mobile NAT.
_SSE_HEARTBEAT_INTERVAL_SECONDS = 5
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


def _callable_accepts_kwarg(callable_obj, kwarg_name: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    if kwarg_name in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _session_list_cache_key(
    active_profile: str | None,
    all_profiles: bool,
    show_cli_sessions: bool,
    show_previous_messaging_sessions: bool,
    show_cron_sessions: bool,
    include_archived: bool = False,
    exclude_hidden: bool = False,
    visible_only: bool = False,
    show_webhook_sessions: bool = False,
    source_filter: str | None = None,
    sidebar_source: str | None = None,
    archived_limit: int | None = None,
    archived_offset: int = 0,
    show_claude_code_sessions: bool = True,
) -> tuple:
    return _route_session_list_cache_key(
        active_profile=active_profile,
        all_profiles=all_profiles,
        show_cli_sessions=show_cli_sessions,
        show_previous_messaging_sessions=show_previous_messaging_sessions,
        show_cron_sessions=show_cron_sessions,
        include_archived=include_archived,
        exclude_hidden=exclude_hidden,
        visible_only=visible_only,
        show_webhook_sessions=show_webhook_sessions,
        source_filter=source_filter,
        sidebar_source=sidebar_source,
        archived_limit=archived_limit,
        archived_offset=archived_offset,
    ) + (bool(show_claude_code_sessions),)

_ROUTE_SESSION_LIST_CACHE_DYNAMIC_EXPORTS = {
    "_SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION",
    "_SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION",
    "_session_list_cache_settings_write_version",
}


def __getattr__(name):
    if name in _ROUTE_SESSION_LIST_CACHE_DYNAMIC_EXPORTS:
        return getattr(_route_session_list_cache, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _prune_orphaned_webui_zero_message_sessions(rows, *, diag_stage=None):
    """#4985 second-pass orphan prune for native-WebUI rows whose ``state.db.messages`` is empty.

    Takes the post-``#3238`` ``webui_sessions`` list (i.e. rows that already
    survived the #3238/#4591 CLI/API-server prune) and returns a NEW list with
    any row whose backing ``state.db.messages`` table is empty removed.
    Removed sids are also persisted to the tombstone via
    ``_record_webui_zero_message_orphan_tombstone`` so
    ``recover_missing_index_sidecars`` does not re-add them to the sidebar
    index on the next poll (avoids the cache-thrash loop where every poll
    does one fsync'd index write + one state.db probe per orphan, forever).

    Invariants preserved:

    - Rows with ``active_stream_id`` / ``has_pending_user_message`` /
      ``worktree_path`` set are NEVER pruned — the inflight / worktree-bound
      / pending safety contract from ``IC_kwDOR1LuPM8AAAABHrkF1Q``.
    - Rows whose ``state.db.messages`` is empty AND that survived the
      upstream ``all_sessions()`` ``#1171`` keep-filter (i.e. titled OR has
      positive ``message_count``) ARE pruned — the post-#1171-survivor
      shape #4985 actually describes (a row that lingers VISIBLY in the
      sidebar because of a stale positive ``message_count`` or a title set
      before the first turn committed).

    This helper is intentionally extracted out of the ``if show_cli_sessions:``
    branch so the prune fires in BOTH branches of
    ``_build_session_list_cache_payload``. Established installs have
    ``settings.show_cli_sessions`` pinned to ``False`` (per
    ``api/config.py:7637-7648``) and those are exactly the long-time users
    who have accumulated the #4985 404 orphans — without hoisting, the
    ``else:`` branch silently skipped the prune and the sidebar kept
    dangling rows that 404 on click (review
    ``IC_kwDOR1LuPM8AAAABHsyFGg``).
    """
    if not rows:
        return list(rows) if rows is not None else []
    _diag = diag_stage if callable(diag_stage) else (lambda *_a, **_k: None)
    # #4985 self-healing: the tombstone is NOT a blind-drop filter at the
    # top of the helper. A row whose sid is in the tombstone is allowed
    # into the gate predicate like any other row — and the post-probe
    # logic below explicitly distinguishes four cases:
    #
    #   1. probe says NOT empty AND sid IS tombstoned → SELF-HEAL: the row
    #      has actually gained messages, so clear the tombstone and keep
    #      the row (do NOT add to missing_webui_orphan_ids). This is the
    #      primary fix for review IC_kwDOR1LuPM8AAAABHvY-dw.
    #   2. probe says empty AND sid IS tombstoned → tombstone persists
    #      (orphan shape unchanged), but the row is excluded from the
    #      returned list so the tombstone continues to suppress it on
    #      this poll too. Do NOT redundantly prune+tombstone (would
    #      cycle).
    #   3. probe says empty AND sid is NOT tombstoned → new orphan: prune
    #      from index, record tombstone, diag_stage.
    #   4. probe says NOT empty AND sid is NOT tombstoned → row has
    #      messages, retain (gate already passes anyway).
    #
    # A blind-drop at the top (the previous behavior) is strictly worse
    # than the orphan it suppresses — it would silently swallow a
    # legitimately-resurfaced row forever, even after the user actually
    # sent messages. The self-healing case is what makes the tombstone a
    # recoverable "this sid is currently empty" signal rather than a
    # permanent hide-list.
    if not rows:
        return []
    # Gate predicate mirrors the inline block that lived here before the
    # helper extract. The (title!='Untitled' OR count>0) clause is what makes
    # this gate actually reach a row #1171 kept — without it, the gate is a
    # no-op because ``all_sessions()`` in the session store (and
    # its full-scan fallback at 3946-3952) has already stripped every
    # (Untitled ∧ count==0 ∧ ¬active_stream_id ∧ ¬has_pending_user_message ∧
    # ¬worktree_path) row before our prune block runs.
    _webui_orphan_probe_rows = [
        s for s in rows
        if _session_source_is_webui(s)
        and not s.get("active_stream_id")
        and not s.get("has_pending_user_message")
        and not s.get("worktree_path")
        and (
            s.get("title", "Untitled") != "Untitled"
            or _numeric_count(s.get("message_count")) > 0
        )
    ]
    if not _webui_orphan_probe_rows:
        return list(rows)
    rows_by_profile_webui: dict[object, list[dict]] = defaultdict(list)
    for row in _webui_orphan_probe_rows:
        rows_by_profile_webui[row.get("profile")].append(row)
    _tombstoned = _load_webui_zero_message_orphan_tombstone()
    self_healed_ids: set[str] = set()
    missing_webui_orphan_ids: set[str] = set()
    still_hidden_ids: set[str] = set()
    for profile_key, profile_rows in rows_by_profile_webui.items():
        probe_ids = [
            str(row.get("session_id")).strip()
            for row in profile_rows
            if str(row.get("session_id") or "").strip()
        ]
        zero_message_sids = agent_session_zero_message_sids(
            probe_ids,
            profile=profile_key if isinstance(profile_key, str) and profile_key else None,
        )
        # Iterate over the actual rows (not just probe_ids) so each sid
        # decision can probe the sidecar for real ``messages``. The r5
        # signal keyed off the row's cached ``message_count`` (which is
        # stale-positive on the very phantom rows #4985 exists to prune:
        # sidecar ``messages`` empty but cached count > 0), so the r5
        # retain branch kept the phantom and re-opened the bug (maintainer
        # review 4584722701, supersedes the r5 cached-count signal). The
        # r6 signal probes ``Session.load(sid).messages`` directly — but
        # ONLY for ``state.db``-empty candidates (the small set; the
        # common live-row path takes the ``else`` branch and pays
        # nothing). Full ``Session.load`` is intentional (vs
        # ``load_metadata_only`` which zeroes the messages array at
        # the session store's projection filter.
        for row in profile_rows:
            sid = str(row.get("session_id") or "").strip()
            if not sid:
                continue
            is_empty = sid in zero_message_sids
            is_tombstoned = sid in _tombstoned
            if is_empty:
                # ``state.db.messages`` is empty. Probe the sidecar JSON
                # for real messages — the cached ``message_count`` alone
                # is stale-positive on phantom rows (sidecar ``messages``
                # empty but cached count > 0) and would retain the very
                # phantom this feature exists to prune (maintainer review
                # 4584722701, supersedes the r5 cached-count signal).
                # Full ``Session.load`` is intentional (vs
                # ``load_metadata_only`` which zeros the messages array
                # in the session store); the common live-row path
                # pays nothing because it skips the load via the
                # ``else`` branch below.
                try:
                    from api.sessions.store import Session as _Session
                    _loaded = _Session.load(sid)
                    sidecar_has_messages = bool(
                        _loaded is not None and len(_loaded.messages or []) > 0
                    )
                except Exception:
                    logger.debug(
                        "Failed to load sidecar for webui orphan decision %s; "
                        "treating as empty for prune purposes",
                        sid,
                        exc_info=True,
                    )
                    sidecar_has_messages = False
            else:
                # ``state.db.messages`` is non-empty — the conversation is real.
                sidecar_has_messages = True
            if sidecar_has_messages:
                # Real transcript (state.db OR loaded sidecar). Retain; if
                # tombstoned, self-heal so it stops thrashing on recovery.
                if is_tombstoned:
                    self_healed_ids.add(sid)
                continue
            if not is_empty and is_tombstoned:
                # Case 1: SELF-HEAL — clear tombstone, keep row.
                self_healed_ids.add(sid)
            elif is_empty and is_tombstoned:
                # Case 2: still-empty tombstoned row stays hidden this
                # poll (do not add to missing_webui_orphan_ids — would
                # cycle through prune_session_from_index + record).
                still_hidden_ids.add(sid)
            elif is_empty and not is_tombstoned:
                # Case 3: new orphan.
                missing_webui_orphan_ids.add(sid)
            # Case 4 (not empty + not tombstoned): row has messages, retain.
    if self_healed_ids:
        for _sid in self_healed_ids:
            try:
                _clear_webui_zero_message_orphan_tombstone(_sid)
                logger.debug(
                    "self-heal: cleared webui zero-message orphan tombstone "
                    "for %s (state.db.messages now non-empty)",
                    _sid,
                )
            except Exception:
                logger.debug(
                    "Failed to clear webui zero-message orphan tombstone for %s",
                    _sid,
                    exc_info=True,
                )
        _diag("self_heal_webui_zero_message_orphan")
    if missing_webui_orphan_ids:
        for _sid in missing_webui_orphan_ids:
            try:
                prune_session_from_index(_sid)
                _diag("prune_orphaned_webui_zero_message")
            except Exception:
                logger.debug(
                    "Failed to prune orphaned webui zero-message row %s",
                    _sid,
                    exc_info=True,
                )
            # Tombstone the sid in a SECOND step so a tombstone-write failure
            # never blocks the prune itself (the prune still removes the row
            # from the sidebar; only the re-prune avoidance would degrade).
            try:
                _record_webui_zero_message_orphan_tombstone(_sid)
            except Exception:
                logger.debug(
                    "Failed to tombstone webui zero-message orphan %s",
                    _sid,
                    exc_info=True,
                )
    # Return rows excluding both the freshly-pruned orphans AND the
    # tombstoned rows that the probe confirmed are still empty (case 2).
    # Self-healed rows (case 1) and live rows (case 4) stay in the result.
    _hidden = missing_webui_orphan_ids | still_hidden_ids
    return [
        s for s in rows
        if str(s.get("session_id") or "").strip() not in _hidden
    ]


def _build_session_list_cache_payload(
    active_profile: str | None,
    all_profiles: bool,
    show_cli_sessions: bool,
    show_previous_messaging_sessions: bool,
    show_cron_sessions: bool,
    show_claude_code_sessions: bool = True,
    include_archived: bool = False,
    exclude_hidden: bool = False,
    visible_only: bool = False,
    show_webhook_sessions: bool = False,
    source_filter: str | None = None,
    sidebar_source: str | None = None,
    archived_limit: int | None = None,
    archived_offset: int = 0,
    diag=None,
) -> dict:
    diag_stage = diag.stage if diag is not None else lambda *_a, **_k: None

    def _session_has_server_visible_messages(session: dict) -> bool:
        """Return True when a non-active sidebar row has a visibility signal.

        Keep this mirror of the non-active server filter narrow and local to
        route behavior so model-layer behavior remains unchanged.
        """
        if not isinstance(session, dict):
            return False
        if _numeric_count(session.get("message_count")) > 0:
            return True

        attention = session.get("attention")
        if not (isinstance(attention, dict) and attention.get("kind")):
            attention = _session_attention_summary(str(session.get("session_id") or ""))
        if isinstance(attention, dict) and attention.get("kind"):
            if _numeric_count(attention.get("count")) > 0:
                return True

        return bool(
            session.get("is_streaming")
            or session.get("active_stream_id")
            or session.get("pending_user_message")
            or session.get("has_pending_user_message")
        )

    def _all_sessions_for_sidebar():
        if _callable_accepts_kwarg(all_sessions, "include_lineage_metadata"):
            return all_sessions(diag=diag, include_lineage_metadata=False)
        # Focused tests and third-party callers sometimes monkeypatch
        # routes.all_sessions with the historical diag-only signature.
        return all_sessions(diag=diag)

    diag_stage("all_sessions")
    webui_sessions = _all_sessions_for_sidebar()
    diag_stage("reconcile_stale_stream_state")
    if _reconcile_stale_stream_state_for_session_rows(webui_sessions):
        diag_stage("all_sessions_after_stale_stream_reconcile")
        webui_sessions = _all_sessions_for_sidebar()
    diag_stage("normalize_cli_rows")
    show_cli_sessions = bool(show_cli_sessions)
    show_previous_messaging_sessions = bool(show_previous_messaging_sessions)
    show_cron_sessions = bool(show_cron_sessions)
    show_webhook_sessions = bool(show_webhook_sessions)
    webui_sessions = [_normalize_sidebar_source_flags(s) for s in webui_sessions]
    if show_cli_sessions:
        diag_stage("get_cli_sessions")
        if _callable_accepts_kwarg(get_cli_sessions, "include_claude_code"):
            cli = get_cli_sessions(
                source_filter=source_filter,
                all_profiles=all_profiles,
                include_claude_code=show_claude_code_sessions,
            )
        else:
            # Focused tests sometimes monkeypatch routes.get_cli_sessions with
            # the historical two-keyword signature.
            cli = get_cli_sessions(
                source_filter=source_filter,
                all_profiles=all_profiles,
            )
        diag_stage("merge_cli_sessions")
        cli_by_id = {s["session_id"]: s for s in cli}
        # #3238/#4591: reconcile orphaned imported sidecars. When a CLI or
        # API-server session is clicked in WebUI it gets a WebUI-owned sidecar
        # that all_sessions() returns independently of state.db. If the user
        # later deletes the backing agent session outside WebUI, the sidecar is
        # never pruned and the stale row lingers in the sidebar forever (there
        # is no WebUI delete affordance for read-only imported rows).
        # Drop rows whose backing agent row is genuinely gone. We probe
        # state.db directly (agent_session_rows_existing) rather than trust
        # cli_by_id absence, because get_cli_sessions() caps at
        # CLI_VISIBLE_SESSION_LIMIT (20) — an existing session can fall
        # out of that window and look deleted. Native WebUI sessions
        # (source == "webui") that merely have a CLI ancestor are never
        # pruned by this path.
        #
        # #4985: parallel pass for native-WebUI rows that have a backing
        # agent row in state.db but zero messages (a `+`-click that opened a
        # row but the first turn never committed, or a sidebar nav that
        # opened then closed before any message landed). The same #3238
        # helper doesn't catch these because source == "webui" is excluded
        # above, and the WebUI delete affordance isn't exposed for them,
        # so they would otherwise linger forever. Inflight first-turn
        # safety is preserved by gating on `active_stream_id` (after
        # _reconcile_stale_stream_state has cleared stale stream ids).
        _orphan_probe_rows = []
        _kept_after_orphan_prune = []
        for s in webui_sessions:
            _sid = s.get("session_id")
            if (
                _sid
                and (is_cli_session_row(s) or _is_api_server_sidecar_row(s))
                and not _session_source_is_webui(s)
                and _sid not in cli_by_id
            ):
                _orphan_probe_rows.append(s)
            else:
                _kept_after_orphan_prune.append(s)
        if _orphan_probe_rows:
            rows_by_profile: dict[object, list[dict]] = defaultdict(list)
            for row in _orphan_probe_rows:
                rows_by_profile[row.get("profile")].append(row)
            missing_orphan_ids: set[str] = set()
            for profile_key, rows in rows_by_profile.items():
                probe_ids = [
                    str(row.get("session_id")).strip()
                    for row in rows
                    if str(row.get("session_id") or "").strip()
                ]
                existing = agent_session_rows_existing(
                    probe_ids,
                    profile=profile_key if isinstance(profile_key, str) and profile_key else None,
                )
                for row in rows:
                    _sid = str(row.get("session_id") or "").strip()
                    if _sid and _sid not in existing:
                        missing_orphan_ids.add(_sid)
            for s in _orphan_probe_rows:
                _sid = str(s.get("session_id") or "").strip()
                if _sid in missing_orphan_ids:
                    try:
                        prune_session_from_index(_sid)
                    except Exception:
                        logger.debug(
                            "Failed to prune orphaned agent sidecar %s",
                            _sid,
                            exc_info=True,
                        )
                    diag_stage("prune_orphaned_agent_sidecar")
                    continue
                _kept_after_orphan_prune.append(s)
        # #4985 second pass — probe state.db.messages for native-WebUI rows
        # that *survived* the upstream all_sessions() #1171 keep-filter (so
        # the row is TITLED or has a POSITIVE message_count, meaning it IS
        # shown in the sidebar — and the 404 click reported in #4985 happens),
        # BUT whose actual state.db.messages table is empty (the ground-truth
        # probe). This is the orphan shape #4985 actually describes: a row
        # that lingers VISIBLY in the sidebar because of a stale positive
        # message_count or a title set before the first turn committed.
        #
        # The (title!='Untitled' OR count>0) clause is the part that makes
        # this gate actually reach a row #1171 kept. Without it, the gate is
        # a no-op because all_sessions() in the session store and
        # 3946-3952 has already stripped every (Untitled ∧ count==0 ∧
        # ¬active_stream_id ∧ ¬has_pending_user_message ∧ ¬worktree_path)
        # row before this point — making the earlier 6-condition gate a
        # no-op against the real pipeline (review IC_kwDOR1LuPM8AAAABHrkF1Q).
        #
        # Implementation lives in ``_prune_orphaned_webui_zero_message_sessions``
        # above so the prune runs in BOTH branches of this function
        # (``if show_cli_sessions:`` AND ``else:``). Established installs
        # have ``settings.show_cli_sessions`` pinned to False (per
        # api/config.py:7637-7648) and those are exactly the long-time
        # users who accumulated the #4985 404 orphans — without hoisting,
        # the ``else:`` branch silently skipped the prune
        # (review IC_kwDOR1LuPM8AAAABHsyFGg).
        #
        # Inflight / worktree / pending safety: same as before — any row
        # still carrying active_stream_id / has_pending_user_message /
        # worktree_path is never pruned, even if its messages table is
        # momentarily empty. _reconcile_stale_stream_state_for_session_rows
        # at line 2224 has already cleared stale stream ids above this point.
        webui_sessions = _prune_orphaned_webui_zero_message_sessions(
            _kept_after_orphan_prune,
            diag_stage=diag_stage,
        )
        for s in webui_sessions:
            meta = cli_by_id.get(s.get("session_id"))
            if not meta:
                continue
            if _is_messaging_session_record(meta):
                s.update(_merge_cli_sidebar_metadata(s, meta))
                if s.get("session_id") != meta.get("session_id"):
                    s["session_id"] = meta.get("session_id")
            else:
                for key in ("source_tag", "raw_source", "session_source", "source_label"):
                    if not s.get(key) and meta.get(key):
                        s[key] = meta[key]
        webui_sessions = [_normalize_sidebar_source_flags(s) for s in webui_sessions]
        # Apply the same CLI visibility semantics to imported local copies so
        # low-value imported artifacts do not leak into the sidebar.
        webui_sessions = [s for s in webui_sessions if is_cli_session_row_visible(s)]
        represented_webui_ids = set()
        for s in webui_sessions:
            represented_webui_ids.update(_session_lineage_ids(s))
        deduped_cli = _dedupe_cli_sidebar_sessions_for_api(
            cli,
            represented_webui_ids,
            show_cron_sessions=show_cron_sessions,
            show_webhook_sessions=show_webhook_sessions,
        )
    else:
        diag_stage("filter_webui_sessions")
        webui_sessions = [s for s in webui_sessions if not _is_cli_session_for_settings(s)]
        # #4985 second pass — see _prune_orphaned_webui_zero_message_sessions
        # for the gate predicate and the post-#1171-survivor rationale. The
        # prune MUST run here too: established installs have
        # ``settings.show_cli_sessions`` pinned to False
        # (api/config.py:7637-7648) and those are exactly the long-time
        # users who accumulated the 404 orphans — review
        # IC_kwDOR1LuPM8AAAABHsyFGg. Without this call the else branch
        # silently skipped the prune and the sidebar kept dangling rows.
        webui_sessions = _prune_orphaned_webui_zero_message_sessions(
            webui_sessions,
            diag_stage=diag_stage,
        )
        deduped_cli = []
    diag_stage("sort_sessions")
    merged = webui_sessions + deduped_cli
    merged.sort(
        key=lambda s: s.get("last_message_at") or s.get("updated_at", 0) or 0,
        reverse=True,
    )
    # ── Profile scoping (#1611) ────────────────────────────────────────
    # Default: filter to the active profile. ?all_profiles=1 opts into
    # the aggregate view used by the "All profiles" sidebar toggle.
    # The other_profile_count is always returned so the UI can render
    # the "Show N from other profiles" affordance without sending the
    # cross-profile rows by default.
    #
    # IMPORTANT: scope BEFORE _keep_latest_messaging_session_per_source.
    # _messaging_source_key is profile-blind (#1614 follow-up): if the
    # same Slack/Telegram identity has sessions in profiles A and B, a
    # profile-blind dedupe would discard the older one even when scoped
    # to its own profile, leaving that profile with zero rows for that
    # source. Filter first so the dedupe operates only within the active
    # profile's rows.
    diag_stage("profile_scope")
    if all_profiles:
        scoped = merged
        other_profile_count = 0
    else:
        scoped = [s for s in merged if _profiles_match(s.get("profile"), active_profile)]
        other_profile_count = 0 if _is_isolated_profile_mode() else len(merged) - len(scoped)
    diag_stage("messaging_dedupe")
    archived_scoped = _keep_latest_messaging_session_per_source(
        list(scoped),
        show_previous_messaging_sessions=show_previous_messaging_sessions,
    )
    visible_scoped = _keep_latest_messaging_session_per_source(
        [s for s in scoped if not s.get("archived")],
        show_previous_messaging_sessions=show_previous_messaging_sessions,
    )
    if show_cli_sessions:
        diag_stage("cli_cap")
        archived_scoped = _cap_recent_cli_sessions(archived_scoped, cli_cap=CLI_VISIBLE_SESSION_CAP)
        visible_scoped = _cap_recent_cli_sessions(visible_scoped, cli_cap=CLI_VISIBLE_SESSION_CAP)
    if visible_only:
        archived_scoped = [
            s for s in archived_scoped if _session_has_server_visible_messages(s)
        ]
        visible_scoped = [
            s for s in visible_scoped if _session_has_server_visible_messages(s)
        ]
    if exclude_hidden:
        archived_scoped = [s for s in archived_scoped if not s.get("default_hidden")]
        visible_scoped = [s for s in visible_scoped if not s.get("default_hidden")]
    archived_webui_count = sum(
        1 for s in archived_scoped
        if s.get("archived") and not _is_cli_session_for_settings(s)
    )
    archived_cli_count = sum(
        1 for s in archived_scoped
        if s.get("archived") and _is_cli_session_for_settings(s)
    )
    archived_count = archived_webui_count + archived_cli_count
    def _filter_sidebar_source(rows: list[dict]) -> list[dict]:
        if sidebar_source == "webui":
            return [s for s in rows if not _is_cli_session_for_settings(s)]
        if sidebar_source == "cli":
            return [s for s in rows if _is_cli_session_for_settings(s)]
        return list(rows)

    full_scoped_all_sources = archived_scoped if include_archived else visible_scoped
    webui_session_count = sum(
        1 for s in full_scoped_all_sources
        if not _is_cli_session_for_settings(s)
    )
    cli_session_count = sum(
        1 for s in full_scoped_all_sources
        if _is_cli_session_for_settings(s)
    )
    visible_scoped_filtered = _filter_sidebar_source(visible_scoped)
    archived_scoped_filtered = _filter_sidebar_source(archived_scoped)
    scoped = _filter_sidebar_source(full_scoped_all_sources)
    if include_archived and archived_limit is not None:
        try:
            normalized_archived_limit = max(0, int(archived_limit))
        except (TypeError, ValueError):
            normalized_archived_limit = None
        try:
            normalized_archived_offset = max(0, int(archived_offset or 0))
        except (TypeError, ValueError):
            normalized_archived_offset = 0
        if normalized_archived_limit is not None:
            visible_rows_for_page = [s for s in visible_scoped_filtered if not s.get("archived")]
            archived_rows_for_page = [s for s in archived_scoped_filtered if s.get("archived")]
            scoped = visible_rows_for_page + archived_rows_for_page[
                normalized_archived_offset: normalized_archived_offset + normalized_archived_limit
            ]
    sidebar_reference_sessions: list[dict] = []
    if not include_archived:
        sidebar_reference_sessions = _hidden_archived_sidebar_reference_sessions(
            visible_scoped_filtered,
            archived_scoped_filtered,
        )
    if not include_archived:
        diag_stage("filter_archived_sessions")
    diag_stage("visible_lineage_metadata")
    _enrich_sidebar_lineage_metadata(scoped)
    # Delegated subagent children (#5307) are view-only, owned by the delegate
    # runner. Coerce their sidebar rows to read_only=True + is_cli_session=False
    # so the UI never offers delete / edit / truncate / pin affordances on them
    # (defense-in-depth is also enforced server-side on the mutation routes).
    def _coerce_subagent_rows(_rows):
        for _r in _rows:
            if not isinstance(_r, dict):
                continue
            _src = (
                str(_r.get("source_tag") or _r.get("raw_source")
                    or _r.get("session_source") or _r.get("source") or "").strip().lower()
            )
            _is_sa = _src == "subagent"
            # A stale index row can say webui/fork while state.db records the
            # row as source='subagent' (the child shares the parent's lineage).
            # For rows not already read-only, confirm via the state.db source so
            # a delegated child can't surface as a writable/CLI sidebar row.
            if not _is_sa and not _r.get("read_only"):
                _sid = str(_r.get("session_id") or "").strip()
                if _sid and _is_subagent_child_session_id(_sid):
                    _is_sa = True
            if _is_sa:
                _r["read_only"] = True
                _r["is_cli_session"] = False
    _coerce_subagent_rows(scoped)
    _coerce_subagent_rows(sidebar_reference_sessions)
    return {
        "sessions": [
            dict(s) if isinstance(s, dict) else {}
            for s in scoped
        ],
        "sidebar_reference_sessions": [
            dict(s) if isinstance(s, dict) else {}
            for s in sidebar_reference_sessions
        ],
        "cli_count": len(deduped_cli),
        "archived_count": archived_count,
        "archived_webui_count": archived_webui_count,
        "archived_cli_count": archived_cli_count,
        "webui_session_count": webui_session_count,
        "cli_session_count": cli_session_count,
        "include_archived": include_archived,
        "archived_limit": archived_limit,
        "archived_offset": archived_offset,
        "all_profiles": all_profiles,
        "active_profile": active_profile,
        "other_profile_count": other_profile_count,
        "settings": {
            "show_cli_sessions": show_cli_sessions,
            "show_previous_messaging_sessions": show_previous_messaging_sessions,
            "show_cron_sessions": show_cron_sessions,
            "show_claude_code_sessions": show_claude_code_sessions if show_cli_sessions else False,
            "show_webhook_sessions": show_webhook_sessions,
        },
    }


def _session_list_payload_to_response(payload: dict) -> dict:
    safe_merged = []
    runtime_rows = _session_list_cache_overlay_runtime_rows(payload.get("sessions", []) or [])
    # Read the redaction setting ONCE for the whole response and thread it through
    # every row, instead of letting each row's _redact_text() re-read settings.json
    # from disk (per title). The _sidebar_session_response_item -> _redact_text(_enabled=...)
    # plumbing already exists; this wires the caller so the sidebar list path gets the
    # same read-once optimization redact_session_data() already uses. On a large list
    # this was the multi-second response_write stage in /api/sessions diagnostics. (#4662 Phase 3)
    # load_settings is imported at module scope (below); this function only runs at
    # request time, well after module load, so no lazy import is needed.
    try:
        _redact_enabled = bool(load_settings().get("api_redact_enabled", True))
    except Exception:
        _redact_enabled = True  # fail safe: redact when settings are unreadable
    for s in runtime_rows:
        item = _sidebar_session_response_item(s, redact_enabled=_redact_enabled) if isinstance(s, dict) else {}
        safe_merged.append(item)
    safe_reference = []
    for s in payload.get("sidebar_reference_sessions", []) or []:
        item = _sidebar_session_response_item(s, redact_enabled=_redact_enabled) if isinstance(s, dict) else {}
        if item:
            item["_sidebar_reference_only"] = True
        safe_reference.append(item)
    response = {
        "sessions": safe_merged,
        "sidebar_reference_sessions": safe_reference,
        "cli_count": int(payload.get("cli_count", 0)),
        "archived_count": int(payload.get("archived_count", 0)),
        "archived_webui_count": int(payload.get("archived_webui_count", 0)),
        "archived_cli_count": int(payload.get("archived_cli_count", 0)),
        "include_archived": bool(payload.get("include_archived", False)),
        "all_profiles": bool(payload.get("all_profiles", False)),
        "active_profile": payload.get("active_profile"),
        "other_profile_count": int(payload.get("other_profile_count", 0)),
        "server_time": time.time(),
        "server_tz": time.strftime("%z"),
    }
    if "webui_session_count" in payload:
        response["webui_session_count"] = int(payload.get("webui_session_count", 0))
    if "cli_session_count" in payload:
        response["cli_session_count"] = int(payload.get("cli_session_count", 0))
    if payload.get("archived_limit") is not None:
        response["archived_limit"] = int(payload.get("archived_limit") or 0)
        response["archived_offset"] = int(payload.get("archived_offset") or 0)
    return response


def _hidden_archived_sidebar_reference_sessions(
    visible_rows: list[dict],
    archived_rows: list[dict],
) -> list[dict]:
    """Return hidden archived ancestors needed for client-side sidebar nesting.

    The default sidebar payload intentionally omits archived sessions. The
    browser still needs a tiny reference row for an archived parent/ancestor so
    `_attachChildSessionsToSidebarRows()` can suppress its visible child rows
    instead of rendering them as orphan top-level conversations (#4293).
    """
    archived_by_id = {
        str(row.get("session_id")): row
        for row in archived_rows
        if isinstance(row, dict) and row.get("archived") and row.get("session_id")
    }
    if not archived_by_id:
        return []

    references: list[dict] = []
    added: set[str] = set()
    visible_ids = {
        str(row.get("session_id"))
        for row in visible_rows
        if isinstance(row, dict) and row.get("session_id")
    }

    for row in visible_rows:
        if not isinstance(row, dict):
            continue
        parent_id = str(row.get("parent_session_id") or "").strip()
        seen: set[str] = set()
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            if parent_id in visible_ids:
                break
            parent = archived_by_id.get(parent_id)
            if not parent:
                break
            if parent_id not in added:
                references.append(parent)
                added.add(parent_id)
            parent_id = str(parent.get("parent_session_id") or "").strip()

    return references


def _get_cached_session_list_payload(
    *,
    key: tuple,
    builder,
    diag=None,
) -> dict:
    if diag is not None:
        try:
            diag.stage("session_list_cache_lookup")
        except Exception:
            pass

    cached, is_fresh = _session_list_cache_get(key, allow_stale=True)
    if cached is not None and is_fresh:
        if diag is not None:
            try:
                diag.stage("session_list_cache_hit")
            except Exception:
                pass
        return cached

    stale = cached  # now actually a stale payload when one exists, else None
    stale_reason = _session_list_cache_stale_reason(key) if stale is not None else None
    if stale is not None and stale_reason != "source":
        event, is_owner = _session_list_cache_claim_rebuild(key)
        if is_owner:
            if diag is not None:
                try:
                    diag.stage("session_list_cache_stale_background_rebuild")
                except Exception:
                    pass

            def _rebuild_stale_session_list_cache():
                try:
                    rebuild_attempts = 0
                    while True:
                        invalidation_stamp = _session_list_cache_invalidation_stamp(key)
                        try:
                            payload = builder()
                        except Exception:
                            logger.exception(
                                "session list stale-cache background rebuild failed"
                            )
                            return
                        if _session_list_cache_invalidation_stamp(key) == invalidation_stamp:
                            _session_list_cache_set(key, payload)
                            return
                        rebuild_attempts += 1
                        if rebuild_attempts >= 3:
                            return
                finally:
                    _session_list_cache_done(key, event)

            try:
                thread = threading.Thread(
                    target=_rebuild_stale_session_list_cache,
                    name="session-list-cache-rebuild",
                    daemon=True,
                )
                thread.start()
            except Exception:
                _session_list_cache_done(key, event)
        elif diag is not None:
            try:
                diag.stage("session_list_cache_stale_return")
            except Exception:
                pass
        return stale

    event, is_owner = _session_list_cache_claim_rebuild(key)
    if is_owner:
        if diag is not None:
            try:
                diag.stage("session_list_cache_rebuild_owner")
            except Exception:
                pass
        try:
            rebuild_attempts = 0
            while True:
                invalidation_stamp = _session_list_cache_invalidation_stamp(key)
                payload = builder()
                if _session_list_cache_invalidation_stamp(key) == invalidation_stamp:
                    _session_list_cache_set(key, payload)
                    if diag is not None:
                        try:
                            diag.stage("session_list_cache_stored")
                        except Exception:
                            pass
                    return payload
                rebuild_attempts += 1
                if diag is not None:
                    try:
                        diag.stage("session_list_cache_invalidated_during_rebuild")
                    except Exception:
                        pass
                if rebuild_attempts >= 3:
                    return payload
        finally:
            _session_list_cache_done(key, event)

    if diag is not None:
        try:
            if stale is not None:
                diag.stage("session_list_cache_wait_stale")
            else:
                diag.stage("session_list_cache_wait")
        except Exception:
            pass

    if stale is not None:
        timeout = _SESSIONS_CACHE_STALE_WAIT_SECONDS
    else:
        timeout = _SESSIONS_CACHE_WAIT_SECONDS
    event.wait(timeout)

    latest, is_fresh = _session_list_cache_get(key, allow_stale=False)
    if latest is not None:
        if diag is not None:
            try:
                diag.stage("session_list_cache_wait_hit")
            except Exception:
                pass
        return latest

    if stale is not None:
        if diag is not None:
            try:
                diag.stage("session_list_cache_wait_stale_fallback")
            except Exception:
                pass
        return stale

    # Safety path if the owner died before storing anything.
    if diag is not None:
        try:
            diag.stage("session_list_cache_fallback_rebuild")
        except Exception:
            pass
    invalidation_stamp = _session_list_cache_invalidation_stamp(key)
    payload = builder()
    if _session_list_cache_invalidation_stamp(key) == invalidation_stamp:
        _session_list_cache_set(key, payload)
    return payload

from api.config import (
    STATE_DIR,
    SESSION_DIR,
    DEFAULT_WORKSPACE,
    DEFAULT_MODEL,
    SESSIONS,
    SESSIONS_MAX,
    LOCK,
    SERVER_START_TIME,
    _resolve_cli_toolsets,
    get_available_models,
    get_available_models_for_session_visit,
    _provider_is_known_or_configured,
    IMAGE_EXTS,
    MD_EXTS,
    MIME_MAP,
    MAX_FILE_BYTES,
    MAX_UPLOAD_BYTES,
    register_runtime_stream,
    blocking_runtime_stream,
    runtime_stream_alive,
    runtime_worker_alive,
    runtime_transport,
    runtime_transport_items,
    runtime_transport_count,
    runtime_worker_items,
    runtime_last_run_finished_at,
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
from api.agent_health import build_agent_health_payload
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


def _clear_session_stream_fields(session) -> None:
    """Project one session object into the durable idle-stream shape."""
    session.active_stream_id = None
    if hasattr(session, "pending_user_message"):
        session.pending_user_message = None
    if hasattr(session, "pending_attachments"):
        session.pending_attachments = []
    if hasattr(session, "pending_started_at"):
        session.pending_started_at = None
    if hasattr(session, "pending_user_source"):
        session.pending_user_source = None


def _clear_stale_stream_state(session) -> bool:
    """Clear persisted streaming flags when the in-memory stream no longer exists.

    A server restart or worker crash can leave active_stream_id/pending_* in the
    session JSON while STREAMS is empty. The frontend then keeps reconnecting to
    a dead stream and shows a permanent running/thinking state.

    The repository reloads the full current generation only after acquiring its
    owner lock. A metadata-only projection or detached stale object therefore
    cannot overwrite the durable transcript or a newer stream generation.
    """
    stream_id = getattr(session, "active_stream_id", None)
    if not stream_id:
        return False
    if runtime_stream_alive(stream_id):
        return False
    if runtime_worker_alive(stream_id):
        logger.debug(
            "_clear_stale_stream_state: stream %s for session %s missing SSE channel "
            "but worker bookkeeping is still active; deferring stale cleanup",
            stream_id,
            getattr(session, "session_id", "?"),
        )
        return False
    grace_seconds = 30.0
    try:
        from api.sessions.store import _REPAIR_STALE_PENDING_GRACE_SECONDS
        grace_seconds = float(_REPAIR_STALE_PENDING_GRACE_SECONDS)
        pending_started_at = getattr(session, "pending_started_at", None)
        pending_age = time.time() - float(pending_started_at) if pending_started_at else None
    except Exception:
        pending_age = None
    if (
        getattr(session, "pending_user_message", None)
        and pending_age is not None
        and pending_age < grace_seconds
    ):
        logger.debug(
            "_clear_stale_stream_state: stream %s for session %s missing SSE channel "
            "but pending turn is %.1fs old; waiting for %.1fs stale-repair grace",
            stream_id,
            getattr(session, "session_id", "?"),
            pending_age,
            grace_seconds,
        )
        return False

    original_projection = session
    authoritative_session = None
    cleared = False
    repair_persisted = False

    try:
        # Runtime cleanup is not user activity; do not bubble old sessions to
        # the top of the sidebar. ``save_when`` also avoids a second save when
        # the recovery helper already persisted its richer repair atomically.
        with edit_session(
            session.session_id,
            session=session,
            touch_updated_at=False,
            save_when=lambda _current: cleared and not repair_persisted,
        ) as current:
            authoritative_session = current
            # A concurrent chat start may have replaced the old generation
            # while this cleanup waited for the same repository owner lock.
            if getattr(current, "active_stream_id", None) != stream_id:
                return False
            if runtime_stream_alive(stream_id) or runtime_worker_alive(stream_id):
                logger.debug(
                    "_clear_stale_stream_state: stream %s for session %s became "
                    "live while cleanup waited for its owner",
                    stream_id,
                    getattr(current, "session_id", "?"),
                )
                return False

            if getattr(current, "pending_user_message", None):
                try:
                    from api.sessions.store import (
                        _apply_core_sync_or_error_marker,
                        _get_profile_home,
                    )

                    profile_home = _get_profile_home(getattr(current, "profile", None))
                    core_path = (
                        profile_home
                        / "sessions"
                        / f"session_{current.session_id}.json"
                    )
                    repair_persisted = _apply_core_sync_or_error_marker(
                        current,
                        core_path,
                        stream_id_for_recheck=stream_id,
                        touch_updated_at=False,
                    )
                except Exception:
                    logger.exception(
                        "_clear_stale_stream_state: failed to repair stale pending "
                        "stream %s for session %s",
                        stream_id,
                        getattr(current, "session_id", "?"),
                    )
                    repair_persisted = False
                if repair_persisted:
                    cleared = True
                elif getattr(current, "active_stream_id", None) != stream_id:
                    return False

            if not repair_persisted:
                _materialize_pending_user_turn_before_error(current)
                _clear_session_stream_fields(current)
                cleared = True
    except Exception:
        logger.exception(
            "_clear_stale_stream_state: repository mutation failed for session %s",
            getattr(session, "session_id", "?"),
        )
        return False

    # Patch the caller's read projection only after durable persistence was
    # confirmed, avoiding one ghost reconnect without publishing an uncommitted
    # idle state after a save failure.
    if cleared and original_projection is not authoritative_session:
        try:
            _clear_session_stream_fields(original_projection)
        except Exception:
            pass
    return cleared


from api.routes_parts import anchor_scene as _anchor_scene_routes_part
from api.routes_parts.anchor_scene import (
    _run_journal_status_payload,
    _RUN_JOURNAL_TOOL_ID_KEYS,
    _run_journal_snapshot_tool_id,
    _truncate_journal_snapshot_value,
    _run_journal_snapshot_recovery_args,
    _run_journal_snapshot_arg_detail_score,
    _run_journal_snapshot_merge_args,
    _run_journal_live_snapshot,
    _ANCHOR_ACTIVITY_SCENE_MAX_BYTES,
    _ANCHOR_ACTIVITY_SCENE_MAX_ROWS,
    _assistant_anchor_scene_message_ref,
    _assistant_anchor_scene_message_ref_payload,
    _anchor_scene_message_ref_digest,
    _sanitize_anchor_activity_scene,
    _anchor_scene_int_or_none,
    _anchor_scene_message_index_from_request,
    _anchor_scene_candidate_matches_scene,
    _find_anchor_scene_message,
    _normalize_anchor_scene_message_ref,
    _anchor_scene_records,
    _anchor_scene_message_text,
    _anchor_scene_content_text,
    _anchor_scene_content_visible_text,
    _anchor_scene_message_has_content_tool_use,
    _anchor_scene_final_answer_text,
    _anchor_scene_message_reasoning_text,
    _anchor_scene_clean_text,
    _anchor_scene_text_key,
    _ANCHOR_SCENE_SETTLED_SNIPPET_CAP,
    _anchor_scene_string_payload,
    _anchor_scene_is_bounded_tool_body_preview,
    _anchor_scene_row_looks_like_final_answer,
    _anchor_scene_text_has_long_overlap,
    _anchor_scene_row_is_stale_token_answer,
    _anchor_scene_message_turn_duration,
    _anchor_scene_tool_id,
    _anchor_scene_tool_name,
    _anchor_scene_tool_args,
    _anchor_scene_content_tool,
    _anchor_scene_row_base,
    _anchor_scene_prose_row,
    _anchor_scene_thinking_row,
    _anchor_scene_tool_row,
    _anchor_scene_tool_row_id,
    _anchor_scene_tool_row_name,
    _anchor_scene_tool_rows_have_compatible_names,
    _anchor_scene_tool_row_args,
    _anchor_scene_object_contains_subset,
    _anchor_scene_tool_rows_have_compatible_invocation,
    _anchor_scene_tool_row_has_invocation_evidence,
    _anchor_scene_tool_rows_can_name_match,
    _anchor_scene_tool_rows_have_different_explicit_ids,
    _anchor_scene_tool_row_started_at,
    _anchor_scene_tool_rows_have_same_started_at,
    _anchor_scene_tool_row_body_text,
    _anchor_scene_tool_rows_have_compatible_body,
    _anchor_scene_matching_content_tool_row_index,
    _anchor_scene_content_rows,
    _anchor_scene_row_key,
    _anchor_scene_row_has_live_identity,
    _anchor_scene_settle_live_running_row,
    _complete_hydrated_anchor_scene,
    _hydrate_anchor_activity_scenes,
    _handle_session_anchor_scene,
)

_install_routes_part(globals(), _anchor_scene_routes_part)
del _anchor_scene_routes_part


def _get_or_materialize_session(sid: str, *, refresh_cli_messages: bool = False):
    """Get a session, materializing from CLI/agent metadata if not in WebUI store.

    Mirrors the fallback logic in /api/session/archive (routes.py:~8530).
    Raises:
        KeyError: session not found in any store
        PermissionError: session is read-only (messaging/Claude Code)
    """
    try:
        s = get_session(sid)
        s = get_full_session(sid, session=s)
        # Read-only guard on the happy path too: an already-stored read-only /
        # imported session must not be mutated via rename/update/move
        # (Session.save() does not enforce this). Scope this to the explicit
        # read_only flag — a stored messaging session already owns its sidecar,
        # so the messaging-fork concern only applies to the materialize fallback
        # below (and the heuristic record-check would mis-trip on mock sessions).
        if getattr(s, "read_only", False):
            raise PermissionError("read-only imported session")
        # A previously-persisted subagent sidecar (#5307) is view-only and
        # owned by the delegate runner — even if it was stored with
        # read_only=False (e.g. materialized before this fix), it must not be
        # mutated / used as a writable chat session. This mirrors the
        # missing-sidecar subagent guard below on the happy path.
        if (
            (getattr(s, "source_tag", "") or getattr(s, "raw_source", "") or "").strip().lower() == "subagent"
            or _is_subagent_child_session_id(sid)
        ):
            raise PermissionError("read-only subagent child session")
        if refresh_cli_messages and getattr(s, "is_cli_session", False):
            latest_messages = get_cli_session_messages(
                sid,
                profile=getattr(s, "profile", None),
            )
            current_messages = list(getattr(s, "messages", None) or [])
            if (
                latest_messages
                and len(latest_messages) >= len(current_messages)
                and _session_messages_have_prefix(latest_messages, current_messages)
            ):
                # Keep the stitched CLI transcript authoritative on the first
                # WebUI continuation path without clobbering later divergent
                # WebUI-owned turns.
                s.messages = list(latest_messages)
        return s
    except KeyError:
        pass

    # Fallback: try to materialize from CLI/agent session metadata
    cli_meta = _lookup_cli_session_metadata(sid)

    # Delegated subagent children (#5307) are view-only: their transcript lives
    # in state.db and ownership belongs to the delegate runner, not WebUI. They
    # must never be materialized as a writable sidecar here — this is the shared
    # chokepoint reached by POST /api/chat/start (_get_or_materialize_session),
    # so gating it closes the write path that bypasses the GET/import_cli guards.
    # Checked via state.db source (independent of cli_meta, which is often empty
    # for a server-side subagent child).
    _mat_source_tag = (
        (cli_meta or {}).get("source_tag") or (cli_meta or {}).get("raw_source") or ""
    ).strip().lower()
    if _mat_source_tag == "subagent" or _is_subagent_child_session_id(sid):
        raise PermissionError("read-only subagent child session")

    if not cli_meta:
        raise KeyError(sid)

    # Read-only guard: messaging sessions and Claude Code imports cannot be
    # mutated. Reject BOTH an explicit read_only flag AND any messaging-source
    # record — agent rows normalize messaging sources without setting read_only,
    # and state.db (not a WebUI sidecar) is the source of truth for them, so
    # materializing a writable sidecar would fork the title/state.
    if cli_meta.get("read_only") or _is_messaging_session_record(cli_meta):
        raise PermissionError("read-only imported session")

    if _is_messaging_session_record(cli_meta):
        # Messaging sessions: lightweight Session with no messages (state.db is source of truth)
        s = Session(
            session_id=sid,
            title=cli_meta.get("title") or title_from(get_cli_session_messages(sid), "CLI Session"),
            workspace=get_last_workspace(),
            model=cli_meta.get("model") or "unknown",
            created_at=cli_meta.get("created_at"),
            updated_at=cli_meta.get("updated_at"),
        )
        apply_cli_source_metadata(
            s,
            cli_meta,
            is_cli_session=is_cli_session_row(cli_meta),
        )
        s.save(touch_updated_at=False)
    else:
        # Regular CLI/agent sessions: import full message history
        msgs = get_cli_session_messages(sid)
        if not msgs:
            raise KeyError(sid)
        s = import_cli_session(
            sid,
            cli_meta.get("title") or title_from(msgs, "CLI Session"),
            msgs,
            cli_meta.get("model") or "unknown",
            profile=cli_meta.get("profile"),
            created_at=cli_meta.get("created_at"),
            updated_at=cli_meta.get("updated_at"),
            source_metadata=cli_meta,
        )
        apply_cli_source_metadata(
            s,
            cli_meta,
            is_cli_session=is_cli_session_row(cli_meta),
        )

    return s


def _share_snapshot_messages_for_session(session, *, cli_meta: dict | None = None) -> list:
    """Return the visible transcript that a public share should snapshot.

    External sessions (Telegram/Discord/Slack/CLI/etc.) may have no WebUI sidecar
    or may persist only local metadata in the sidecar while the transcript lives
    in state.db. Public sharing should snapshot the same visible conversation the
    session page renders, not the bare local sidecar payload.
    """
    sid = str(getattr(session, "session_id", "") or "").strip()
    current_messages = list(getattr(session, "messages", None) or [])
    if not sid:
        return current_messages
    profile = getattr(session, "profile", None)
    is_messaging = (
        _is_messaging_session_record(session)
        or _is_messaging_session_record(cli_meta)
    )
    if is_messaging or not current_messages:
        cli_messages = get_cli_session_messages(sid, profile=profile)
        if cli_messages:
            if is_messaging:
                return _merged_session_messages_for_display(session, cli_messages)
            return list(cli_messages)
    return current_messages


def _build_share_metadata_sidecar(
    sid: str,
    snapshot_session,
    *,
    cli_meta: dict | None = None,
):
    """Create a minimal WebUI sidecar for share metadata on external sessions."""
    cli_meta = dict(cli_meta or {})
    workspace = (
        cli_meta.get("workspace")
        or cli_meta.get("cwd")
        or getattr(snapshot_session, "workspace", None)
    )
    if not workspace:
        workspace = get_last_workspace()
    session = Session(
        session_id=sid,
        title=(
            cli_meta.get("title")
            or getattr(snapshot_session, "title", None)
            or title_from(getattr(snapshot_session, "messages", None) or [], "CLI Session")
        ),
        workspace=workspace,
        messages=[],
        model=cli_meta.get("model") or getattr(snapshot_session, "model", None) or "unknown",
        model_provider=(
            cli_meta.get("model_provider")
            or getattr(snapshot_session, "model_provider", None)
        ),
        created_at=cli_meta.get("created_at") or getattr(snapshot_session, "created_at", None),
        updated_at=cli_meta.get("updated_at") or getattr(snapshot_session, "updated_at", None),
        profile=cli_meta.get("profile") or getattr(snapshot_session, "profile", None),
    )
    session.is_cli_session = bool(
        getattr(snapshot_session, "is_cli_session", False)
        or is_cli_session_row(cli_meta)
    )
    session.source_tag = cli_meta.get("source_tag") or getattr(snapshot_session, "source_tag", None)
    session.raw_source = (
        cli_meta.get("raw_source")
        or getattr(snapshot_session, "raw_source", None)
        or session.source_tag
    )
    session.session_source = (
        cli_meta.get("session_source")
        or getattr(snapshot_session, "session_source", None)
    )
    session.source_label = (
        cli_meta.get("source_label")
        or getattr(snapshot_session, "source_label", None)
    )
    session.read_only = bool(
        cli_meta.get("read_only") or getattr(snapshot_session, "read_only", False)
    )
    for attr in (
        "user_id",
        "chat_id",
        "chat_type",
        "thread_id",
        "session_key",
        "platform",
        "origin_chat_id",
        "origin_user_id",
        "parent_session_id",
    ):
        value = cli_meta.get(attr)
        if value is None:
            value = getattr(snapshot_session, attr, None)
        if value is not None:
            setattr(session, attr, value)
    return session


def _resolve_share_session_pair(sid: str, handler):
    """Resolve a shareable session plus the sidecar that stores share metadata.

    Returns ``(snapshot_session, stored_session_or_none, cli_meta)``. The
    snapshot session always carries the transcript that should become the public
    share payload. ``stored_session`` is the WebUI-owned sidecar to mutate for
    share_token/share_created_at persistence; it may be absent for pure external
    sessions that have not yet created local metadata.
    """
    try:
        stored_session = get_session(sid)
        cli_meta = (
            _lookup_cli_session_metadata(sid)
            if _session_requires_cli_metadata_lookup(stored_session)
            else {}
        )
        effective_profile = (
            (cli_meta or {}).get("profile")
            or getattr(stored_session, "profile", None)
            or None
        )
        if not _session_visible_to_active_profile(effective_profile, handler):
            raise KeyError(sid)
        stored_session = get_full_session(sid, session=stored_session)
        snapshot_session = copy.copy(stored_session)
        snapshot_session.messages = _share_snapshot_messages_for_session(
            stored_session,
            cli_meta=cli_meta,
        )
        return snapshot_session, stored_session, cli_meta or {}
    except KeyError:
        cli_meta = _lookup_cli_session_metadata(sid) or {}
        effective_profile = cli_meta.get("profile") or None
        if not _session_visible_to_active_profile(effective_profile, handler):
            raise KeyError(sid) from None
        synth, reason = _claim_or_synthesize_cli_session(sid, cli_meta=cli_meta)
        if reason == "was_webui" or synth is None:
            raise KeyError(sid) from None
        return synth, None, cli_meta


def _reconcile_stale_stream_state_for_session_rows(session_rows) -> bool:
    """Clear stale persisted stream fields before /api/sessions serializes rows."""
    changed = False
    for row in session_rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("session_id")
        if not sid or not row.get("active_stream_id"):
            continue
        if row.get("is_streaming") is True:
            continue
        try:
            session = get_session(sid, metadata_only=True)
        except Exception:
            logger.debug(
                "Failed to load session %s while reconciling stale stream state",
                sid,
                exc_info=True,
            )
            continue
        if session is None:
            continue
        changed = _clear_stale_stream_state(session) or changed
    return changed

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


from api.routes_parts import session_projection as _session_projection_routes_part
from api.routes_parts.session_projection import (
    _lookup_gateway_session_identity,
    _lookup_cli_session_metadata,
    _session_index_marks_was_webui,
    _session_deleted_tombstone_marks_was_webui,
    _state_db_session_source,
    _is_subagent_child_session_id,
    _session_is_subagent_view_only,
    _is_claimable_cli_source,
    _claim_or_synthesize_cli_session,
    _request_wants_all_profiles_import,
    _normalize_import_profile_value,
    _load_branch_source_or_refuse,
    _resolve_cli_import_metadata,
    _messaging_session_identity,
    _is_pre_compression_snapshot_id,
    _is_pre_compression_continuation_row,
    _session_messaging_raw_source,
    _has_durable_messaging_identity,
    _numeric_count,
    _should_hide_stale_messaging_session,
    _is_messaging_session_record,
    _messages_include_tool_metadata,
    _tool_calls_for_message_window,
    _message_counts_as_renderable_for_window,
    _tool_call_ids_in_messages,
    _tool_result_matches_call_ids,
    _message_window_for_display,
    _LIMITED_TOOL_CONTENT_MAX_CHARS,
    _MAX_MSG_LIMIT,
    _parse_msg_limit,
    _SIDECAR_BYTE_TAIL_THRESHOLD,
    _STATE_DB_DISPLAY_ROW_BACKSTOP,
    _state_db_backstop_limit_for_display,
    _LIMITED_TOOL_CONTENT_NOTICE,
    _tool_message_for_limited_payload,
    _messages_for_limited_payload,
    _limited_webui_messages_for_display,
    _limited_webui_messages_for_display_with_sidecar,
    _sidecar_file_exceeds_threshold,
    _state_db_since_timestamp_for_limited_display,
    _messages_start_with_visible_prefix,
    _webui_sidecar_lineage_messages_for_display,
    _merged_session_messages_for_display,
    _merged_webui_lineage_messages_for_display,
    _message_summary,
    _metadata_only_message_summary,
    _session_requires_cli_metadata_lookup,
    _is_messaging_session_id,
    _session_sort_timestamp,
    _is_cli_session_for_settings,
    _normalize_sidebar_source_flags,
    _reconcile_session_detail_source_flags,
    _session_source_is_webui,
    _normalized_source_marker,
    _is_api_server_sidecar_row,
    _session_lineage_ids,
    _is_duplicate_webui_state_projection,
    _dedupe_cli_sidebar_sessions_for_api,
    CLI_VISIBLE_SESSION_CAP,
    _cap_recent_cli_sessions,
    _merge_cli_sidebar_metadata,
    _messaging_source_key,
    _keep_latest_messaging_session_per_source,
    _publish_materialized_session,
    _SESSION_DETAIL_TAIL_CACHE_VERSION,
    _SESSION_DETAIL_TAIL_CACHE_MAX_ENTRIES,
    _SESSION_DETAIL_TAIL_CACHE_MAX_BYTES,
    _SESSION_DETAIL_TAIL_CACHE_MAX_ENTRY_BYTES,
    _SESSION_DETAIL_TAIL_CACHE,
    _SESSION_DETAIL_TAIL_CACHE_BYTES,
    _SESSION_DETAIL_TAIL_CACHE_LOCK,
    _session_detail_tail_path_stamp,
    _session_detail_tail_source_stamp,
    _session_detail_tail_cache_eligible,
    _session_detail_tail_cache_key,
    _session_detail_tail_cache_get,
    _session_detail_tail_cache_set,
    _clear_session_detail_tail_cache,
    _COMPRESSION_RECOVERY_START_LOCK,
    _pre_compression_continuation_session_id,
    _session_attention_summary,
    _SIDEBAR_SESSION_RESPONSE_FIELDS,
    _sidebar_session_response_item,
    _redact_sidebar_title_fields,
)

_install_routes_part(globals(), _session_projection_routes_part)
del _session_projection_routes_part


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
    git_info_for_workspace,
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

# ── Logs endpoint ─────────────────────────────────────────────────────────────
_LOG_FILE_WHITELIST = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
}
_LOG_TAIL_VALUES = {100, 200, 500, 1000}
_LOG_DEFAULT_TAIL = 200
_LOG_MAX_BYTES = 4 * 1024 * 1024


def _normalize_logs_tail(raw_tail) -> int:
    try:
        tail = int(str(raw_tail or "").strip())
    except (TypeError, ValueError):
        return _LOG_DEFAULT_TAIL
    return tail if tail in _LOG_TAIL_VALUES else _LOG_DEFAULT_TAIL


def _handle_logs(handler, parsed) -> bool:
    """Return a bounded tail window for an active-profile Hermes log file."""
    query = parse_qs(parsed.query)
    file_key = (query.get("file", ["agent"])[0] or "agent").strip().lower()
    filename = _LOG_FILE_WHITELIST.get(file_key)
    if not filename:
        return bad(handler, "Unknown log file", status=400)

    tail = _normalize_logs_tail(query.get("tail", [None])[0])
    try:
        from api.profiles import get_active_hermes_home

        hermes_home = Path(get_active_hermes_home()).expanduser()
    except Exception:
        hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()

    log_dir = hermes_home / "logs"
    log_path = log_dir / filename
    try:
        # Defense in depth: the filename is hardcoded above, but keep the final
        # path anchored under the active profile's logs directory.
        if log_path.resolve(strict=False).parent != log_dir.resolve(strict=False):
            return bad(handler, "Invalid log file", status=400)
        if not log_path.exists() or not log_path.is_file():
            return j(handler, {
                "file": file_key,
                "tail": tail,
                "lines": [],
                "truncated": False,
                "total_bytes": 0,
                "mtime": None,
                "hint": f"Log file for {file_key} not found yet.",
            })
        st = log_path.stat()
        total_bytes = int(st.st_size)
        read_bytes = min(total_bytes, _LOG_MAX_BYTES)
        with log_path.open("rb") as fh:
            if total_bytes > read_bytes:
                fh.seek(total_bytes - read_bytes)
            raw = fh.read(read_bytes)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()[-tail:]
        return j(handler, {
            "file": file_key,
            "tail": tail,
            "lines": lines,
            "truncated": total_bytes > read_bytes,
            "total_bytes": total_bytes,
            "mtime": st.st_mtime,
            "hint": "",
        })
    except Exception as exc:
        logger.exception("Failed to read whitelisted log file %s", file_key)
        return bad(handler, _sanitize_error(exc), status=500)

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


# ── Insights endpoint ──────────────────────────────────────────────────────────

def _handle_insights(handler, parsed) -> bool:
    """Return usage analytics from local WebUI and Hermes session data."""
    from api.insights import build_insights
    from api.sessions.store import _active_state_db_path

    payload = build_insights(
        parsed.query,
        session_dir=SESSION_DIR,
        state_db_path=_active_state_db_path,
    )
    return j(handler, payload)


def _project_os_workspace_read(repo_root: Path, rel: str) -> dict | None:
    try:
        return read_file_content(repo_root, rel)
    except Exception:
        return None


def _project_os_workspace_json(repo_root: Path, rel: str) -> dict | None:
    payload = _project_os_workspace_read(repo_root, rel)
    if not payload or not isinstance(payload.get("content"), str):
        return None
    try:
        parsed = json.loads(payload.get("content") or "")
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _project_os_truth_board_slugs(repo_root: Path) -> set[str]:
    slugs: set[str] = set()

    def add(value) -> None:
        text = str(value or "").strip()
        if text:
            slugs.add(text)

    for rel in (
        ".ax/handoff/current.json",
        ".ax/status/active.json",
        ".ax/status/heartbeat.json",
    ):
        truth = _project_os_workspace_json(repo_root, rel)
        if not isinstance(truth, dict):
            continue
        raw_board = truth.get("board")
        if isinstance(raw_board, dict):
            add(raw_board.get("slug"))
            add(raw_board.get("id"))
            add(raw_board.get("name"))
            add(raw_board.get("display_name"))
        else:
            add(raw_board)
        for key in (
            "selected_board_slug",
            "canonical_backlog_board_id",
            "current_browser_board_id",
            "active_proof_board_id",
            "recover_board_id",
        ):
            add(truth.get(key))
    return slugs


def _project_os_repo_matches_board(repo_root: Path, board_slug: str | None) -> bool:
    slug = str(board_slug or "").strip()
    return bool(slug and slug in _project_os_truth_board_slugs(repo_root))


def _project_os_candidate_repo_roots(workspace_root: Path | None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            return
        if not resolved.is_dir():
            return
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            candidates.append(resolved)

    add(workspace_root)

    scan_root = None
    if workspace_root is not None:
        try:
            scan_root = workspace_root.expanduser().resolve()
        except Exception:
            scan_root = None
    if not scan_root or not scan_root.is_dir():
        return candidates

    skip_names = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
        "vendor",
        "dist",
        "build",
    }
    queue_dirs: list[tuple[Path, int]] = [(scan_root, 0)]
    inspected = 0
    while queue_dirs and inspected < 300:
        current, depth = queue_dirs.pop(0)
        inspected += 1
        if (current / ".ax").is_dir() or (current / "docs" / "project-os").is_dir():
            add(current)
        if depth >= 3:
            continue
        try:
            children = sorted(
                (child for child in current.iterdir() if child.is_dir()),
                key=lambda child: child.name,
            )
        except Exception:
            continue
        for child in children:
            name = child.name
            if name in skip_names or (name.startswith(".") and name != ".ax"):
                continue
            queue_dirs.append((child, depth + 1))
    add(Path.cwd())
    return candidates


def _project_os_resolve_repo_root_for_board(repo_root: Path | None, board_slug: str | None) -> Path | None:
    slug = str(board_slug or "").strip()
    if not slug:
        return repo_root if repo_root and repo_root.exists() else None
    if repo_root and repo_root.exists() and _project_os_repo_matches_board(repo_root, slug):
        return repo_root
    for candidate in _project_os_candidate_repo_roots(repo_root):
        if _project_os_repo_matches_board(candidate, slug):
            return candidate
    return repo_root if repo_root and repo_root.exists() else None


def _project_os_goal_summary(project_md: dict | None, handoff: dict | None, status_md: dict | None, board_name: str | None = None, board_desc: str | None = None) -> str:
    project_text = str((project_md or {}).get("content") or "")
    for line in project_text.splitlines():
        text = line.strip().lstrip("- ").strip()
        if not text or text.startswith("#"):
            continue
        return text[:220]
    handoff_summary = str((handoff or {}).get("goal_summary") or "").strip()
    if handoff_summary:
        return handoff_summary[:220]
    desc = str(board_desc or "").strip()
    if desc:
        return desc[:220]
    status_text = str((status_md or {}).get("content") or "")
    for line in status_text.splitlines():
        text = line.strip().lstrip("- ").strip()
        if not text or text.startswith("#"):
            continue
        return text[:220]
    return str(board_name or "Project OS").strip()[:220]


def _project_os_onboarding_context(repo_root: Path, project_md: dict | None, plan_md: dict | None, status_md: dict | None) -> dict:
    project_text = str((project_md or {}).get("content") or "")
    plan_text = str((plan_md or {}).get("content") or "")
    status_text = str((status_md or {}).get("content") or "")
    merged = "\n".join([project_text, plan_text, status_text])
    is_non_git_workspace = not (repo_root / ".git").exists()
    has_boundary_hold = "TO_BE_VALIDATED_BY_HERMES" in merged
    child_repo_blocked = (
        "auto-promoted" in merged
        or "auto-adopted" in merged
        or "auto-adoption | `금지`" in merged
        or "자동 승격 금지" in merged
        or "canonical repo continuity로 승격하지 않습니다" in merged
    )
    workspace_root_confirmed = str(repo_root) in merged
    if not (is_non_git_workspace and (project_text or plan_text or status_text)):
        return {
            "active": False,
            "doc_source": "project-os",
        }
    status_label = "보류(안전)" if has_boundary_hold else "확인됨"
    summary = "workspace root onboarding 진행 중 · 저장소 경계는 아직 미확정이며 자동 승격은 금지됩니다."
    next_safe_action = "workspace-root 기준으로 경계만 좁게 검증"
    if has_boundary_hold:
        summary = "workspace root onboarding 진행 중 · 저장소 경계는 아직 미확정이며 TO_BE_VALIDATED_BY_HERMES 상태를 유지합니다."
    return {
        "active": True,
        "doc_source": "root",
        "status_label": status_label,
        "summary": summary,
        "next_safe_action": next_safe_action,
        "workspace_root_confirmed": workspace_root_confirmed,
        "repo_boundary_status": "TO_BE_VALIDATED_BY_HERMES" if has_boundary_hold else "confirmed",
        "child_repo_auto_promotion_blocked": bool(child_repo_blocked),
        "guardrails": [
            "workspace root 확인됨" if workspace_root_confirmed else "workspace root 확인 필요",
            "child repo 자동 승격 금지 유지" if child_repo_blocked else "child repo guardrail 확인 필요",
            "repo boundary 미확정 유지" if has_boundary_hold else "repo boundary confirmed",
        ],
    }


def _handle_project_os_dashboard(handler, parsed) -> bool:
    qs = parse_qs(parsed.query or "")
    requested_board = str((qs.get("board") or [""])[0] or "").strip()
    workspace_raw = str(get_last_workspace() or "").strip()
    repo_root = Path(workspace_raw).expanduser() if workspace_raw else None
    selected_board_meta = None
    if requested_board:
        try:
            from api.kanban_bridge import _kb, _board_meta_dict
            kb = _kb()
            for meta in kb.list_boards(include_archived=True) or []:
                board = _board_meta_dict(meta)
                if str(board.get("slug") or "") == requested_board:
                    selected_board_meta = board
                    workdir = str(board.get("default_workdir") or "").strip()
                    if workdir:
                        candidate = Path(workdir).expanduser()
                        if candidate.exists():
                            repo_root = candidate
                    break
        except Exception:
            selected_board_meta = None
    repo_root = _project_os_resolve_repo_root_for_board(repo_root, requested_board)
    if not repo_root or not repo_root.exists():
        j(handler, {
            "workspace": None,
            "repo_root": None,
            "git": None,
            "docs": {},
            "handoff": None,
            "active": None,
            "heartbeat": None,
            "goal_summary": "",
        })
        return True

    handoff = _project_os_workspace_json(repo_root, ".ax/handoff/current.json")
    active = _project_os_workspace_json(repo_root, ".ax/status/active.json")
    heartbeat = _project_os_workspace_json(repo_root, ".ax/status/heartbeat.json")
    project_md = _project_os_workspace_read(repo_root, "docs/project-os/PROJECT.md")
    plan_md = _project_os_workspace_read(repo_root, "docs/project-os/PLAN.md")
    status_md = _project_os_workspace_read(repo_root, "docs/project-os/STATUS.md")
    blocker_md = _project_os_workspace_read(repo_root, "docs/project-os/BLOCKER-RESOLVER.md")
    root_project_md = _project_os_workspace_read(repo_root, "PROJECT.md")
    root_plan_md = _project_os_workspace_read(repo_root, "PLAN.md")
    root_status_md = _project_os_workspace_read(repo_root, "STATUS.md")
    onboarding = _project_os_onboarding_context(repo_root, root_project_md, root_plan_md, root_status_md)
    if onboarding.get("active"):
        project_md = root_project_md or project_md
        plan_md = root_plan_md or plan_md
        status_md = root_status_md or status_md

    if isinstance(active, dict):
        original_repo_root = repo_root
        active_repo_root = str(active.get("repo_root") or "").strip()
        if active_repo_root:
            candidate = Path(active_repo_root).expanduser()
            if candidate.exists():
                try:
                    repo_root = candidate.resolve()
                except Exception:
                    repo_root = candidate
        if repo_root != original_repo_root:
            handoff = _project_os_workspace_json(repo_root, ".ax/handoff/current.json")
            active = _project_os_workspace_json(repo_root, ".ax/status/active.json")
            heartbeat = _project_os_workspace_json(repo_root, ".ax/status/heartbeat.json")
            project_md = _project_os_workspace_read(repo_root, "docs/project-os/PROJECT.md")
            plan_md = _project_os_workspace_read(repo_root, "docs/project-os/PLAN.md")
            status_md = _project_os_workspace_read(repo_root, "docs/project-os/STATUS.md")
            blocker_md = _project_os_workspace_read(repo_root, "docs/project-os/BLOCKER-RESOLVER.md")
            root_project_md = _project_os_workspace_read(repo_root, "PROJECT.md")
            root_plan_md = _project_os_workspace_read(repo_root, "PLAN.md")
            root_status_md = _project_os_workspace_read(repo_root, "STATUS.md")
            onboarding = _project_os_onboarding_context(repo_root, root_project_md, root_plan_md, root_status_md)
            if onboarding.get("active"):
                project_md = root_project_md or project_md
                plan_md = root_plan_md or plan_md
                status_md = root_status_md or status_md

    try:
        git = git_info_for_workspace(repo_root)
    except Exception:
        git = None

    board_name = None
    board_desc = None
    if isinstance(handoff, dict):
        board_dict: dict = {}
        raw_board = handoff.get("board")
        if isinstance(raw_board, dict):
            board_dict = raw_board
        board_name = board_dict.get("display_name") or board_dict.get("name") or board_dict.get("slug")
        board_desc = handoff.get("goal_summary") or board_dict.get("repo_corroboration")
    if selected_board_meta:
        board_name = board_name or selected_board_meta.get("name") or selected_board_meta.get("slug")
        board_desc = board_desc or selected_board_meta.get("description")

    j(handler, {
        "workspace": str(repo_root),
        "repo_root": str(repo_root),
        "selected_board_slug": requested_board or (selected_board_meta or {}).get("slug"),
        "git": git,
        "docs": {
            "project": project_md,
            "plan": plan_md,
            "status": status_md,
            "blocker_resolver": blocker_md,
        },
        "handoff": handoff,
        "active": active,
        "heartbeat": heartbeat,
        "onboarding": onboarding,
        "goal_summary": _project_os_goal_summary(project_md, handoff, status_md, board_name, board_desc),
    })
    return True


# ── GET routes ────────────────────────────────────────────────────────────────


def _accept_loop_health(handler) -> dict:
    server = getattr(handler, "server", None)
    return {
        "requests_total": int(getattr(server, "accept_loop_requests_total", 0) or 0),
        "last_request_at": round(float(getattr(server, "accept_loop_last_request_at", 0.0) or 0.0), 3),
    }


def _streams_lock_health(timeout_seconds: float = 0.5) -> dict:
    t0 = time.time()
    active_streams = runtime_transport_count(timeout=timeout_seconds)
    elapsed_ms = round((time.time() - t0) * 1000, 1)
    if active_streams is None:
        return {
            "status": "blocked",
            "timeout_seconds": timeout_seconds,
            "ms": elapsed_ms,
        }
    return {
        "status": "ok",
        "active_streams": active_streams,
        "ms": elapsed_ms,
    }


def _stream_runtime_diagnostics() -> dict:
    """Return non-sensitive SSE stream diagnostics for health/deep status.

    The WebUI chat path can feel slow or stuck when streams are alive but no
    browser is attached, or when many events are buffering offline. This helper
    exposes counts only — stream ids plus subscriber/buffer sizes — and avoids
    event payloads, prompts, tool arguments, or paths.
    """
    streams = []
    total_subscribers = 0
    total_offline_buffered_events = 0
    items = runtime_transport_items()
    for stream_id, stream in items:
        snapshot = {}
        diagnostic_snapshot = getattr(stream, "diagnostic_snapshot", None)
        if callable(diagnostic_snapshot):
            try:
                raw_snapshot = diagnostic_snapshot()
                if isinstance(raw_snapshot, dict):
                    snapshot = raw_snapshot
            except Exception:
                snapshot = {}
        subscriber_count = int(snapshot.get("subscriber_count") or 0)
        offline_buffered_events = int(snapshot.get("offline_buffered_events") or 0)
        total_subscribers += subscriber_count
        total_offline_buffered_events += offline_buffered_events
        streams.append({
            "stream_id": str(stream_id),
            "subscriber_count": subscriber_count,
            "offline_buffered_events": offline_buffered_events,
        })
    streams.sort(key=lambda item: item["stream_id"])
    return {
        "active_streams": len(streams),
        "total_subscribers": total_subscribers,
        "total_offline_buffered_events": total_offline_buffered_events,
        "streams": streams,
    }


def _run_lifecycle_health() -> dict:
    """Return active worker-run state independent of SSE stream presence."""
    now = time.time()
    runs = []
    for _stream_id, raw in runtime_worker_items():
        item = dict(raw or {})
        item.pop("session_id", None)
        item.pop("stream_id", None)
        item.pop("workspace", None)
        started_at = item.get("started_at")
        try:
            age = max(0.0, now - float(started_at))
        except Exception:
            age = 0.0
        item["age_seconds"] = round(age, 1)
        runs.append(item)
    last_finished = runtime_last_run_finished_at()
    runs.sort(key=lambda item: float(item.get("started_at") or 0.0))
    payload = {
        "active_runs": len(runs),
        "runs": runs,
        "last_run_finished_at": last_finished,
    }
    if runs:
        payload["oldest_run_age_seconds"] = runs[0].get("age_seconds", 0.0)
    elif last_finished:
        payload["idle_seconds_since_last_run"] = round(max(0.0, now - float(last_finished)), 1)
    return payload


def _deep_health_checks(stream_check: dict | None = None) -> tuple[dict, bool]:
    """Run cheap probes that exercise the state paths used by the UI shell.

    Plain /health intentionally stays tiny. /health?deep=1 is for supervisors
    and watchdogs that need to know whether the process can still touch the
    shared stream map, sidebar/session path, project state, and Hermes state.db
    without hitting the RST-before-write failure mode from #1458.

    `stream_check` is the result from a prior `_streams_lock_health()` call;
    if provided, it's reused so we don't acquire `STREAMS_LOCK` twice on the
    same /health?deep=1 request (per Opus advisor on stage-297).
    """
    checks: dict[str, dict] = {}

    checks["streams_lock"] = stream_check if stream_check is not None else _streams_lock_health()
    checks["stream_runtime"] = {
        "status": "ok",
        **_stream_runtime_diagnostics(),
    }
    if checks["streams_lock"].get("status") != "ok":
        return checks, False

    t0 = time.time()
    try:
        sessions = all_sessions()
        checks["sessions"] = {
            "status": "ok",
            "count": len(sessions),
            "ms": round((time.time() - t0) * 1000, 1),
        }
    except Exception as exc:
        checks["sessions"] = {
            "status": "error",
            "error": type(exc).__name__,
            "ms": round((time.time() - t0) * 1000, 1),
        }

    t0 = time.time()
    try:
        projects = load_projects(_migrate=False)
        checks["projects"] = {
            "status": "ok",
            "count": len(projects),
            "ms": round((time.time() - t0) * 1000, 1),
        }
    except Exception as exc:
        checks["projects"] = {
            "status": "error",
            "error": type(exc).__name__,
            "ms": round((time.time() - t0) * 1000, 1),
        }

    t0 = time.time()
    try:
        db_path = _active_state_db_path()
        if not db_path.exists():
            checks["state_db"] = {
                "status": "missing",
                "ms": round((time.time() - t0) * 1000, 1),
            }
        else:
            with closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute("PRAGMA schema_version").fetchone()
            checks["state_db"] = {
                "status": "ok",
                "ms": round((time.time() - t0) * 1000, 1),
            }
    except Exception as exc:
        checks["state_db"] = {
            "status": "error",
            "error": type(exc).__name__,
            "ms": round((time.time() - t0) * 1000, 1),
        }

    healthy = all(
        check.get("status") in {"ok", "missing"}
        for check in checks.values()
    )
    return checks, healthy


def _handle_health(handler, parsed):
    deep = parse_qs(parsed.query or "").get("deep", [""])[0].lower() in {"1", "true", "yes", "on"}
    stream_check = _streams_lock_health()
    run_check = _run_lifecycle_health()
    payload = {
        "status": "ok" if stream_check.get("status") == "ok" else "degraded",
        "sessions": len(SESSIONS),
        "active_streams": int(stream_check.get("active_streams") or 0),
        "active_runs": int(run_check.get("active_runs") or 0),
        "runs": run_check.get("runs", []),
        "last_run_finished_at": run_check.get("last_run_finished_at"),
        "server_started_at": SERVER_START_TIME,
        "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
        "accept_loop": _accept_loop_health(handler),
    }
    if "oldest_run_age_seconds" in run_check:
        payload["oldest_run_age_seconds"] = run_check["oldest_run_age_seconds"]
    if "idle_seconds_since_last_run" in run_check:
        payload["idle_seconds_since_last_run"] = run_check["idle_seconds_since_last_run"]
    if deep:
        if stream_check.get("status") != "ok":
            payload["checks"] = {"streams_lock": stream_check}
            return j(handler, payload, status=503)
        checks, healthy = _deep_health_checks(stream_check=stream_check)
        payload["checks"] = checks
        if not healthy:
            payload["status"] = "degraded"
            return j(handler, payload, status=503)
    if payload["status"] != "ok":
        return j(handler, payload, status=503)
    return j(handler, payload)


# ── Plugin visibility endpoint (#539) ───────────────────────────────────────
_PLUGIN_VISIBILITY_HOOKS = (
    "pre_tool_call",
    "post_tool_call",
    "pre_llm_call",
    "post_llm_call",
)
_PLUGIN_VISIBILITY_HOOK_SET = set(_PLUGIN_VISIBILITY_HOOKS)


def _get_plugin_manager_for_visibility():
    """Return Hermes Agent's plugin manager for read-only WebUI visibility."""
    from hermes_cli.plugins import get_plugin_manager

    return get_plugin_manager()


def _clean_plugin_visibility_text(value, *, limit=240) -> str:
    """Return bounded display text without path/callback-like internals."""
    if value is None:
        return ""
    text = str(value).replace("\x00", "").strip()
    # Display metadata should be plain labels/descriptions. Drop multiline text
    # and common path separators rather than risk leaking local plugin paths.
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _plugin_visibility_category_from_key(key: str) -> str:
    """Return the config category prefix for nested plugin keys."""
    raw = str(key or "").strip().replace("\\", "/")
    if "/" not in raw:
        return ""
    category = raw.split("/", 1)[0].strip()
    return category if category and category not in {".", ".."} else ""


def _plugin_visibility_selected_provider(category: str) -> str:
    """Read ``<category>.provider`` without surfacing config errors."""
    if not category:
        return ""
    try:
        from api.config import get_config as _get_cfg

        cfg = _get_cfg() or {}
    except Exception:
        return ""
    category_cfg = cfg.get(category, {}) if isinstance(cfg, dict) else {}
    if not isinstance(category_cfg, dict):
        return ""
    return str(category_cfg.get("provider") or "").strip().lower()


def _plugin_visibility_payload(manager=None) -> dict:
    """Build a sanitized plugin/hook visibility payload for Settings.

    The Hermes Agent manager stores manifests and callback objects internally.
    This endpoint intentionally exposes only safe, user-facing metadata and the
    four lifecycle hook names called out by the Settings visibility MVP. It
    never includes plugin source paths, callback names, callback reprs, or raw
    load errors because those can contain private filesystem details.

    Exclusive plugins (e.g. memory providers) are activated through their
    category's ``<category>.provider`` config, not through ``plugins.enabled``.
    Their ``loaded.enabled`` stays False by design and they register hooks
    outside the four visibility hooks below. The payload surfaces ``kind``
    and ``activation`` plus ``is_active_provider`` when the plugin key carries a
    category prefix so the panel can render the selected provider distinctly
    instead of mislabeling it as "Disabled" with no hooks (issue #2659), while
    still showing unselected exclusive providers as disabled. Flat-key exclusive
    plugins omit the new field so older activation-based badge semantics remain
    intact when the category cannot be inferred.
    """
    manager = manager or _get_plugin_manager_for_visibility()
    manager.discover_and_load(force=False)

    plugins = []

    # Hermes Agent lifecycle-hook plugins
    raw_plugins = getattr(manager, "_plugins", {}) or {}
    for key, loaded in sorted(raw_plugins.items(), key=lambda item: str(item[0])):
        manifest = getattr(loaded, "manifest", None)
        if manifest is None:
            continue
        plugin_key = _clean_plugin_visibility_text(
            getattr(manifest, "key", None) or key or getattr(manifest, "name", ""),
            limit=120,
        )
        name = _clean_plugin_visibility_text(getattr(manifest, "name", "") or plugin_key, limit=120)
        version = _clean_plugin_visibility_text(getattr(manifest, "version", ""), limit=80)
        description = _clean_plugin_visibility_text(getattr(manifest, "description", ""), limit=280)
        kind = _clean_plugin_visibility_text(getattr(manifest, "kind", "") or "standalone", limit=40)
        enabled_flag = bool(getattr(loaded, "enabled", False))
        category = _plugin_visibility_category_from_key(plugin_key)
        selected_provider = _plugin_visibility_selected_provider(category)
        plugin_slug = plugin_key.rsplit("/", 1)[-1].strip().lower()
        if kind == "exclusive":
            activation = "exclusive"
        elif kind == "model-provider" and enabled_flag:
            activation = "provider"
        else:
            activation = "enabled" if enabled_flag else "disabled"
        include_active_provider = True
        if kind == "exclusive":
            if category:
                is_active_provider = bool(selected_provider) and plugin_slug == selected_provider
            else:
                include_active_provider = False
                is_active_provider = False
        else:
            is_active_provider = kind == "model-provider" and enabled_flag
        registered = []
        for hook in list(getattr(manifest, "provides_hooks", []) or []) + list(getattr(loaded, "hooks_registered", []) or []):
            hook_name = str(hook or "").strip()
            if hook_name in _PLUGIN_VISIBILITY_HOOK_SET and hook_name not in registered:
                registered.append(hook_name)
        registered.sort(key=_PLUGIN_VISIBILITY_HOOKS.index)
        plugin_payload = {
            "name": name,
            "key": plugin_key or name,
            "version": version,
            "description": description,
            # `enabled` is preserved for back-compat with older WebUI clients
            # that key off it directly. New clients should prefer `activation`.
            "enabled": enabled_flag,
            "kind": kind,
            "activation": activation,
            "hooks": registered,
        }
        if include_active_provider:
            plugin_payload["is_active_provider"] = bool(is_active_provider)
        plugins.append(plugin_payload)

    return {
        "plugins": plugins,
        "empty": not bool(plugins),
        "supported_hooks": list(_PLUGIN_VISIBILITY_HOOKS),
        "read_only": True,
    }


# WebUI dashboard plugins (from manifest.json discovery)
def _dashboard_plugin_enabled(plugin_name: str) -> bool:
    """True if a dashboard plugin is enabled in settings.

    Dashboard plugins are opt-in (default off). Enforced server-side so a
    disabled plugin's page + asset URLs are fully 404'd, not merely hidden in
    the Settings UI.
    """
    try:
        from api.config import load_settings
        prefs = (load_settings() or {}).get("dashboard_plugins", {}) or {}
        return bool(prefs.get(plugin_name, False))
    except Exception:
        return False


def _webui_plugin_payload() -> list[dict]:
    try:
        from api.plugins import get_plugin_metadata
        return get_plugin_metadata()
    except Exception:
        return []


def _handle_plugins(handler, parsed) -> bool:
    try:
        hermes_plugins = _plugin_visibility_payload()
        webui = _webui_plugin_payload()
        all_plugins = hermes_plugins["plugins"] + webui
        return j(handler, {
            "plugins": all_plugins,
            "empty": not bool(all_plugins),
            "supported_hooks": hermes_plugins["supported_hooks"],
            "read_only": True,
        })
    except Exception as exc:
        logger.warning("Failed to build plugin visibility payload: %s", exc)
        return j(
            handler,
            {
                "plugins": [],
                "empty": True,
                "supported_hooks": list(_PLUGIN_VISIBILITY_HOOKS),
                "read_only": True,
                "unavailable": True,
            },
        )


_SHELL_ERROR_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Hermes is restarting</title>
</head>
<body style=\"margin:0;padding:2rem;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#111827;color:#e5e7eb;\">
  <main style=\"max-width:40rem;margin:10vh auto;line-height:1.5;\">
    <h1 style=\"font-size:1.5rem;margin:0 0 0.75rem;\">Hermes is restarting…</h1>
    <p style=\"margin:0;color:#cbd5e1;\">The WebUI shell could not load cleanly. Refresh in a moment if this page does not update automatically.</p>
  </main>
</body>
</html>"""


def _serve_shell_unavailable(handler, exc: Exception) -> bool:
    """Return HTML for shell-route failures so `/` never renders JSON."""
    logger.warning("Failed to serve WebUI shell route: %s", exc)
    t(
        handler,
        _SHELL_ERROR_HTML,
        status=503,
        content_type="text/html; charset=utf-8",
    )
    return True


_SHUTDOWN_LOG_VALUE_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _shutdown_log_value(value, *, default: str = "unknown", max_len: int = 160) -> str:
    """Return a bounded single-line value safe for shutdown diagnostics."""
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    text = _SHUTDOWN_LOG_VALUE_RE.sub("?", text).strip()
    if not text:
        return default
    if len(text) > max_len:
        text = f"{text[:max_len]}…"
    return text


def _handle_shutdown(handler) -> bool:
    """Shut down the WebUI server process."""
    headers = getattr(handler, "headers", {})
    ua = headers.get("User-Agent", "no-ua") if hasattr(headers, "get") else "no-ua"
    remote = "unknown"
    if getattr(handler, "client_address", None):
        remote = getattr(handler, "client_address", ("unknown",))[0]
    logger.info(
        "[shutdown-request] remote=%s method=%s path=%s ua=%s",
        _shutdown_log_value(remote),
        _shutdown_log_value(getattr(handler, "command", None)),
        _shutdown_log_value(getattr(handler, "path", None), max_len=240),
        _shutdown_log_value(ua, default="no-ua", max_len=240),
    )
    j(handler, {"status": "shutting_down"})
    import signal
    import threading

    def _do_shutdown():
        import time
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return True


def _handle_health_restart(handler) -> bool:
    """Restart the Hermes messaging gateway service."""
    outcome = restart_active_profile_gateway()

    if outcome.get("status") == "completed":
        return j(handler, {"ok": True, "message": "Gateway service restarted successfully"})

    if outcome.get("status") == "in_progress":
        return j(handler, {"ok": True, "message": "Gateway service restart initiated (in progress)"})

    if outcome.get("status") == "busy":
        return j(
            handler,
            {"ok": False, "error": outcome.get("message", "Restart already in progress. Please wait a moment and try again.")},
            status=429,
        )

    return j(
        handler,
        {"ok": False, "error": outcome.get("message", "Internal error running restart")},
        status=500,
    )


def _serve_manifest(handler) -> bool:
    """Serve static/manifest.json with the correct PWA Content-Type.

    Shared by the root (/manifest.json, /manifest.webmanifest) and
    session-prefixed (/session/manifest.json, /session/manifest.webmanifest)
    routes so Firefox Android can fetch the manifest when installing from
    a /session/<id> page.  See #2226.
    """
    static_root = api_config.get_static_root()
    manifest_path = (static_root / "manifest.json").resolve()
    if manifest_path.exists():
        data = manifest_path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True
    return j(handler, {"error": "not found"}, status=404)


def _saved_prompts_path() -> "Path":
    try:
        from api.profiles import get_active_hermes_home
        return Path(get_active_hermes_home()).expanduser() / "webui" / "saved_prompts.json"
    except Exception:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser() / "webui" / "saved_prompts.json"


def _load_saved_prompts() -> list:
    p = _saved_prompts_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_saved_prompts(prompts: list) -> None:
    p = _saved_prompts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")


# In-process cache for the app-shell template. The `/`, `/index.html`, and
# `/session/<id>` routes are the hottest navigations and each re-read the
# ~190 KB static/index.html from disk and re-ran the two process-constant
# substitutions (__WEBUI_VERSION__, __MAX_UPLOAD_BYTES__) on every request.
# Those values are fixed for the process lifetime, so we cache the partially
# rendered template here, keyed by (size, nanosecond mtime) exactly like
# _STATIC_CACHE so a redeploy is picked up without a restart. The two values
# that genuinely vary per request — the per-session CSRF token and the runtime
# extension tags (inject_extension_tags) — are still applied on each request
# against the cached base, so caching changes no observable output.
_INDEX_SHELL_CACHE: dict = {}
_INDEX_SHELL_CACHE_LOCK = threading.Lock()


def _render_index_shell_base() -> str:
    """Return static/index.html with the process-constant tokens substituted.

    Cached and invalidated on (size, mtime_ns) change. The CSRF token and
    extension-tag injection are intentionally NOT applied here — they vary per
    request and are applied by the caller against this base string.
    """
    from api.updates import WEBUI_VERSION

    index_path = api_config.get_index_html_path()
    st = index_path.stat()
    sig = (index_path, st.st_size, st.st_mtime_ns)
    with _INDEX_SHELL_CACHE_LOCK:
        cached = _INDEX_SHELL_CACHE.get("base")
        if cached and cached[0] == sig:
            return cached[1]
    from urllib.parse import quote

    version_token = quote(WEBUI_VERSION, safe="")
    base = (
        index_path.read_text(encoding="utf-8")
        .replace("__WEBUI_VERSION__", version_token)
        .replace("__MAX_UPLOAD_BYTES__", str(MAX_UPLOAD_BYTES))
    )
    with _INDEX_SHELL_CACHE_LOCK:
        _INDEX_SHELL_CACHE["base"] = (sig, base)
    return base


def handle_get(handler, parsed) -> bool:
    """Handle all GET routes. Returns True if handled, False for 404."""
    proxy_result = _handle_extension_sidecar_proxy(handler, parsed, "GET")
    if proxy_result is not False:
        return proxy_result

    if parsed.path.startswith("/session/static/"):
        # Strip the leading "/session" so _serve_static() sees a path that
        # starts with "/static/" (its required prefix). _serve_static enforces
        # its own path-traversal sandbox via Path.resolve()+relative_to().
        stripped = parsed._replace(path=parsed.path[len("/session"):])
        return _serve_static(handler, stripped)

    # Firefox Android resolves <link rel="manifest"> against the page URL
    # before the dynamic <base href> script runs when installing from
    # /session/<id>, producing requests like /session/manifest.json.
    # Without this guard the catch-all below returns index.html instead of
    # the manifest, and Firefox falls back to a generated letter icon.
    # See #2226.
    if parsed.path in ("/session/manifest.json", "/session/manifest.webmanifest"):
        return _serve_manifest(handler)

    if parsed.path in ("/", "/index.html") or parsed.path.startswith("/session/"):
        try:
            from api.extensions import inject_extension_tags

            csrf_token = ""
            try:
                from api.auth import csrf_token_for_session, is_auth_enabled, parse_cookie, verify_session

                if is_auth_enabled():
                    cookie_val = parse_cookie(handler)
                    if not cookie_val:
                        cookie_val = getattr(handler, "_trusted_auth_session_cookie_value", None)
                    if cookie_val and verify_session(cookie_val):
                        csrf_token = csrf_token_for_session(cookie_val) or ""
            except Exception:
                csrf_token = ""

            # The disk read + process-constant token substitutions are cached;
            # only the per-session CSRF token and per-request extension tags are
            # applied here (see _render_index_shell_base).
            html = _render_index_shell_base().replace(
                "__CSRF_TOKEN_JSON__", json.dumps(csrf_token)
            )
            return t(
                handler,
                inject_extension_tags(html),
                content_type="text/html; charset=utf-8",
            )
        except Exception as exc:
            return _serve_shell_unavailable(handler, exc)

    if parsed.path == "/share" or parsed.path.startswith("/share/"):
        share_path = (Path(__file__).parent.parent / "static" / "share.html").resolve()
        return t(
            handler,
            share_path.read_text(encoding="utf-8"),
            content_type="text/html; charset=utf-8",
            extra_headers={
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    if parsed.path == "/login":
        _settings = load_settings()
        _bn = _html.escape(_settings.get("bot_name") or "Hermes")
        _lang = _settings.get("language", "en")
        _login_strings = _LOGIN_LOCALE[
            _resolve_login_locale_key(_lang)
        ]
        from urllib.parse import quote
        from api.updates import WEBUI_VERSION
        version_token = quote(WEBUI_VERSION, safe="")
        _page = (
            _LOGIN_PAGE_HTML.replace("{{BOT_NAME}}", _bn)
            .replace("{{BOT_NAME_INITIAL}}", _bn[0].upper())
            .replace("{{WEBUI_VERSION}}", version_token)
            .replace("{{LANG}}", _html.escape(_login_strings["lang"]))
            .replace("{{LOGIN_TITLE}}", _html.escape(_login_strings["title"]))
            .replace("{{LOGIN_SUBTITLE}}", _html.escape(_login_strings["subtitle"]))
            .replace(
                "{{LOGIN_PLACEHOLDER}}", _html.escape(_login_strings["placeholder"])
            )
            .replace("{{LOGIN_BTN}}", _html.escape(_login_strings["btn"]))
            .replace("{{LOGIN_INVALID_PW}}", _html.escape(_login_strings["invalid_pw"]))
            .replace(
                "{{LOGIN_CONN_FAILED}}", _html.escape(_login_strings["conn_failed"])
            )
            .replace("{{OIDC_LOGIN_HTML}}", _oidc_login_html(parsed))
        )
        return t(handler, _page, content_type="text/html; charset=utf-8")

    if parsed.path == "/api/auth/oidc/start":
        from api.auth import OIDCAuthError, OIDCConfigError, build_authorization_redirect

        next_path = _safe_login_redirect_path(
            parse_qs(parsed.query or "").get("next", [""])[0]
        )
        try:
            location = build_authorization_redirect(
                _request_base_url(handler), next_path
            )
        except OIDCConfigError as exc:
            return j(handler, {"error": str(exc)}, status=404)
        except OIDCAuthError as exc:
            return j(handler, {"error": str(exc)}, status=exc.status_code)
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        _security_headers(handler)
        handler.end_headers()
        return True

    if parsed.path == "/api/auth/oidc/callback":
        from api.auth import create_session, set_auth_cookie
        from api.auth import OIDCAuthError, OIDCConfigError, complete_authorization_code_flow

        query = parse_qs(parsed.query or "")
        error = str(query.get("error", [""])[0] or "").strip()
        if error:
            description = str(query.get("error_description", [""])[0] or "").strip()
            return j(handler, {"error": description or error}, status=401)
        state = str(query.get("state", [""])[0] or "").strip()
        code = str(query.get("code", [""])[0] or "").strip()
        if not state or not code:
            return j(handler, {"error": "Missing OIDC callback state or code"}, status=400)
        try:
            result = complete_authorization_code_flow(
                _request_base_url(handler), state, code
            )
        except OIDCConfigError as exc:
            return j(handler, {"error": str(exc)}, status=404)
        except OIDCAuthError as exc:
            return j(handler, {"error": str(exc)}, status=exc.status_code)
        cookie_val = create_session()
        handler.send_response(302)
        handler.send_header(
            "Location",
            _safe_login_redirect_path(result.get("next_path")),
        )
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        set_auth_cookie(handler, cookie_val)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True

    if parsed.path == "/api/auth/status":
        from api.auth import (
            _passkey_feature_flag_enabled,
            ensure_trusted_auth_session,
            get_password_hash,
            is_auth_enabled,
            is_oidc_auth_enabled,
            is_trusted_auth_enabled,
        )
        from api.auth import registered_credentials

        logged_in = False
        session_info = None
        auth_enabled = is_auth_enabled()
        oidc_enabled = is_oidc_auth_enabled()
        if auth_enabled:
            session_info = ensure_trusted_auth_session(handler)
            logged_in = bool(session_info)
        passkey_flag = _passkey_feature_flag_enabled()
        passkeys = registered_credentials() if passkey_flag else []
        password_auth_enabled = get_password_hash() is not None
        payload = {
            "auth_enabled": auth_enabled,
            "logged_in": logged_in,
            "oidc_enabled": oidc_enabled,
            "password_auth_enabled": password_auth_enabled,
            "passwordless_enabled": bool(passkeys) and not password_auth_enabled,
            "passkeys_enabled": bool(passkeys),
            "passkeys_count": len(passkeys),
            "passkey_feature_flag": passkey_flag,
            "auth_disabled_acknowledged": bool(load_settings().get("auth_disabled_acknowledged")) if not auth_enabled else False,
        }
        if is_trusted_auth_enabled() or (session_info and session_info.get("auth_type") == "trusted"):
            payload["trusted_auth_enabled"] = True
        if session_info and session_info.get("auth_type") == "trusted":
            payload["auth_type"] = session_info.get("auth_type")
            payload["user"] = session_info.get("username")
            payload["bound_profile"] = session_info.get("bound_profile")
        return j(handler, payload)

    if parsed.path.startswith("/api/share/"):
        token = parsed.path[len("/api/share/"):].strip()
        share = load_share(token)
        if not share:
            return bad(handler, "Shared conversation not found", 404)
        return j(
            handler,
            {"share": share},
            extra_headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    if parsed.path in ("/manifest.json", "/manifest.webmanifest"):
        return _serve_manifest(handler)

    if parsed.path == "/sw.js":
        static_root = api_config.get_static_root()
        sw_path = (static_root / "sw.js").resolve()
        if sw_path.exists():
            # Inject the current git-derived version as the cache name so the
            # service worker cache busts automatically on every new deploy.
            from urllib.parse import quote
            from api.updates import WEBUI_VERSION
            version_token = quote(WEBUI_VERSION, safe="")
            text = sw_path.read_text(encoding="utf-8").replace(
                "__WEBUI_VERSION__", version_token
            )
            data = text.encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/javascript; charset=utf-8")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Service-Worker-Allowed", "/")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return True
        return j(handler, {"error": "not found"}, status=404)

    if parsed.path == "/favicon.ico":
        static_root = api_config.get_static_root()
        ico_path = (static_root / "favicon.ico").resolve()
        if ico_path.exists() and ico_path.is_file():
            data = ico_path.read_bytes()
            handler.send_response(200)
            handler.send_header("Content-Type", "image/x-icon")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "public, max-age=86400")
            handler.end_headers()
            handler.wfile.write(data)
        else:
            handler.send_response(204)
            handler.end_headers()
        return True

    if parsed.path.startswith("/api/") and not _guard_request_session_visibility(handler, parsed, method="GET"):
        return True

    # ── Insights / knowledge status ──
    if parsed.path == "/api/insights":
        return _handle_insights(handler, parsed)
    if parsed.path == "/api/project-os/dashboard":
        return _handle_project_os_dashboard(handler, parsed)

    if parsed.path.startswith("/api/kanban/"):
        from api.kanban_bridge import handle_kanban_get

        # Only treat an explicit False as "no route matched". None means the
        # bridge already sent a response via bad()/j() — emitting our own 404
        # on top of that produces concatenated JSON bodies on the wire.
        result = handle_kanban_get(handler, parsed)
        if result is False:
            return _kanban_unknown_endpoint(handler, parsed, "GET")
        return True
    if parsed.path == "/api/wiki/status":
        return _handle_llm_wiki_status(handler, parsed)
    if parsed.path == "/api/wiki/browse":
        return _handle_llm_wiki_browse(handler, parsed)
    if parsed.path == "/api/wiki/page":
        return _handle_llm_wiki_page(handler, parsed)
    if parsed.path == "/api/logs":
        return _handle_logs(handler, parsed)

    if parsed.path == "/health":
        return _handle_health(handler, parsed)

    if parsed.path == "/api/health/agent":
        payload = build_agent_health_payload()
        payload["gateway_chat"] = gateway_chat_config_status()
        j(handler, payload)
        return True

    if parsed.path == "/api/system/health":
        j(handler, build_system_health_payload())
        return True

    if parsed.path == "/api/models":
        # Profile-scoping for non-default profiles (#3957) is handled INSIDE
        # get_available_models() — it binds the active profile's env + TLS on
        # the detached rebuild worker (and the legacy synchronous rebuild),
        # which the request-thread wrapper could not reach. See
        # api.config.get_available_models cold path + profile_scope_for_detached_worker.
        freshness = parse_qs(parsed.query or "").get("freshness", [""])[0].strip().lower()
        diag = RequestDiagnostics.maybe_start("GET", parsed.path, logger=logger, print_fn=getattr(handler, '_safe_webui_print', None))
        try:
            diag.stage(f"enter:freshness={freshness or 'default'}") if diag else None
            if freshness == "session_visit":
                result = get_available_models_for_session_visit()
                diag.stage("response_serialize") if diag else None
                return j(handler, result)
            if freshness:
                return bad(handler, f"unknown models freshness: {freshness}", status=400)
            return j(handler, get_available_models())
        finally:
            if diag:
                diag.finish()

    if parsed.path == "/api/models/live":
        from api.profiles import profile_env_for_active_request
        with profile_env_for_active_request("/api/models/live", logger_override=logger):
            return _handle_live_models(handler, parsed)

    # ── Auxiliary models (GET/POST) ──
    if parsed.path == "/api/model/auxiliary":
        from api.config import get_auxiliary_models
        return j(handler, get_auxiliary_models())

    if parsed.path == "/api/dashboard/status":
        from api import dashboard_probe

        j(handler, dashboard_probe.get_dashboard_status())
        return True

    if parsed.path == "/api/dashboard/config":
        from api import dashboard_probe

        try:
            j(handler, dashboard_probe.get_dashboard_config())
        except ValueError as exc:
            bad(handler, str(exc), status=400)
        return True

    # ── Providers (GET) ──
    if parsed.path == "/api/providers":
        # Apply the active per-request profile's env so provider auth probes
        # resolve against that profile's credentials, not the process-default
        # profile's (#3957). Without this, get_auth_status() probes on a
        # non-default profile resolve the wrong/empty creds and can stall past
        # the 30s frontend timeout. No-op for the default profile.
        from api.profiles import profile_env_for_active_request_readonly
        with profile_env_for_active_request_readonly("/api/providers", logger_override=logger):
            return j(handler, get_providers())

    # ── Plugins/hooks visibility (read-only, no callback/source internals) ──
    if parsed.path == "/api/plugins":
        return _handle_plugins(handler, parsed)
    if parsed.path == "/api/provider/quota":
        query = parse_qs(parsed.query)
        provider_id = (query.get("provider", [""])[0] or None)
        refresh = (query.get("refresh", [""])[0] or "").strip().lower() in {"1", "true", "yes", "on"}
        # Bind the active request's profile env (matches /api/providers and
        # /api/models/live). #4365 added a credential_pool.load_pool() path in
        # get_provider_quota for all pooled providers; without this wrapper that
        # read/write runs under the process-default profile, so a multi-profile
        # client would see (and seed) the default profile's pool instead of its
        # own (#4247/#4067 profile-isolation class).
        from api.profiles import profile_env_for_active_request_readonly
        with profile_env_for_active_request_readonly("/api/provider/quota", logger_override=logger):
            return j(handler, get_provider_quota(provider_id, refresh=refresh))

    if parsed.path == "/api/provider/cost-history":
        query = parse_qs(parsed.query)
        provider_id = (query.get("provider", [""])[0] or None)
        days_raw = (query.get("days", ["7"])[0] or "7").strip()
        try:
            days = max(1, min(int(days_raw), 365))
        except (ValueError, TypeError):
            days = 7
        return j(handler, get_provider_cost_history(provider_id, days))

    if parsed.path == "/api/settings":
        settings = load_settings()
        settings["persisted_speech_keys"] = persisted_speech_settings_keys()
        # Never expose the stored password hash to clients
        settings.pop("password_hash", None)
        settings.setdefault("max_tokens", None)
        settings.setdefault("max_tokens_effective", None)
        settings.setdefault("max_tokens_fallback", None)
        try:
            from api.config import get_max_tokens_status
            settings.update(get_max_tokens_status())
        except Exception:
            settings["max_tokens"] = None
            settings["max_tokens_effective"] = None
            settings["max_tokens_fallback"] = None
        # Surface env-var precedence so the UI can disable the password field
        # instead of silently no-oping the save (#1560). The setting takes
        # precedence in api.auth.get_password_hash(), but until now the UI
        # had no way to know — see issue #1139 / #1560.
        settings["password_env_var"] = bool(
            os.getenv("HERMES_WEBUI_PASSWORD", "").strip()
        )
        # Auth-state fields for frontend safety badge / confirmation flows
        from api.auth import get_password_hash, is_auth_enabled
        settings["auth_enabled"] = is_auth_enabled()
        settings["password_auth_enabled"] = get_password_hash() is not None
        try:
            from api.auth import _passkey_feature_flag_enabled as _pffe
            from api.auth import registered_credentials as _rc
            if _pffe():
                settings["passkeys_enabled"] = bool(_rc())
                settings["passwordless_enabled"] = bool(_rc()) and not settings["password_auth_enabled"]
            else:
                settings["passkeys_enabled"] = False
                settings["passwordless_enabled"] = False
        except Exception:
            pass
        # Inject the running version so the UI badge stays in sync with git tags
        # without any manual release step.
        try:
            from api.updates import AGENT_VERSION, WEBUI_VERSION
            settings["webui_version"] = WEBUI_VERSION
            settings["agent_version"] = AGENT_VERSION
        except Exception:
            pass
        # Channel-scoped display badge — SEPARATE from webui_version (which is
        # load-bearing for asset cache-busting / SW cache / skew detection and
        # must stay channel-neutral). update_channel_version is display-only.
        try:
            from api.updates import channel_version_badge, _read_update_channel
            channel = _read_update_channel()
            settings["update_channel"] = channel
            settings["update_channel_version"] = channel_version_badge(channel)
        except Exception:
            pass
        return j(handler, settings)

    if parsed.path == "/api/transcribe/capability":
        return handle_transcribe_capability(handler)

    if parsed.path == "/api/reasoning":
        # Current reasoning config (shared source of truth with the CLI —
        # reads display.show_reasoning and agent.reasoning_effort from
        # the active profile's config.yaml).
        query = parse_qs(parsed.query)
        model_id = (query.get("model", [""])[0] or "").strip() or None
        provider_id = (query.get("provider", [""])[0] or "").strip() or None
        base_url = (query.get("base_url", [""])[0] or "").strip() or None
        return j(
            handler,
            get_reasoning_status(
                model_id=model_id,
                provider_id=provider_id,
                base_url=base_url,
            ),
        )

    if parsed.path == "/api/onboarding/status":
        return j(handler, get_onboarding_status())

    if parsed.path == "/api/extensions/status":
        from api.extensions import get_extension_status

        return j(handler, get_extension_status())

    if parsed.path == "/api/extensions/registry":
        from api.extensions import get_extension_registry

        return j(handler, get_extension_registry())

    if parsed.path.startswith("/extensions/"):
        from api.extensions import serve_extension_static

        return serve_extension_static(handler, parsed)

    if parsed.path.startswith("/static/"):
        return _serve_static(handler, parsed)


    if parsed.path == "/api/session/worktree/status":
        query = parse_qs(parsed.query)
        sid = query.get("session_id", [""])[0]
        if not sid:
            return bad(handler, "session_id is required", status=400)
        try:
            s = get_session(sid, metadata_only=True)
        except KeyError:
            return bad(handler, "Session not found", status=404)
        try:
            from api.worktrees import worktree_status_for_session

            return j(handler, {"status": worktree_status_for_session(s)})
        except ValueError as exc:
            return bad(handler, str(exc), status=400)
        except Exception as exc:
            logger.exception("failed to read worktree status for session %s", sid)
            return bad(handler, _sanitize_error(exc), status=500)

    if parsed.path == "/api/session/compress/status":
        query = parse_qs(parsed.query)
        _handle_session_compress_status(handler, query.get("session_id", [""])[0])
        return True

    if parsed.path == "/api/session":
        import time as _time
        _t0 = _time.monotonic()
        _debug_slow = os.environ.get("HERMES_DEBUG_SLOW", "")
        # perf(webui/session-load-latency) tier2c: per-stage breakdown via
        # RequestDiagnostics. maybe_start() returns None for paths not in
        # the allowlist, in which case the existing _tN-driven [SLOW] log
        # is the only signal — same as before.
        _diag = RequestDiagnostics.maybe_start("GET", parsed.path, logger=logger, print_fn=getattr(handler, '_safe_webui_print', None))
        # perf(webui/session-load-latency) tier2c-followup: every early-return
        # in this handler calls `_diag.finish()` before returning so the
        # watchdog's _watchdog_pending dict stays bounded to in-flight requests.
        # Greptile flagged this in PR review — finish() unregisters the
        # pending watchdog entry; without it the entry stays for the full
        # 5s slow-request timeout and emits a spurious "Slow WebUI request
        # still running" log. Idempotent — finish() no-ops if already called.
        query = parse_qs(parsed.query)
        sid = query.get("session_id", [""])[0]
        if not sid:
            if _diag: _diag.finish()
            return j(handler, {"error": "session_id is required"}, status=400)
        # ?messages=0 skips the message payload for fast session switching.
        # The frontend uses this when switching conversations in the sidebar
        # (only needs metadata). The full message array is loaded lazily
        # via ?messages=1 when the message panel opens.
        load_messages = query.get("messages", ["1"])[0] != "0"
        resolve_model_default = "1" if load_messages else "0"
        resolve_model = query.get("resolve_model", [resolve_model_default])[0] != "0"
        # ?msg_limit=N returns a tail window containing the last N visible
        # transcript rows. Hidden tool-result rows do not consume the budget;
        # they are included only when they sit inside the selected window and
        # are bounded before serialization. Older rows load on-demand.
        # Clamp to _MAX_MSG_LIMIT so an oversized request (e.g. msg_limit=9999
        # from an outline jump, or a hostile value) can't force an unbounded
        # payload; the existing _messages_truncated signal covers the clamped
        # case (the client sees there are more rows than returned). Parsing +
        # clamping live in _parse_msg_limit so the expression has direct test
        # coverage; None means the bare no-msg_limit path (full transcript).
        msg_limit = _parse_msg_limit(query.get("msg_limit", [None])[0])
        # ?msg_before=N — 0-based index into the full message array.
        # Returns messages before this index (for scroll-to-top lazy loading).
        # Combined with msg_limit for paging.
        _msg_before = query.get("msg_before", [None])[0]
        try:
            msg_before = int(_msg_before) if _msg_before else None
        except (ValueError, TypeError):
            msg_before = None
        # ?expand_renderable=1 is retained for compatibility with older
        # frontends. msg_limit now counts visible transcript rows by default, so
        # the flag no longer changes the server-side pagination semantics.
        _expand_renderable = query.get("expand_renderable", [None])[0]
        expand_renderable = str(_expand_renderable).strip() in ("1", "true", "True")
        try:
            _t1 = _time.monotonic()
            if _diag: _diag.stage("t1_after_get_session_check")
            _detail_cache_candidate = (
                load_messages
                and msg_limit is not None
                and msg_before is None
                and not resolve_model
            )
            _detail_cache_key = None
            s = get_session(
                sid,
                metadata_only=(not load_messages) or _detail_cache_candidate,
            )
            _session_profile = getattr(s, 'profile', None) or None
            if not _session_visible_to_active_profile(_session_profile, handler):
                if _session_profile:
                    # Valid session owned by a KNOWN other profile: 409 so the
                    # client can offer to switch to it (#5419).
                    if _diag: _diag.finish()
                    return j(handler, {
                        "error": "Session belongs to a different profile",
                        "code": "session_profile_mismatch",
                        "session_id": sid,
                        "profile": _session_profile,
                    }, status=409)
                # Unknown/legacy None-profile sidecar: keep the original 404 so
                # the frontend's self-heal (clear stale URL + localStorage) still
                # fires. _profiles_match coerces None->'default', so a truly
                # missing/legacy session under a non-default active profile would
                # otherwise emit a useless 409 with profile=null.
                if _diag: _diag.finish()
                return bad(handler, "Session not found", 404)
            original_stream_id = getattr(s, "active_stream_id", None)
            _clear_stale_stream_state(s)
            if _detail_cache_candidate:
                _detail_cache_key = _session_detail_tail_cache_key(
                    s,
                    msg_limit=msg_limit,
                    expand_renderable=expand_renderable,
                )
                _cached_detail_payload = _session_detail_tail_cache_get(_detail_cache_key)
                if _cached_detail_payload is not None:
                    if _diag:
                        _diag.stage("session_detail_tail_cache_hit")
                        _diag.finish()
                    return j(handler, _cached_detail_payload)
                if _diag:
                    _diag.stage("session_detail_tail_cache_miss")
                if getattr(s, "_loaded_metadata_only", False):
                    s = get_session(sid, metadata_only=False)
                    _session_profile = getattr(s, 'profile', None) or None
                # A full cached object may have changed while the metadata-only
                # read was in flight. Re-evaluate eligibility before storing.
                if not _session_detail_tail_cache_eligible(s):
                    _detail_cache_key = None
            cli_meta = _lookup_cli_session_metadata(sid) if _session_requires_cli_metadata_lookup(s) else {}
            is_messaging_session = _is_messaging_session_record(s) or _is_messaging_session_record(cli_meta)
            cli_messages = []
            state_db_messages = []
            metadata_summary = None
            limited_sidecar_messages = None
            state_db_since_timestamp = None
            if is_messaging_session:
                cli_messages = get_cli_session_messages(sid)
            elif load_messages:
                if msg_limit is not None:
                    (
                        state_db_since_timestamp,
                        limited_sidecar_messages,
                    ) = _state_db_since_timestamp_for_limited_display(
                        s,
                        msg_limit,
                        msg_before=msg_before,
                    )
                _state_db_reader_kwargs = {"profile": _session_profile}
                if state_db_since_timestamp is not None:
                    _state_db_reader_kwargs["since_timestamp"] = state_db_since_timestamp
                # Apply the display-path row backstop ONLY on provably-safe
                # reads where no truncation_boundary prefix is required for the
                # merge — see _state_db_backstop_limit_for_display. Compressed
                # sessions and msg_before paging need their full prefix rows for
                # correct reconciliation, so those stay uncapped.
                _backstop = _state_db_backstop_limit_for_display(s, msg_before)
                if _backstop is not None:
                    _state_db_reader_kwargs["limit"] = _backstop
                state_db_messages = get_state_db_session_messages(
                    sid,
                    **_state_db_reader_kwargs,
                )
            elif not is_messaging_session:
                # Metadata-only callers still need the same append-only
                # reconciliation contract as full loads so stale/replayed
                # state.db rows do not make sidebar polling think the
                # transcript is always newer. Helper threads profile= to
                # honor #2827's TLS-vs-thread fix.
                metadata_summary = _metadata_only_message_summary(sid, profile=_session_profile)
            _t2 = _time.monotonic()
            if _diag: _diag.stage("t2_after_state_db_load")
            effective_model = (
                _resolve_effective_session_model_for_display(s)
                if resolve_model
                else None
            )
            effective_provider = (
                _resolve_effective_session_model_provider_for_display(s)
                if resolve_model
                else None
            )
            _t3 = _time.monotonic()
            if _diag: _diag.stage("t3_after_model_resolve")
            if load_messages:
                if is_messaging_session and cli_messages:
                    # Recovery/aggregate sidecars can intentionally contain a
                    # longer visible conversation than the single state.db
                    # segment for this messaging session id. Prefer the longer
                    # sidecar so repaired WebUI history is not hidden behind the
                    # canonical per-segment transcript. When both sources carry
                    # different slices of the same stitched conversation, merge
                    # them chronologically and dedupe exact repeats.
                    _all_msgs = _merged_session_messages_for_display(s, cli_messages)
                elif msg_limit is not None:
                    _all_msgs = _limited_webui_messages_for_display_with_sidecar(
                        s,
                        limited_sidecar_messages,
                        state_db_messages,
                    )
                else:
                    _all_msgs = merge_session_messages_append_only(
                        _webui_sidecar_lineage_messages_for_display(s),
                        state_db_messages,
                        truncation_watermark=getattr(s, "truncation_watermark", None),
                        truncation_boundary=getattr(s, "truncation_boundary", None),
                    )
                    _all_msgs = _merged_webui_lineage_messages_for_display(s, _all_msgs)
            else:
                if is_messaging_session and cli_messages:
                    _all_msgs = _merged_session_messages_for_display(s, cli_messages)
                else:
                    if metadata_summary is None:
                        metadata_summary = _message_summary(getattr(s, "messages", []) or [])
                    _summary_message_count = metadata_summary["message_count"]
                    _summary_last_message_at = metadata_summary["last_message_at"]
                    _all_msgs = []
            if not load_messages:
                if metadata_summary is None:
                    metadata_summary = _message_summary(_all_msgs)
                    _summary_message_count = metadata_summary["message_count"]
                    _summary_last_message_at = metadata_summary["last_message_at"]
                if _summary_message_count == 0:
                    # Legacy session with no loaded sidecar and no state.db summary —
                    # fall back to the persisted metadata count from session JSON.
                    # See PR #2605 (LumenYoung): without this, the metadata poll
                    # returns 0 and the active-session external-refresh signal
                    # never trips on legacy sessions.
                    try:
                        metadata_count = getattr(s, "_metadata_message_count", None)
                        if metadata_count is not None:
                            _summary_message_count = max(0, int(metadata_count))
                    except (TypeError, ValueError):
                        pass
            else:
                _summary_message_count = None
                _summary_last_message_at = None
            if load_messages:
                _truncated_msgs, _messages_offset = _message_window_for_display(
                    _all_msgs,
                    msg_limit=msg_limit,
                    msg_before=msg_before,
                    expand_renderable=expand_renderable,
                )
                if msg_limit is not None:
                    _truncated_msgs = _messages_for_limited_payload(_truncated_msgs)
                _truncated_msgs = _hydrate_anchor_activity_scenes(
                    _truncated_msgs,
                    getattr(s, "anchor_activity_scenes", None),
                    message_offset=_messages_offset,
                    tool_calls=getattr(s, "tool_calls", None),
                )
            else:
                _truncated_msgs = []
                _messages_offset = 0
            # Index of the first returned message in the full message array.
            # Frontend uses this as cursor for scroll-to-top paging.
            _windowed_messages = (
                load_messages
                and msg_limit is not None
                and (msg_before is not None or len(_truncated_msgs) < len(_all_msgs))
            )
            # Resolve effective context_length with model-metadata fallback so
            # older sessions (pre-#1318) that have context_length=0 persisted
            # still render a meaningful indicator on load.  Mirrors the
            # SSE-path fallback in api/streaming.py:2333-2342.  Fixes #1436.
            #
            # #1896: pass config_context_length, provider, and custom_providers
            # so explicit config overrides win over the 256K default fallback.
            # Without these, an old session loaded after a user upgraded to a
            # 1M-context model with `model.context_length: 1048576` in
            # config.yaml gets a 256K window in the initial UI indicator and
            # /api/session/get response — the same wrong-window display this
            # fix addresses on the streaming side.
            _persisted_cl = getattr(s, "context_length", 0) or 0
            _threshold_tokens = getattr(s, "threshold_tokens", 0) or 0
            if (not _persisted_cl) or resolve_model:
                _stored_model_for_lookup = getattr(s, "model", "") or ""
                _stored_provider_for_lookup = getattr(s, "model_provider", None) or ""
                _model_for_lookup = (
                    effective_model or _stored_model_for_lookup
                ).strip()
                (
                    _model_for_lookup,
                    _provider_for_lookup,
                    _base_url_for_lookup,
                    _api_key_for_lookup,
                ) = _session_context_length_lookup_state(
                    _model_for_lookup,
                    effective_provider or getattr(s, "model_provider", None) or "",
                )
                _fb_cl = _resolve_context_length_for_session_model(
                    _model_for_lookup,
                    _provider_for_lookup,
                    base_url=_base_url_for_lookup,
                    api_key=_api_key_for_lookup,
                )
                _model_changed_for_context = not _session_model_identity_matches(
                    _stored_model_for_lookup,
                    _stored_provider_for_lookup,
                    _model_for_lookup,
                    _provider_for_lookup,
                )
                if _should_accept_session_context_length_refresh(
                    _persisted_cl,
                    _fb_cl,
                    model_changed=_model_changed_for_context,
                ):
                    if _persisted_cl and _fb_cl != _persisted_cl:
                        # The old threshold belongs to the old window. Hiding it
                        # is less useful than keeping the same compression ratio
                        # against the freshly resolved context length.
                        _threshold_tokens = _rescale_threshold_tokens_for_context_window(
                            _threshold_tokens,
                            _persisted_cl,
                            _fb_cl,
                        )
                    _persisted_cl = _fb_cl
            _session_tool_calls = getattr(s, "tool_calls", []) if load_messages else []
            # Always include session-level tool_calls so the browser can merge
            # them with per-message tool_calls for messages that lack the
            # per-message variant (older messages whose tool_calls live only
            # in the session-level list).  The browser-side
            # _syncToolCallsForLoadedMessages handles deduplication by tid.
            if _windowed_messages:
                _session_tool_calls = _tool_calls_for_message_window(
                    _session_tool_calls,
                    _messages_offset,
                    len(_truncated_msgs),
                )
            _merged_message_count = _summary_message_count if _summary_message_count is not None else len(_all_msgs)
            _merged_last_message_at = _summary_last_message_at if _summary_last_message_at is not None else 0
            if _summary_last_message_at is None and _all_msgs:
                try:
                    _merged_last_message_at = max(
                        float((m or {}).get("timestamp") or 0)
                        for m in _all_msgs
                        if isinstance(m, dict)
                    )
                except (TypeError, ValueError):
                    _merged_last_message_at = 0
            active_stream_ids = _active_stream_ids()
            try:
                compact_session = s.compact(
                    include_runtime=True,
                    active_stream_ids=active_stream_ids,
                )
            except TypeError:
                compact_session = s.compact()
            raw = compact_session | {
                "messages": _truncated_msgs,
                "message_count": _merged_message_count,
                "tool_calls": _session_tool_calls,
                "active_stream_id": getattr(s, "active_stream_id", None),
                "pending_user_message": getattr(s, "pending_user_message", None),
                "pending_attachments": getattr(s, "pending_attachments", []) if load_messages else [],
                "pending_started_at": getattr(s, "pending_started_at", None),
                "pending_user_source": getattr(s, "pending_user_source", None),
                "context_length": _persisted_cl,
                "threshold_tokens": _threshold_tokens,
                "last_prompt_tokens": getattr(s, "last_prompt_tokens", 0) or 0,
            }
            if original_stream_id:
                try:
                    journal = find_run_summary(original_stream_id)
                except Exception:
                    journal = None
                if journal:
                    journal_active = bool(original_stream_id in active_stream_ids)
                    raw["runtime_journal"] = _run_journal_status_payload(
                        journal,
                        active=journal_active,
                    )
                    if journal_active:
                        try:
                            snapshot = _run_journal_live_snapshot(original_stream_id, handler=handler)
                        except Exception:
                            logger.debug(
                                "Failed to build runtime journal snapshot for %s",
                                original_stream_id,
                                exc_info=True,
                            )
                            snapshot = None
                        if snapshot:
                            raw["runtime_journal_snapshot"] = snapshot
                            raw["pending_attachments"] = getattr(s, "pending_attachments", []) or []
            # Cold-load: derive the latest settled todo snapshot from the full
            # merged transcript, not the truncated display window. This keeps
            # the Todos panel correct after refresh even when the latest todo
            # tool result is outside msg_limit, and treats an explicit empty
            # todo list as the current state instead of falling through to an
            # older non-empty write.
            if load_messages and _all_msgs:
                attach_todo_state(raw, _all_msgs)
            if _merged_last_message_at:
                raw["last_message_at"] = max(
                    float(raw.get("last_message_at") or 0),
                    _merged_last_message_at,
                )
                raw["updated_at"] = max(
                    float(raw.get("updated_at") or 0),
                    _merged_last_message_at,
                )
            # #2980: surface the visible continuation for a hidden pre-compression
            # snapshot so a mobile reload mid-compression can recover to it.
            continuation_sid = _pre_compression_continuation_session_id(s)
            if continuation_sid:
                raw["continuation_session_id"] = continuation_sid
            if cli_meta and _session_source_is_webui(cli_meta):
                raw = _reconcile_session_detail_source_flags(raw, cli_meta)
            elif cli_meta and _is_messaging_session_record(cli_meta):
                raw = _merge_cli_sidebar_metadata(raw, cli_meta)
                # ``message_count`` in /api/session is the display coordinate
                # space used for pagination and the header badge. Messaging
                # state.db metadata can include raw duplicate transport rows that
                # _merged_session_messages_for_display() intentionally dedupes;
                # keep the raw count available as ``actual_message_count`` but
                # do not let it make the frontend expect phantom messages.
                raw["message_count"] = _merged_message_count
            # Signal to the frontend that older messages were omitted. The
            # message window cursor already reflects visible-row pagination and
            # avoids false positives when raw hidden tool rows exceed msg_limit.
            _truncated = load_messages and msg_limit is not None and _messages_offset > 0
            raw["_messages_truncated"] = _truncated
            raw["_messages_offset"] = _messages_offset
            raw["_msg_limit_max"] = _MAX_MSG_LIMIT
            _t4 = _time.monotonic()
            if _diag: _diag.stage("t4_after_compact_and_merge")
            if effective_model:
                raw["model"] = effective_model
            if effective_provider:
                raw["model_provider"] = effective_provider
            # A subagent child (#5307) is view-only regardless of what a stale
            # sidecar stored: coerce the serialized flags so the browser never
            # treats an existing subagent sidecar as writable / CLI-classified.
            if (
                (str(raw.get("source_tag") or raw.get("raw_source") or raw.get("session_source") or "").strip().lower() == "subagent")
                or _is_subagent_child_session_id(sid)
            ):
                raw["is_cli_session"] = False
                raw["read_only"] = True
            redact = redact_session_data(raw)
            _t5 = _time.monotonic()
            if _diag: _diag.stage("t5_after_redact")
            _response_payload = {"session": redact}
            if _detail_cache_key is not None:
                _fresh_detail_cache_key = _session_detail_tail_cache_key(
                    s,
                    msg_limit=msg_limit,
                    expand_renderable=expand_renderable,
                )
                # The response was derived between two independent filesystem
                # snapshots. Store it only when no authoritative input changed
                # during reconciliation/redaction (TOCTOU stale-cache guard).
                if _fresh_detail_cache_key == _detail_cache_key:
                    _session_detail_tail_cache_set(_detail_cache_key, _response_payload)
            resp = j(handler, _response_payload)
            _t6 = _time.monotonic()
            if _diag: _diag.stage("t6_after_json_write")
            _total_ms = (_t6 - _t0) * 1000
            # Always log when slow (>2s) so we don't need HERMES_DEBUG_SLOW env var
            # to diagnose latency regressions. Opt-in env var still forces
            # logging on every request for development.
            if _debug_slow or _total_ms >= 2000:
                # perf(webui/session-load-latency) tier2c: route the [SLOW] line
                # through handler._safe_webui_print() rather than logger.warning().
                # The WebUI process starts the root logger without any handler, so
                # logger.warning() calls are silently dropped (the [SLOW] line
                # previously worked only on PIDs that happened to have a logger
                # handler set up by an earlier run; today the line is invisible).
                # _safe_webui_print writes to the systemd journal socket directly,
                # same as the per-request ms line — which is why THAT line keeps
                # working.
                handler._safe_webui_print(
                    "[SLOW] session_id=%s get_session=%.1fms model_resolve=%.1fms "
                    "compact=%.1fms redact=%.1fms json_write=%.1fms total=%.1fms" % (
                        sid,
                        (_t2-_t1)*1000, (_t3-_t2)*1000, (_t4-_t3)*1000,
                        (_t5-_t4)*1000, (_t6-_t5)*1000, _total_ms,
                    )
                )
            if _diag: _diag.finish()
            return resp
        except KeyError:
            # perf(webui/session-load-latency) tier2c-followup: fire
            # _diag.finish() in the exception branch too. Greptile flagged
            # this in PR review — finish() unregisters the pending watchdog
            # entry; without it the entry stays for the full 5s slow-request
            # timeout and emits a spurious "Slow WebUI request still
            # running" log. Idempotent — finish() no-ops if already called.
            if _diag: _diag.finish()
            # No WebUI sidecar. Delegate to the shared foreign-session
            # synthesizer so GET and POST have symmetric writeable/read-only
            # behaviour for CLI/TUI/Desktop sessions. The helper enforces the
            # #2782 deleted-WebUI-session 404 contract (via
            # _session_index_marks_was_webui) and the #4911 source ownership
            # gate (via _is_claimable_cli_source) so the two endpoints can't
            # drift on foreign-session semantics.
            cli_meta = _lookup_cli_session_metadata(sid)
            _session_profile = (cli_meta or {}).get("profile") or None
            if not _session_visible_to_active_profile(_session_profile, handler):
                if _session_profile:
                    # Valid CLI/foreign session owned by a KNOWN other profile:
                    # 409 so the client can offer to switch to it (#5419).
                    return j(handler, {
                        "error": "Session belongs to a different profile",
                        "code": "session_profile_mismatch",
                        "session_id": sid,
                        "profile": _session_profile,
                    }, status=409)
                # Missing session (cli_meta={} -> profile=None): keep the 404
                # self-heal path. _profiles_match coerces None->'default', so a
                # truly-missing session under a non-default active profile would
                # otherwise emit a useless 409 with profile=null and skip the
                # frontend self-heal + spin the SSE reconnect against a dead sid.
                return bad(handler, "Session not found", 404)
            synth, reason = _claim_or_synthesize_cli_session(sid, cli_meta=cli_meta or {})
            if reason == "was_webui":
                # Deleted WebUI session: 404 so the client self-heals
                # (clears stale /session/<id> URL and localStorage, #2782).
                return bad(handler, "Session not found", 404)
            if synth is None:
                # 'no_foreign_state' / 'invalid_sid' — nothing to render.
                return bad(handler, "Session not found", 404)
            # Build the legacy dict response from the synthesized Session so
            # the wire shape stays byte-equivalent to the previous inline
            # synthesis (the frontend has been reading these exact keys).
            msgs = list(synth.messages or [])
            sess = {
                "session_id": synth.session_id,
                "title": synth.title,
                "workspace": synth.workspace,
                "model": synth.model,
                "message_count": len(msgs),
                "created_at": synth.created_at,
                "updated_at": synth.updated_at,
                "last_message_at": (
                    (cli_meta or {}).get("last_message_at")
                    or (cli_meta or {}).get("updated_at", 0)
                    or ((msgs or [{}])[-1].get("timestamp", 0))
                ),
                "pinned": bool(getattr(synth, "pinned", False)),
                "archived": bool(getattr(synth, "archived", False)),
                "project_id": getattr(synth, "project_id", None),
                "profile": synth.profile,
                # Read is_cli_session from the synthesized Session, not a
                # hardcoded True: delegated subagent children (#5307) are
                # recovered read-only with is_cli_session=False so they don't
                # pass the frontend _isExternalSession poll-skip / active-refresh
                # gates (#3603). Every other synthesized foreign session keeps
                # is_cli_session=True so its source badge renders.
                "is_cli_session": bool(getattr(synth, "is_cli_session", False)),
                "source_tag": synth.source_tag,
                "raw_source": synth.raw_source,
                "session_source": synth.session_source,
                "source_label": synth.source_label,
                # Greptile #4911 follow-up: read read_only from the
                # synthesized Session, NOT from cli_meta directly.
                # The helper sets synth.read_only=True for BOTH
                # explicit read_only=True cli_meta AND source-refused
                # sessions (messaging / claude_code / external_agent).
                # cli_meta.get("read_only") is only populated for the
                # explicit case, so reading it from there causes the
                # frontend to render the composer for source-refused
                # sessions and the user only discovers the block at
                # POST time with a confusing 403.
                "read_only": bool(getattr(synth, "read_only", False)),
                "messages": msgs,
                "tool_calls": [],
            }
            attach_todo_state(sess, msgs)
            sess = _merge_cli_sidebar_metadata(sess, cli_meta)
            return j(handler, {"session": redact_session_data(sess)})

    if parsed.path == "/api/session/lineage/report":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "session_id required", 400)
        report = read_session_lineage_report(_active_state_db_path(), sid)
        if not report.get("found"):
            return bad(handler, "Session not found", 404)
        return j(handler, report)

    if parsed.path == "/api/session/recovery/audit":
        from api.sessions.recovery import audit_session_recovery
        return j(handler, audit_session_recovery(SESSION_DIR, state_db_path=_active_state_db_path()))

    if parsed.path == "/api/session/status":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        try:
            from api.sessions.operations import session_status
            _clear_stale_stream_state(get_session(sid, metadata_only=True))
            return j(handler, session_status(sid))
        except KeyError:
            return bad(handler, "Session not found", 404)

    if parsed.path == "/api/session/yolo":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        return j(handler, {"yolo_enabled": is_session_yolo_enabled(sid)})

    if parsed.path == "/api/session/usage":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        try:
            from api.sessions.operations import session_usage
            return j(handler, session_usage(sid))
        except KeyError:
            return bad(handler, "Session not found", 404)

    if parsed.path == "/api/background/status":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        from api.runs.background import get_results
        return j(handler, {"results": get_results(sid)})

    if parsed.path == "/api/sessions":
        diag = RequestDiagnostics.maybe_start("GET", parsed.path, logger=logger, print_fn=getattr(handler, '_safe_webui_print', None))
        try:
            from api import profiles as profiles_api

            diag.stage("load_settings")
            settings = load_settings()
            show_cli_sessions = bool(settings.get("show_cli_sessions"))
            show_claude_code_sessions = bool(settings.get("show_claude_code_sessions"))
            show_previous_messaging_sessions = bool(
                settings.get("show_previous_messaging_sessions")
            )
            show_cron_sessions = bool(settings.get("show_cron_sessions"))
            show_webhook_sessions = bool(settings.get("show_webhook_sessions"))
            agent_session_source_filter = settings.get("agent_session_source_filter")
            active_profile = profiles_api.get_active_profile_name()
            all_profiles = _all_profiles_enabled(parsed)
            include_archived = _query_flag(parsed, "include_archived")
            exclude_hidden = _query_flag(parsed, "exclude_hidden")
            archived_limit = _query_positive_int(parsed, "archived_limit", default=None, maximum=2000)
            archived_offset = _query_positive_int(parsed, "archived_offset", default=0, maximum=200000)
            sidebar_source = parse_qs(parsed.query).get("sidebar_source", [""])[0].strip().lower() or None
            if sidebar_source not in ("webui", "cli"):
                sidebar_source = None
            # /api/sessions is the default sidebar contract, so keep the route-owned
            # visible-row filter in the shared cache builder for both cache hits and misses.
            key = _session_list_cache_key(
                active_profile=active_profile,
                all_profiles=all_profiles,
                show_cli_sessions=show_cli_sessions,
                show_claude_code_sessions=show_claude_code_sessions,
                show_previous_messaging_sessions=show_previous_messaging_sessions,
                show_cron_sessions=show_cron_sessions,
                include_archived=include_archived,
                exclude_hidden=exclude_hidden,
                visible_only=True,
                show_webhook_sessions=show_webhook_sessions,
                source_filter=agent_session_source_filter,
                sidebar_source=sidebar_source,
                archived_limit=archived_limit,
                archived_offset=archived_offset,
            )
            # Keep the visible /api/sessions contract unchanged even though the
            # heavy lifting now lives in the cache builder: profile scoping via
            # `_profiles_match(s.get("profile"), active_profile)` still happens
            # before `_keep_latest_messaging_session_per_source(`.
            payload = _get_cached_session_list_payload(
                key=key,
                builder=lambda: _build_session_list_cache_payload(
                    active_profile=active_profile,
                    all_profiles=all_profiles,
                    show_cli_sessions=show_cli_sessions,
                    show_claude_code_sessions=show_claude_code_sessions,
                    show_previous_messaging_sessions=show_previous_messaging_sessions,
                    show_cron_sessions=show_cron_sessions,
                    include_archived=include_archived,
                    exclude_hidden=exclude_hidden,
                    visible_only=True,
                    show_webhook_sessions=show_webhook_sessions,
                    source_filter=agent_session_source_filter,
                    sidebar_source=sidebar_source,
                    archived_limit=archived_limit,
                    archived_offset=archived_offset,
                    diag=diag,
                ),
                diag=diag,
            )
            diag.stage("response_write")
            return j(handler, _session_list_payload_to_response(payload), pretty=False)
        finally:
            diag.finish()

    if parsed.path == "/api/projects":
        # ── Profile scoping (#1614) ────────────────────────────────────────
        # Default: filter to the active profile. ?all_profiles=1 returns the
        # aggregate list so settings/admin UIs can still see everything.
        from api import profiles as profiles_api

        active_profile = profiles_api.get_active_profile_name()
        all_projects = load_projects()
        isolated_profile_mode = _is_isolated_profile_mode()
        all_profiles = _all_profiles_enabled(parsed)
        if all_profiles:
            scoped = all_projects
            other_profile_count = 0
        else:
            scoped = [p for p in all_projects
                      if _profiles_match(p.get("profile"), active_profile)]
            other_profile_count = 0 if isolated_profile_mode else len(all_projects) - len(scoped)
        return j(handler, {
            "projects": scoped,
            "all_profiles": all_profiles,
            "active_profile": active_profile,
            "other_profile_count": other_profile_count,
        })

    if parsed.path == "/api/prompts":
        return j(handler, {"prompts": _load_saved_prompts()})

    if parsed.path == "/api/session/export":
        return _handle_session_export(handler, parsed)

    if parsed.path == "/api/workspaces":
        return j(
            handler,
            {
                "workspaces": load_workspaces(),
                "last": get_last_workspace(),
                "terminal_remote_backend": _terminal_remote_backend_enabled(),
            },
        )

    if parsed.path == "/api/workspaces/suggest":
        qs = parse_qs(parsed.query)
        prefix = qs.get("prefix", [""])[0]
        return j(
            handler,
            {
                "suggestions": list_workspace_suggestions(prefix),
                "prefix": prefix,
            },
        )

    if parsed.path == "/api/sessions/search":
        return _handle_sessions_search(handler, parsed)

    if parsed.path == "/api/list":
        return _handle_list_dir(handler, parsed)

    if parsed.path == "/api/escape/list":
        return _handle_escape_list_dir(handler, parsed)

    if parsed.path == "/api/git/status":
        return _handle_git_status(handler, parsed)

    if parsed.path == "/api/git/branches":
        return _handle_git_branches(handler, parsed)

    if parsed.path == "/api/git/diff":
        return _handle_git_diff(handler, parsed)

    if parsed.path == "/api/personalities":
        # Read personalities from config.yaml agent.personalities section
        # (matches hermes-agent CLI behavior, not filesystem SOUL.md approach)
        from api.config import reload_config as _reload_cfg

        _reload_cfg()  # pick up config.yaml changes without server restart
        from api.config import get_config as _get_cfg

        _cfg = _get_cfg()
        agent_cfg = _cfg.get("agent", {})
        raw_personalities = agent_cfg.get("personalities", {})
        personalities = []
        if isinstance(raw_personalities, dict):
            for name, value in raw_personalities.items():
                desc = ""
                if isinstance(value, dict):
                    desc = value.get("description", "")
                elif isinstance(value, str):
                    desc = value[:80] + ("..." if len(value) > 80 else "")
                personalities.append({"name": name, "description": desc})
        return j(handler, {"personalities": personalities})

    if parsed.path == "/api/git-info":
        qs = parse_qs(parsed.query)
        sid = qs.get("session_id", [""])[0]
        if not sid:
            return bad(handler, "session_id required")
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        from api.workspace_git import GitWorkspaceError, git_status

        try:
            status = git_status(Path(s.workspace))
        except GitWorkspaceError as e:
            return _git_bad(handler, e)
        totals = status.get("totals") or {}
        info = None if not status.get("is_git") else {
            "branch": status.get("branch"),
            "dirty": totals.get("changed", 0),
            "modified": (totals.get("staged", 0) or 0) + (totals.get("unstaged", 0) or 0),
            "untracked": totals.get("untracked", 0),
            "ahead": status.get("ahead", 0),
            "behind": status.get("behind", 0),
            "is_git": True,
        }
        return j(handler, {"git": info})

    if parsed.path == "/api/commands":
        from api.commands import list_commands
        return j(handler, {"commands": list_commands()})

    if parsed.path == "/api/commands/bundles":
        from api.commands import list_command_bundles
        return j(handler, {"bundles": list_command_bundles()})

    if parsed.path == "/api/commands/moa/resolve":
        from api.commands import resolve_moa_config
        try:
            return j(handler, resolve_moa_config())
        except RuntimeError as e:
            return bad(handler, str(e), 503)

    if parsed.path == "/api/updates/check":
        settings = load_settings()
        if not settings.get("check_for_updates", True):
            return j(handler, {"disabled": True})
        include_agent_updates = not bool(settings.get("ignore_agent_updates"))
        qs = parse_qs(parsed.query)
        # ?simulate=1 returns fake behind counts for UI testing (localhost only)
        if (
            qs.get("simulate", ["0"])[0] == "1"
            and handler.client_address[0] == "127.0.0.1"
        ):
            return j(
                handler,
                {
                    "webui": {
                        "name": "webui",
                        "behind": 3,
                        "current_sha": "abc1234",
                        "latest_sha": "def5678",
                        "branch": "master",
                        "repo_url": "https://github.com/nesquena/hermes-webui",
                        "compare_url": "https://github.com/nesquena/hermes-webui/compare/abc1234...def5678",
                    },
                    "agent": {
                        "name": "agent",
                        "behind": 1 if include_agent_updates else 0,
                        "ignored": not include_agent_updates,
                        "current_sha": "aaa0001",
                        "latest_sha": "bbb0002",
                        "branch": "master",
                        "repo_url": "https://github.com/NousResearch/hermes-agent",
                        "compare_url": "https://github.com/NousResearch/hermes-agent/compare/aaa0001...bbb0002",
                    },
                    "checked_at": 0,
                },
            )
        from api.updates import cached_update_status

        return j(handler, cached_update_status(include_agent=include_agent_updates))

    if parsed.path == "/api/chat/stream/status":
        stream_id = parse_qs(parsed.query).get("stream_id", [""])[0]
        if not _stream_id_visible_to_request_profile(handler, stream_id):
            return True
        active = runtime_stream_alive(stream_id)
        payload = {"active": active, "stream_id": stream_id, "replay_available": False}
        try:
            journal = find_run_summary(stream_id) if stream_id else None
        except Exception:
            journal = None
        if journal:
            payload["replay_available"] = True
            payload["journal"] = _run_journal_status_payload(journal, active=active)
        return j(handler, payload)

    if parsed.path == "/api/chat/cancel":
        stream_id = parse_qs(parsed.query).get("stream_id", [""])[0]
        if not stream_id:
            return bad(handler, "stream_id required")
        if not _stream_id_visible_to_request_profile(handler, stream_id):
            return True
        from api.runs.adapter import LegacyJournalRuntimeAdapter, runtime_adapter_enabled

        if runtime_adapter_enabled():
            adapter = LegacyJournalRuntimeAdapter(cancel_delegate=cancel_stream)
            cancelled = adapter.cancel_run(stream_id).accepted
        else:
            cancelled = cancel_stream(stream_id)
        return j(handler, {"ok": True, "cancelled": cancelled, "stream_id": stream_id})

    if parsed.path == "/api/chat/stream":
        return _handle_sse_stream(handler, parsed)

    if parsed.path == "/api/terminal/output":
        return _handle_terminal_output(handler, parsed)

    if parsed.path == '/api/sessions/gateway/stream':
        return _handle_gateway_sse_stream(handler, parsed)

    if parsed.path == '/api/sessions/events':
        return _handle_session_events_stream(handler)

    session_events_session_id = _session_events_path_session_id(parsed.path)
    if session_events_session_id is not None:
        return _handle_session_sse_stream_for_session(handler, parsed, session_events_session_id)

    if parsed.path == "/api/media":
        return _handle_media(handler, parsed)

    if parsed.path == "/api/file/raw":
        return _handle_file_raw(handler, parsed)

    if parsed.path == "/api/escape/file/raw":
        return _handle_escape_file_raw(handler, parsed)

    if parsed.path == "/api/folder/download":
        return _handle_folder_download(handler, parsed)

    if parsed.path == "/api/file":
        return _handle_file_read(handler, parsed)

    if parsed.path == "/api/escape/file/read":
        return _handle_escape_file_read(handler, parsed)

    if parsed.path == "/api/approval/pending":
        return _handle_approval_pending(handler, parsed)

    if parsed.path == "/api/approval/stream":
        return _handle_approval_sse_stream(handler, parsed)

    if parsed.path == "/api/approval/inject_test":
        # Loopback-only: used by automated tests; blocked from any remote client
        if handler.client_address[0] != "127.0.0.1":
            return j(handler, {"error": "not found"}, status=404)
        return _handle_approval_inject(handler, parsed)

    if parsed.path == "/api/clarify/pending":
        return _handle_clarify_pending(handler, parsed)

    if parsed.path == "/api/clarify/stream":
        return _handle_clarify_sse_stream(handler, parsed)

    if parsed.path == "/api/session/stream":
        return _handle_session_sse_stream(handler, parsed)

    if parsed.path == "/api/clarify/inject_test":
        # Loopback-only: used by automated tests; blocked from any remote client
        if handler.client_address[0] != "127.0.0.1":
            return j(handler, {"error": "not found"}, status=404)
        return _handle_clarify_inject(handler, parsed)

    if parsed.path == "/api/onboarding/oauth/poll":
        qs = parse_qs(parsed.query)
        flow_id = qs.get("flow_id", [""])[0]
        try:
            return j(
                handler,
                poll_onboarding_oauth_flow(flow_id),
                extra_headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            return bad(handler, str(e))
        except KeyError as e:
            return bad(handler, str(e), 404)

    # ── Cron API (GET) ──
    # Cron reads are active-profile-scoped by default. The list route now
    # aggregates per visible profile home so the UI can surface hidden-row
    # counts and, when opted in, read-only foreign rows.
    if parsed.path == "/api/crons":
        # #4768: in split-container / minimal Docker deployments the WebUI image may
        # not ship the agent's `cron` package on its import path. Degrade gracefully
        # (empty list + cron_unavailable flag) instead of 500ing the whole Task tab.
        # Only treat a genuinely-absent cron package as "unavailable"; a
        # ModuleNotFoundError whose missing module is an internal dependency of an
        # existing cron/jobs.py is a real bug and must still surface.
        _ensure_agent_cron_import_path()
        active_profile = _get_active_profile_name() or "default"
        try:
            active_jobs, other_jobs = _cron_jobs_cross_profile(active_profile)
        except ModuleNotFoundError as exc:
            if exc.name in ("cron", "cron.jobs"):
                return j(handler, {"jobs": [], "cron_unavailable": True})
            raise
        all_profiles = _all_profiles_enabled(parsed)
        jobs = active_jobs + other_jobs if all_profiles else active_jobs
        hidden_other_count = 0 if all_profiles else len(other_jobs)
        return j(handler, {
            "jobs": jobs,
            "all_profiles": all_profiles,
            "active_profile": active_profile,
            "other_profile_count": hidden_other_count,
        })

    if parsed.path == "/api/crons/output":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_output(handler, parsed)

    if parsed.path == "/api/crons/history":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_history(handler, parsed)

    if parsed.path == "/api/crons/run":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_run_detail(handler, parsed)

    if parsed.path == "/api/crons/recent":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_recent(handler, parsed)

    if parsed.path == "/api/crons/status":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            return _handle_cron_status(handler, parsed)

    if parsed.path == "/api/crons/delivery-options":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_delivery_options(handler)

    # ── Skills API (GET) ──
    if parsed.path == "/api/skills":
        qs = parse_qs(parsed.query)
        category = qs.get("category", [None])[0]
        data = _skills_list_from_dir(_active_skills_dir(), category=category)
        return j(handler, {"skills": data.get("skills", [])})

    if parsed.path == "/api/skills/usage":
        from api.skill_usage import read_skill_usage
        raw = read_skill_usage(_active_skills_dir())
        # Pass through agent's format as-is; defensive coercion for fields
        usage = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not isinstance(v, dict):
                    usage[k] = {"use_count": 0, "view_count": 0, "patch_count": 0}
                    continue
                usage[k] = {
                    "use_count": (int(v["use_count"]) if v.get("use_count") is not None else 0),
                    "view_count": (int(v["view_count"]) if v.get("view_count") is not None else 0),
                    "patch_count": (int(v["patch_count"]) if v.get("patch_count") is not None else 0),
                }
                # Preserve agent's metadata (timestamps, state, etc.)
                for meta_key in v:
                    if meta_key not in usage[k]:
                        usage[k][meta_key] = v[meta_key]
        skills_data = _skills_list_from_dir(_active_skills_dir()).get("skills", [])
        skill_names = sorted({s["name"] for s in skills_data})
        total = sum(
            e.get("use_count", 0) + e.get("view_count", 0) + e.get("patch_count", 0)
            for e in usage.values()
        )
        unique = sum(
            1 for e in usage.values()
            if e.get("use_count", 0) > 0 or e.get("view_count", 0) > 0 or e.get("patch_count", 0) > 0
        )
        return j(handler, {
            "usage": usage,
            "skill_names": skill_names,
            "total_invocations": total,
            "unique_skills_used": unique,
        })

    if parsed.path == "/api/skills/content":
        qs = parse_qs(parsed.query)
        name = qs.get("name", [""])[0]
        if not name:
            return j(handler, {"error": "name required"}, status=400)
        file_path = qs.get("file", [""])[0]
        if file_path:
            # Serve a linked file from the skill directory
            import re as _re

            if _re.search(r"[*?\[\]]", name):
                return bad(handler, "Invalid skill name", 400)
            skills_dir = _active_skills_dir()
            skill_dir, _skill_md = _find_skill_in_dirs(
                name, _active_skill_search_dirs(skills_dir)
            )
            if not skill_dir:
                return bad(handler, "Skill not found", 404)
            target = (skill_dir / file_path).resolve()
            try:
                target.relative_to(skill_dir.resolve())
            except ValueError:
                return bad(handler, "Invalid file path", 400)
            if not target.exists() or not target.is_file():
                return bad(handler, "File not found", 404)
            return j(
                handler,
                {"content": target.read_text(encoding="utf-8"), "path": file_path},
            )
        data = _skill_view_from_active_dir(name)
        if not isinstance(data.get("linked_files"), dict):
            data["linked_files"] = {}
        return j(handler, data)

    # ── Memory API (GET) ──
    if parsed.path == "/api/memory":
        return _handle_memory_read(handler, parsed)

    # ── Profile API (GET) ──
    if parsed.path == "/api/profiles":
        from api import profiles as profiles_api
        diag = RequestDiagnostics.maybe_start("GET", parsed.path, logger=logger, print_fn=getattr(handler, '_safe_webui_print', None))
        try:
            diag.stage("list_profiles_api") if diag else None
            profiles_payload = profiles_api.list_profiles_api()
            diag.stage("active_profile_lookup") if diag else None
            active = profiles_api.get_active_profile_name()
            diag.stage("isolated_mode_check") if diag else None
            return j(
                handler,
                {
                    "profiles": profiles_payload,
                    "active": active,
                    "single_profile_mode": _is_isolated_profile_mode(),
                },
            )
        finally:
            if diag:
                diag.finish()

    if parsed.path == "/api/profile/active":
        from api import profiles as profiles_api

        active_profile_name = profiles_api.get_active_profile_name()
        # Resolve the ACTIVE PROFILE's configured workspace so a cold boot with a
        # profile cookie shows the right composer workspace chip on a blank
        # new-chat page (#5169). Use get_profile_default_workspace() (NOT
        # get_last_workspace) so a named profile without its own last_workspace.txt
        # resolves to its config.yaml workspace/terminal.cwd rather than leaking the
        # GLOBAL last-workspace file (the #5169 regression Codex flagged). It is
        # profile-scoped via the per-request hermes_profile cookie set in server.py.
        # Fail open: a resolution error must never 500 this boot-critical endpoint.
        try:
            _profile_default_workspace = get_profile_default_workspace()
        except Exception:
            logger.debug("Failed to resolve profile default workspace for /api/profile/active", exc_info=True)
            _profile_default_workspace = None
        return j(
            handler,
            {
                "name": active_profile_name,
                "path": str(profiles_api.get_active_hermes_home()),
                "is_default": profiles_api._is_root_profile(active_profile_name),
                "default_workspace": _profile_default_workspace,
            },
        )

    # ── Gateway Status (GET) ──
    if parsed.path == "/api/gateway/status":
        return j(handler, _gateway_status_payload())

    # ── MCP Servers (GET) ──
    if parsed.path == "/api/mcp/servers":
        return _handle_mcp_servers_list(handler)

    # ── MCP Tools (GET) ──
    if parsed.path == "/api/mcp/tools":
        return _handle_mcp_tools_list(handler)

    if parsed.path == "/api/notes/sources":
        return _handle_notes_sources_list(handler)
    if parsed.path == "/api/notes/search":
        return _handle_notes_search(handler, parsed)
    if parsed.path == "/api/notes/item":
        return _handle_notes_item(handler, parsed)

    # ── Checkpoints / Rollback (GET) ──
    if parsed.path == "/api/rollback/list":
        qs = parse_qs(parsed.query)
        workspace = qs.get("workspace", [""])[0]
        if not workspace:
            return bad(handler, "workspace query parameter is required")
        try:
            from api.rollback import list_checkpoints
            return j(handler, list_checkpoints(workspace))
        except ValueError as e:
            return bad(handler, str(e))
        except Exception as e:
            logger.exception("rollback/list failed")
            return bad(handler, str(e), status=500)

    if parsed.path == "/api/rollback/diff":
        qs = parse_qs(parsed.query)
        workspace = qs.get("workspace", [""])[0]
        checkpoint = qs.get("checkpoint", [""])[0]
        if not workspace or not checkpoint:
            return bad(handler, "workspace and checkpoint query parameters are required")
        try:
            from api.rollback import get_checkpoint_diff
            return j(handler, get_checkpoint_diff(workspace, checkpoint))
        except ValueError as e:
            return bad(handler, str(e))
        except Exception as e:
            logger.exception("rollback/diff failed")
            return bad(handler, str(e), status=500)

    # ── Plugin shared assets (e.g. /plugins/plugin.css) ──
    # Restricted to shared plugin assets only — no cross-plugin file access.
    if parsed.path.startswith("/plugins/"):
        from api.plugins import _get_plugin_base
        plugin_base = _get_plugin_base()
        rel = parsed.path[len("/plugins/"):]
        allowed = {"plugin.css"}
        if rel not in allowed:
            return False  # 404
        safe = (plugin_base / rel).resolve()
        try:
            safe.relative_to(plugin_base.resolve())
        except ValueError:
            return False  # path traversal — 404
        if safe.is_file():
            import os as _os
            data = safe.read_bytes()
            ext = _os.path.splitext(rel.lower())[1]
            ct = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")
            handler.send_response(200)
            handler.send_header("Content-Type", ct)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return True

    # ── Plugin static assets ──
    if parsed.path.startswith("/dashboard-plugins/"):
        parts = parsed.path.split("/", 3)
        if len(parts) >= 3:
            plugin_name = parts[2]
            rel_path = parts[3] if len(parts) > 3 else ""
            # Server-side enable-gate: a plugin disabled in Settings must have its
            # entire URL surface shut off, not merely hidden in the UI.
            if not _dashboard_plugin_enabled(plugin_name):
                return False  # 404 — disabled plugins serve nothing
            from api.plugins import serve_plugin_static
            result = serve_plugin_static(plugin_name, rel_path)
            if result:
                data, content_type = result
                handler.send_response(200)
                handler.send_header("Content-Type", content_type)
                # Defense-in-depth: plugin-controlled assets are served from the
                # WebUI's own origin. Sandbox them (null origin) so a plugin's
                # .html/.svg can't run privileged same-origin script if navigated
                # to directly (the in-panel iframe sandbox doesn't cover direct
                # navigation). nosniff prevents content-type confusion.
                handler.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups")
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
                return True

    # ── Plugin pages (HTML shell) ──
    from api.plugins import PLUGIN_MANIFESTS, _PLUGIN_STATIC_ROOTS
    for name, manifest in PLUGIN_MANIFESTS.items():
        tab = manifest.get("tab", {})
        tab_path = tab.get("path", f"/{name}")
        if parsed.path == tab_path:
            # Server-side enable-gate (opt-in): a disabled plugin's page 404s.
            if not _dashboard_plugin_enabled(name):
                return False
            dashboard_dir = _PLUGIN_STATIC_ROOTS.get(name)
            if dashboard_dir:
                # 1) dashboard/dist/index.html (full SPA build)
                index_html = dashboard_dir / "dist" / "index.html"
                if index_html.is_file():
                    data = index_html.read_bytes()
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/html; charset=utf-8")
                    handler.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups")
                    handler.send_header("Content-Length", str(len(data)))
                    handler.end_headers()
                    handler.wfile.write(data)
                    return True
                # 2) static/index.html in plugin root (content page for IIFE loader)
                plugin_root = dashboard_dir.parent
                static_html = plugin_root / "static" / "index.html"
                if static_html.is_file():
                    data = static_html.read_bytes()
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/html; charset=utf-8")
                    handler.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups")
                    handler.send_header("Content-Length", str(len(data)))
                    handler.end_headers()
                    handler.wfile.write(data)
                    return True
                # 3) Fallback: generate shell that loads the IIFE bundle
                index_js = dashboard_dir / "dist" / "index.js"
                if index_js.is_file():
                    import html
                    label = html.escape(manifest.get("label") or name)
                    css = html.escape(manifest.get("css", ""))
                    name_escaped = html.escape(name)
                    css_tag = f'<link rel="stylesheet" href="/dashboard-plugins/{name_escaped}/{css}">' if css else ""
                    html_content = (
                        f"<!doctype html>\n"
                        f"<html lang=\"en\">\n"
                        f"<head>\n"
                        f"  <meta charset=\"utf-8\">\n"
                        f"  <title>{label}</title>\n"
                        f"  {css_tag}\n"
                        f"</head>\n"
                        f"<body>\n"
                        f'  <div id="pluginPageContainer"></div>\n'
                        f'  <script src="/dashboard-plugins/{name_escaped}/dist/index.js"></script>\n'
                        f"</body>\n"
                        f"</html>\n"
                    ).encode("utf-8")
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/html; charset=utf-8")
                    handler.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups")
                    handler.send_header("Content-Length", str(len(html_content)))
                    handler.end_headers()
                    handler.wfile.write(html_content)
                    return True

    return False  # 404


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
    """Handle all POST routes. Returns True if handled, False for 404."""
    diag = RequestDiagnostics.maybe_start("POST", parsed.path, logger=logger, print_fn=getattr(handler, '_safe_webui_print', None))
    if parsed.path == "/api/csp-report":
        if diag:
            diag.stage("csp_report")
        try:
            return _handle_csp_report(handler)
        finally:
            if diag:
                diag.finish()
    # T1 deprecation alias for the legacy ack endpoint that the pre-rename
    # WebUI used to POST to after handling ``process_complete``. The new
    # canonical SSE event is ``bg_task_complete`` and the new ack endpoint
    # will be ``/api/bg-task-complete-ack`` (introduced by PR (b), the WebUI
    # half of the split). Until PR (b) lands we keep the old path responding
    # with HTTP 410 Gone + ``X-Replaced-By`` so any stale tab posting under
    # the old name fails loudly with a discoverable hint. The handler runs
    # BEFORE the CSRF gate on purpose: an old tab will not carry a CSRF token
    # for the deprecated path, and surfacing 410 (not 403) is the correct
    # contract here.
    if parsed.path == "/api/process-complete-ack":
        if diag:
            diag.stage("process_complete_ack_deprecated")
        try:
            j(
                handler,
                {
                    "error": (
                        "gone: /api/process-complete-ack was replaced by "
                        "/api/bg-task-complete-ack as part of the "
                        "process_complete -> bg_task_complete event rename"
                    ),
                    "replaced_by": "/api/bg-task-complete-ack",
                },
                status=410,
                extra_headers={"X-Replaced-By": "/api/bg-task-complete-ack"},
            )
            return True
        finally:
            if diag:
                diag.finish()
    # CSRF: reject cross-origin or tokenless authenticated browser requests.
    # /api/auth/login has no authenticated session token yet, and /api/csp-report
    # is intentionally unauthenticated for browser-generated violation reports.
    if diag:
        diag.stage("csrf")
    if not _csrf_exempt_path(parsed.path) and not _check_csrf(handler):
        try:
            return j(handler, {"error": _csrf_rejection_error(handler)}, status=403)
        finally:
            if diag:
                diag.finish()
    proxy_result = _handle_extension_sidecar_proxy(
        handler,
        parsed,
        "POST",
        read_request_body=True,
    )
    if proxy_result is not False:
        if diag:
            diag.finish()
        return proxy_result

    if parsed.path == "/api/shutdown":
        return _handle_shutdown(handler)

    if parsed.path == "/api/health/restart":
        return _handle_health_restart(handler)

    if parsed.path == "/api/upload":
        return handle_upload(handler)
    if parsed.path == "/api/upload/extract":
        return handle_upload_extract(handler)
    if parsed.path == "/api/workspace/upload":
        return handle_workspace_upload(handler)

    if parsed.path == "/api/transcribe":
        return handle_transcribe(handler)

    if parsed.path == "/api/tts":
        return _handle_tts(handler, parsed)

    if parsed.path == "/api/client-events/log":
        if diag:
            diag.stage("read_client_event_body")
        return _handle_client_event_log(handler, _read_client_event_payload(handler))

    if diag:
        diag.stage("read_body")
    try:
        body = read_body(handler)
    except ValueError as exc:
        if diag:
            diag.finish()
        status = 413 if "too large" in str(exc).lower() else 400
        return bad(handler, str(exc), status=status)
    except Exception:
        if diag:
            diag.finish()
        raise
    if not _guard_request_session_visibility(handler, parsed, body=body, method="POST"):
        if diag:
            diag.finish()
        return True

    if parsed.path == "/api/escape/authorize":
        return _handle_escape_authorize(handler, parsed, body)

    if parsed.path == "/api/updates/check":
        settings = load_settings()
        if not settings.get("check_for_updates", True):
            force = bool(body.get("force", False)) if isinstance(body, dict) else False
            if force:
                # Manual force-check bypasses auto-check toggle (#6082)
                pass
            else:
                return j(handler, {"disabled": True})
        include_agent_updates = not bool(settings.get("ignore_agent_updates"))
        force = bool(body.get("force", False))
        # Allow the client to pass the channel explicitly in the POST body. This
        # avoids a race on channel switch: the Settings dropdown re-checks
        # immediately, but its autosave PUT (debounced) may not have landed
        # server-side yet, so reading the saved setting here could answer for the
        # OLD channel. An explicit body channel (validated against the enum) wins;
        # otherwise fall back to the saved setting. (Fable UX gate.)
        channel = body.get("channel") if isinstance(body, dict) else None
        if channel not in ("stable", "experimental"):
            channel = settings.get("update_channel")
        from api.updates import check_for_updates

        return j(handler, check_for_updates(force=force, include_agent=include_agent_updates, channel=channel))

    if parsed.path == "/api/extensions/toggle":
        from api.extensions import ExtensionToggleError, set_extension_user_enabled

        try:
            return j(
                handler,
                set_extension_user_enabled(body.get("id"), body.get("enabled")),
            )
        except ExtensionToggleError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension toggle failed")
            return bad(handler, "Failed to update extension state", status=500)

    if parsed.path == "/api/extensions/sidecar-proxy-consent":
        from api.extensions import (
            ExtensionSidecarProxyError,
            set_extension_sidecar_proxy_consent,
        )

        try:
            return j(
                handler,
                set_extension_sidecar_proxy_consent(
                    body.get("id"),
                    body.get("approved"),
                ),
            )
        except ExtensionSidecarProxyError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension sidecar proxy consent update failed")
            return bad(handler, "Failed to update extension state", status=500)

    if parsed.path == "/api/extensions/install":
        from api.extensions import ExtensionInstallError, install_extension

        try:
            return j(
                handler,
                install_extension(body.get("id"), body.get("download_url"), body.get("sha256")),
            )
        except ExtensionInstallError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension install failed")
            return bad(handler, "Failed to install extension", status=500)

    if parsed.path == "/api/extensions/uninstall":
        from api.extensions import ExtensionInstallError, uninstall_extension

        try:
            return j(
                handler,
                uninstall_extension(body.get("id")),
            )
        except ExtensionInstallError as exc:
            return bad(handler, str(exc), status=exc.status)
        except Exception:
            logger.exception("extension uninstall failed")
            return bad(handler, "Failed to uninstall extension", status=500)

    if parsed.path == "/api/session/recovery/repair-safe":
        from api.sessions.recovery import repair_safe_session_recovery
        result = repair_safe_session_recovery(SESSION_DIR, state_db_path=_active_state_db_path())
        return j(handler, result, status=200 if result.get("clean") else 409)

    if parsed.path.startswith("/api/kanban/"):
        from api.kanban_bridge import handle_kanban_post

        result = handle_kanban_post(handler, parsed, body)
        if result is False:
            return _kanban_unknown_endpoint(handler, parsed, "POST")
        return True
    if parsed.path == "/api/dashboard/config":
        from api import dashboard_probe

        try:
            j(handler, dashboard_probe.save_dashboard_config(body))
        except ValueError as exc:
            bad(handler, str(exc), status=400)
        except Exception as exc:
            logger.exception("dashboard config save failed")
            bad(handler, str(exc), status=500)
        return True

    if parsed.path == "/api/prompts":
        text = str(body.get("text") or "").strip()
        label = str(body.get("label") or "").strip()
        if not text:
            return bad(handler, "text is required")
        if len(text) > 8000:
            return bad(handler, "text too long (max 8000 chars)")
        prompts = _load_saved_prompts()
        if len(prompts) >= 200:
            return bad(handler, "saved prompts limit reached (max 200)")
        new_prompt = {"id": uuid.uuid4().hex[:12], "label": label or text[:60], "text": text, "created_at": time.time()}
        prompts.append(new_prompt)
        _save_saved_prompts(prompts)
        return j(handler, {"ok": True, "prompt": new_prompt})

    if parsed.path == "/api/share/create":
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return bad(handler, "session_id is required", 400)
        try:
            snapshot_session, stored_session, cli_meta = _resolve_share_session_pair(sid, handler)
        except KeyError:
            return bad(handler, "Session not found", 404)
        # A pre-resolved WebUI session is never a missing-record seed: delete
        # may have won after resolution but before this owner is acquired. Only
        # a genuinely external session may materialize its first local sidecar.
        share_metadata_seed = None
        if stored_session is None:
            share_metadata_seed = _build_share_metadata_sidecar(
                sid,
                snapshot_session,
                cli_meta=cli_meta,
            )
        share_meta = None
        had_existing_share = False
        try:
            with edit_session(
                sid,
                session=share_metadata_seed,
                touch_updated_at=False,
            ) as persisted_session:
                # Keep transcript preparation outside the owner, but resolve
                # the token and commit the public file under the same owner as
                # its session metadata. Create and revoke can no longer pass
                # each other between share-file I/O and sidecar persistence.
                share_snapshot = copy.copy(snapshot_session)
                share_snapshot.share_token = persisted_session.share_token
                had_existing_share = bool(
                    str(persisted_session.share_token or "").strip()
                )
                share_meta = create_or_refresh_share(share_snapshot)
                persisted_session.share_token = share_meta["share_token"]
                persisted_session.share_created_at = share_meta["share_created_at"]
        except KeyError:
            return bad(handler, "Session not found", 404)
        except ValueError as exc:
            return bad(handler, str(exc), 400)
        except Exception:
            logger.exception("share metadata persistence failed for session %s", sid)
            if share_meta is not None and not had_existing_share:
                # The public snapshot was committed first so its sanitized I/O
                # completed under the owner before its matching sidecar save.
                # If that save fails, fail closed instead of leaving an
                # untracked public conversation.
                compensation_session = copy.copy(snapshot_session)
                compensation_session.share_token = share_meta["share_token"]
                try:
                    revoke_share(compensation_session)
                except Exception:
                    logger.exception(
                        "failed to compensate unpersisted share for session %s",
                        sid,
                    )
            return bad(
                handler,
                "Failed to persist session metadata for shared conversation",
                500,
            )
        _publish_session_list_changed(
            "session_share_create",
            profile=getattr(persisted_session, "profile", None),
            session_id=sid,
        )
        response_session = copy.copy(persisted_session)
        response_session.messages = list(getattr(snapshot_session, "messages", None) or [])
        return j(
            handler,
            {
                "ok": True,
                "share": {
                    "token": share_meta["share_token"],
                    "url": f"/share/{share_meta['share_token']}",
                    "title": share_meta["share_title"],
                    "message_count": share_meta["share_message_count"],
                    "created_at": share_meta["share_created_at"],
                    "updated_at": share_meta["share_updated_at"],
                },
                "session": response_session.compact() | {"messages": response_session.messages},
            },
        )

    if parsed.path == "/api/share/revoke":
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return bad(handler, "session_id is required", 400)
        try:
            snapshot_session, stored_session, cli_meta = _resolve_share_session_pair(sid, handler)
        except KeyError:
            return bad(handler, "Session not found", 404)
        share_metadata_seed = None
        if stored_session is None:
            token = str(getattr(snapshot_session, "share_token", "") or "").strip()
            if not token:
                return bad(handler, "Session not found", 404)
            share_metadata_seed = _build_share_metadata_sidecar(
                sid,
                snapshot_session,
                cli_meta=cli_meta,
            )
            share_metadata_seed.share_token = token
            share_metadata_seed.share_created_at = getattr(
                snapshot_session,
                "share_created_at",
                None,
            )
        share_revocation_completed = False
        try:
            with edit_session(
                sid,
                session=share_metadata_seed,
                touch_updated_at=False,
            ) as target_session:
                revoke_share(target_session)
                share_revocation_completed = True
                target_session.share_token = None
                target_session.share_created_at = None
        except KeyError:
            return bad(handler, "Session not found", 404)
        except Exception:
            if not share_revocation_completed:
                logger.exception("share revocation failed for session %s", sid)
                return bad(handler, "Failed to revoke shared conversation", 500)
            # The public file was revoked atomically before __exit__ attempted
            # the sidecar save. Report that partial state truthfully; a retry
            # can clear the remaining local token without republishing it.
            logger.exception(
                "revoked share metadata cleanup failed for session %s",
                sid,
            )
            return bad(
                handler,
                "Share was revoked, but session metadata could not be updated",
                500,
            )
        _publish_session_list_changed(
            "session_share_revoke",
            profile=getattr(target_session, "profile", None),
            session_id=sid,
        )
        response_session = copy.copy(target_session)
        response_session.messages = list(getattr(snapshot_session, "messages", None) or [])
        return j(
            handler,
            {
                "ok": True,
                "session": response_session.compact() | {"messages": response_session.messages},
            },
        )

    if parsed.path == "/api/session/new":
        try:
            workspace = str(resolve_trusted_workspace(body.get("workspace"))) if body.get("workspace") else None
        except (TypeError, ValueError) as e:
            return bad(handler, str(e))
        worktree_info = None
        worktree_skipped = None
        # Three-value worktree model (#6022): an explicit body value always
        # wins; an ABSENT key falls back to the agent's config-level
        # ``worktree:`` default so WebUI sessions and CLI sessions agree on
        # isolation for the same repo.  Clients that must never create a
        # worktree (e.g. the boot-time auto-bind) send ``worktree: false``
        # explicitly.
        raw_worktree = body.get("worktree")
        # Presence-based, not truthiness-based: a client that sends the key at
        # all (even ``worktree: null``) has spoken explicitly and never falls
        # through to the config default.  ``null`` parses as non-true below,
        # i.e. an explicit opt-out — only a genuinely ABSENT key inherits.
        worktree_explicit = "worktree" in body
        if worktree_explicit:
            worktree_requested = (
                raw_worktree is True
                or str(raw_worktree).strip().lower() in {"1", "true", "yes", "on"}
            )
        else:
            worktree_requested = _worktree_default_from_config(body.get("profile") or None)
        if worktree_requested:
            try:
                from api.worktrees import create_worktree_for_workspace
                base_workspace = workspace
                if not base_workspace:
                    base_workspace = str(resolve_trusted_workspace(get_last_workspace()))
                worktree_info = create_worktree_for_workspace(base_workspace)
                workspace = worktree_info["path"]
            except (TypeError, ValueError) as e:
                # Explicit requests keep the hard failure.  A config-default
                # request on a non-git workspace (create_worktree_for_workspace
                # raises ValueError) degrades to a plain session instead —
                # otherwise `worktree: true` in config.yaml would 400 every
                # session in every non-git directory.
                if worktree_explicit:
                    return bad(handler, str(e), status=400)
                worktree_info = None
                worktree_skipped = str(e)
            except Exception as e:
                logger.exception("failed to create worktree-backed session")
                return bad(handler, f"Failed to create worktree: {e}", status=500)
        model, model_provider = _session_model_state_from_request(
            body.get("model"),
            body.get("model_provider"),
        )
        try:
            enabled_toolsets = _validate_session_toolsets_shape(body.get("enabled_toolsets"))
        except ValueError as e:
            return bad(handler, str(e), status=400)
        # Use the profile sent by the client tab (if any) so that two tabs on
        # different profiles never clobber each other via the process-level global.
        # ── Memory lifecycle: commit the previous session before starting a new one ──
        prev_session_id = body.get("prev_session_id")
        if prev_session_id:
            if not _session_id_visible_to_request_profile(
                handler, prev_session_id, emit_error=False
            ):
                # Cross-profile hand-off after a profile switch: skip memory
                # commit for the previous profile's session, but still create
                # the new session (#5420).
                prev_session_id = None
            if prev_session_id:
                # Fire-and-forget: commit_memory_session() can take 1-5+ seconds
                # (extraction call to the memory provider), and blocking the
                # response here made "+ New Chat" feel slow/unresponsive.
                # commit_session_memory() already serialises overlapping commits
                # for a session via its own in-flight guard, so running it off
                # the request thread is safe.
                def _commit_prev_session_memory(_sid=prev_session_id):
                    try:
                        from api.sessions.lifecycle import commit_session_memory
                        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
                        prev_agent = None
                        with SESSION_AGENT_CACHE_LOCK:
                            _cached = SESSION_AGENT_CACHE.get(_sid)
                            if _cached:
                                prev_agent = _cached[0]
                        commit_session_memory(_sid, agent=prev_agent)
                    except Exception:
                        logger.warning(
                            "Lifecycle commit for prev_session %s failed",
                            _sid,
                            exc_info=True,
                        )
                    finally:
                        # Self-unregister so the background-commit registry does
                        # not leak completed threads; drain only tracks live ones.
                        try:
                            from api.sessions.lifecycle import _unregister_background_commit_thread
                            _unregister_background_commit_thread(threading.current_thread())
                        except Exception:
                            pass

                t = threading.Thread(
                    target=_commit_prev_session_memory,
                    daemon=True,
                    name=f"commit-memory-{prev_session_id}",
                )
                from api.sessions.lifecycle import _register_background_commit_thread
                # Refused only if shutdown draining has already begun; in that
                # window the inline drain commits the pending generation instead,
                # so skipping the worker start is safe (avoids a late daemon
                # thread the drain snapshot already missed).
                if _register_background_commit_thread(t):
                    t.start()
        s = new_session(
            workspace=workspace,
            model=model,
            model_provider=model_provider,
            profile=body.get("profile") or None,
            project_id=body.get("project_id") or None,
            worktree_info=worktree_info,
            enabled_toolsets=enabled_toolsets,
        )
        if worktree_info:
            publish_session_list_changed(
                "session_new",
                profile=getattr(s, "profile", None),
                session_id=getattr(s, "session_id", None),
            )
        payload = {"session": s.compact() | {"messages": s.messages}}
        if worktree_skipped:
            # Config-default worktree was skipped (non-git workspace); tell the
            # client the session is plain so the UI doesn't assume isolation.
            payload["worktree_skipped"] = worktree_skipped
        return j(handler, payload)

    if parsed.path == "/api/session/compression-recovery/start":
        return _handle_session_compression_recovery_start(handler, body)

    if parsed.path == "/api/session/duplicate":
        try:
            sid = body.get("session_id")
            if not sid:
                return bad(handler, "session_id is required")
            if _session_is_subagent_view_only(sid):
                return bad(handler, "Subagent sessions are view-only and cannot be duplicated from WebUI", 400)

            session = Session.load(sid)
            if not session:
                # 404, not 400 — missing resource, not a malformed request.
                return bad(handler, "Session not found", status=404)

            # Deep-copy mutable lists so the duplicate is *actually* independent.
            # `Session.__init__` does `self.messages = messages or []` — plain
            # assignment, no copy. Without deepcopy, both sessions share the same
            # list object in memory; appending to one mutates the other.
            # Items inside `messages` are dicts with mutable values (tool_calls,
            # content arrays), so a shallow `list(...)` is not enough.
            copied_session = Session(
                session_id=uuid.uuid4().hex[:12],
                # Defensive: legacy sessions may have title=None on disk; fall back to 'Untitled'
                # so `+ " (copy)"` doesn't TypeError.
                title=(session.title or "Untitled") + " (copy)",
                workspace=session.workspace,
                model=session.model,
                model_provider=session.model_provider,
                messages=copy.deepcopy(session.messages),
                tool_calls=copy.deepcopy(session.tool_calls),
                # Reset ephemeral / per-session-instance flags. Duplicating an
                # archived conversation should produce a visible (un-archived)
                # copy; pinned status doesn't transfer either.
                pinned=False,
                archived=False,
                project_id=session.project_id,
                profile=session.profile,
                input_tokens=session.input_tokens,
                output_tokens=session.output_tokens,
                estimated_cost=session.estimated_cost,
                cache_read_tokens=getattr(session, "cache_read_tokens", 0),
                cache_write_tokens=getattr(session, "cache_write_tokens", 0),
                # Per-session settings the user may have customized — carry them over
                # so the duplicate behaves identically until further edits. Compression
                # anchor + last_prompt_tokens are intentionally NOT carried — those
                # re-derive on the next turn.
                personality=session.personality,
                enabled_toolsets=getattr(session, "enabled_toolsets", None),
                context_length=getattr(session, "context_length", None),
                threshold_tokens=getattr(session, "threshold_tokens", None),
                truncation_watermark=getattr(session, "truncation_watermark", None),
                truncation_boundary=getattr(session, "truncation_boundary", None),
                # context_messages is the authoritative model-facing prefix — must be
                # deepcopied so the duplicate has its own independent context that won't
                # be mutated when the original session's context changes (#2914).
                context_messages=copy.deepcopy(getattr(session, "context_messages", None) or []),
                # Gateway routing — if the user customized routing for this session,
                # the duplicate should behave identically.
                gateway_routing=copy.deepcopy(getattr(session, "gateway_routing", None)),
                gateway_routing_history=copy.deepcopy(getattr(session, "gateway_routing_history", None) or []),
                # Preserve LLM-generated title flag so we don't regenerate title on duplicate.
                llm_title_generated=getattr(session, "llm_title_generated", False),
                manual_title=getattr(session, "manual_title", False),
                # Composer draft — preserve per-session draft state.
                composer_draft=copy.deepcopy(getattr(session, "composer_draft", None) or {}),
                # Context engine state — preserve so the duplicate's context engine
                # starts from the same point as the original.
                context_engine=getattr(session, "context_engine", None),
                context_engine_state=copy.deepcopy(getattr(session, "context_engine_state", None) or {}),
                created_at=time.time(),
                updated_at=time.time(),
            )

            # Persist immediately. The pre-PR flow (/api/session/new + /api/session/rename)
            # accidentally avoided this because `/api/session/rename` calls `s.save()`.
            # Without this explicit save, the duplicate is in-memory only — if the user
            # refreshes before sending a turn, the duplicate vanishes.
            _publish_materialized_session(copied_session, persist=True)
            publish_session_list_changed(
                "session_duplicate",
                profile=getattr(copied_session, "profile", None),
                session_id=getattr(copied_session, "session_id", None),
            )

            return j(handler, {"session": copied_session.compact() | {"messages": copied_session.messages}})
        except Exception as e:
            return bad(handler, str(e))

    if parsed.path == "/api/default-model":
        try:
            advanced = body.get("advanced") if isinstance(body, dict) else None
            provider = body.get("provider") if isinstance(body, dict) else None
            if str(provider or "").strip().lower() == "auto":
                provider = None
            return j(handler, set_hermes_default_model(body.get("model"), provider=provider, advanced=advanced))
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    # ── Auxiliary model set (POST) ──
    if parsed.path == "/api/model/set":
        scope = str(body.get("scope") or "").strip()
        task = str(body.get("task") or "").strip()
        provider = str(body.get("provider") or "auto").strip()
        model = str(body.get("model") or "").strip()
        advanced = body.get("advanced") if isinstance(body, dict) else None
        if scope == "auxiliary":
            from api.config import set_auxiliary_model
            try:
                return j(handler, set_auxiliary_model(task, provider, model, advanced=advanced))
            except Exception as exc:
                return bad(handler, str(exc), status=400)
        if scope == "main":
            try:
                main_provider = provider if provider != "auto" else None
                return j(handler, set_hermes_default_model(model, provider=main_provider, advanced=advanced))
            except ValueError as exc:
                return bad(handler, str(exc), status=400)
        return bad(handler, f"unknown scope: {scope}", status=400)

    # ── Providers (POST) ──
    if parsed.path == "/api/providers":
        provider_id = (body.get("provider") or "").strip().lower()
        api_key = body.get("api_key")
        if not provider_id:
            return bad(handler, "provider is required")
        if api_key is not None:
            api_key = str(api_key).strip() or None
        result = set_provider_key(provider_id, api_key)
        if not result.get("ok"):
            return bad(handler, result.get("error", "Unknown error"))
        return j(handler, result)

    if parsed.path == "/api/providers/delete":
        provider_id = (body.get("provider") or "").strip().lower()
        if not provider_id:
            return bad(handler, "provider is required")
        result = remove_provider_key(provider_id)
        if not result.get("ok"):
            return bad(handler, result.get("error", "Unknown error"))
        return j(handler, result)

    if parsed.path == "/api/providers/self-hosted":
        try:
            from api.onboarding import apply_self_hosted_provider_setup
            return j(handler, apply_self_hosted_provider_setup(body))
        except ValueError as exc:
            return bad(handler, str(exc), 400)

    if parsed.path == "/api/models/refresh":
        provider_id = (body.get("provider") or "").strip().lower()
        if not provider_id:
            return bad(handler, "provider is required")
        from api.config import invalidate_provider_models_cache
        invalidate_provider_models_cache(provider_id)
        _clear_live_models_cache()
        return j(handler, {"ok": True, "provider": provider_id})

    if parsed.path == "/api/reasoning":
        # CLI-parity /reasoning handler — writes to the same config.yaml keys
        # the CLI uses (display.show_reasoning, agent.reasoning_effort) so a
        # preference set via WebUI is honoured in the terminal REPL and vice
        # versa.  Body is one of:
        #   {"display": "show"|"hide"|"on"|"off"}   → display.show_reasoning
        #   {"effort":  "none"|"minimal"|"low"|"medium"|"high"|"xhigh"}
        #                                            → agent.reasoning_effort
        try:
            display = body.get("display")
            effort = body.get("effort")
            if display is not None:
                flag = str(display).strip().lower()
                if flag in ("show", "on", "true", "1"):
                    return j(handler, set_reasoning_display(True))
                if flag in ("hide", "off", "false", "0"):
                    return j(handler, set_reasoning_display(False))
                return bad(handler, f"display must be show|hide|on|off (got '{display}')")
            if effort is not None:
                model_id = str(body.get("model") or "").strip() or None
                provider_id = str(body.get("provider") or "").strip() or None
                base_url = str(body.get("base_url") or "").strip() or None
                return j(
                    handler,
                    set_reasoning_effort(
                        effort,
                        model_id=model_id,
                        provider_id=provider_id,
                        base_url=base_url,
                    ),
                )
            return bad(handler, "reasoning: must supply 'display' or 'effort'")
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    if parsed.path == "/api/admin/reload":
        # Hot-reload the session store to pick up code changes without restart.
        import importlib
        import api.sessions.store as _models
        importlib.reload(_models)
        # Also re-expose get_session from the reloaded module so routes.py
        # continues to work (routes.py imported it at module level).
        import api.routes as _routes
        _routes.get_session = _models.get_session
        _routes.Session = _models.Session
        _routes.compact = _models.compact
        return j(handler, {"status": "ok", "reloaded": "api.sessions.store"})

    if parsed.path == "/api/sessions/cleanup":
        return _handle_sessions_cleanup(handler, body, zero_only=False)

    if parsed.path == "/api/sessions/cleanup_zero_message":
        return _handle_sessions_cleanup(handler, body, zero_only=True)

    if parsed.path == "/api/session/anchor-scene":
        return _handle_session_anchor_scene(handler, body)

    if parsed.path == "/api/session/rename":
        try:
            require(body, "session_id", "title")
        except ValueError as e:
            return bad(handler, str(e))
        try:
            s = _get_or_materialize_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(handler, "Read-only imported sessions cannot be renamed from WebUI", 403)
        with edit_session(body["session_id"], session=s) as s:
            from api.sessions.operations import apply_session_title_rename
            apply_session_title_rename(s, body["title"])
        _sync_session_title_to_insights(s)
        publish_session_list_changed(
            "session_rename",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", body["session_id"]),
        )
        return j(handler, {"session": s.compact()})


    if parsed.path == "/api/session/title/regenerate":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        prefer_latest = bool(body.get("prefer_latest", False))
        try:
            s = _get_or_materialize_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(handler, "Read-only imported sessions cannot regenerate titles", 403)
        next_title, reason, raw_preview = generate_session_title_for_session(s, prefer_latest=prefer_latest)
        if not next_title:
            return bad(handler, f"Could not generate a better title ({reason or 'empty'})", 422)
        _persist_generated_session_title(s, next_title, event_reason="session_title_regenerate")
        return j(handler, {
            "session": s.compact(),
            "title": s.title,
            "status": reason,
            "raw_preview": (raw_preview or "")[:240],
        })

    if parsed.path == "/api/personality/set":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if "name" not in body:
            return bad(handler, "Missing required field: name")
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(handler, "Subagent sessions are view-only and cannot be modified from WebUI", 400)
        name = body["name"].strip()
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        # Resolve personality from config.yaml agent.personalities section
        # (matches hermes-agent CLI behavior)
        prompt = ""
        if name:
            from api.config import reload_config as _reload_cfg2

            _reload_cfg2()  # pick up config changes without restart
            from api.config import get_config as _get_cfg2

            _cfg2 = _get_cfg2()
            agent_cfg = _cfg2.get("agent", {})
            raw_personalities = agent_cfg.get("personalities", {})
            if not isinstance(raw_personalities, dict) or name not in raw_personalities:
                return bad(
                    handler, f'Personality "{name}" not found in config.yaml', 404
                )
            value = raw_personalities[name]
            # Resolve prompt using the same logic as hermes-agent cli.py
            if isinstance(value, dict):
                parts = [value.get("system_prompt", "") or value.get("prompt", "")]
                if value.get("tone"):
                    parts.append(f"Tone: {value['tone']}")
                if value.get("style"):
                    parts.append(f"Style: {value['style']}")
                prompt = "\n".join(p for p in parts if p)
            else:
                prompt = str(value)
        with edit_session(sid, session=s) as s:
            s.personality = name if name else None
        return j(handler, {"ok": True, "personality": s.personality, "prompt": prompt})

    if parsed.path == "/api/session/toolsets":
        """Set or clear per-session toolset override (#493).

        POST body: { session_id, toolsets: [...] | null }
        - toolsets: list of toolset names to restrict the session to, or null to clear.
        """
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(handler, "Subagent sessions are view-only and cannot be modified from WebUI", 400)
        toolsets = body.get("toolsets")
        try:
            toolsets = _validate_session_toolsets_shape(toolsets)
        except ValueError as e:
            return bad(handler, str(e), status=400)
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        with edit_session(sid, session=s) as s:
            s.enabled_toolsets = toolsets
        return j(handler, {"ok": True, "enabled_toolsets": s.enabled_toolsets})

    if parsed.path == "/api/session/draft":
        # POST body → save draft { session_id, text?, files? }. Current draft
        # state is returned as part of GET /api/session.
        import time as _draft_time
        _draft_t0 = _draft_time.monotonic()
        _draft_stages = []

        def _draft_mark(name):
            _draft_stages.append((name, _draft_time.monotonic()))
        _draft_mark("enter")
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(handler, "Subagent sessions are view-only and cannot store a draft from WebUI", 400)
        text = body.get("text")
        files = body.get("files")
        # Stage-326 hardening (per Opus advisor): size + type validation on
        # the draft inputs. Without this, a misbehaving or malicious client
        # can persist multi-MB strings into the session JSON on every keystroke
        # via the 400ms debounced auto-save.
        _MAX_DRAFT_TEXT = 50_000  # 50 KB cap on textarea content
        _MAX_DRAFT_FILES = 50  # max number of attached file references
        if text is not None and not isinstance(text, str):
            text = ""
        if isinstance(text, str) and len(text) > _MAX_DRAFT_TEXT:
            text = text[:_MAX_DRAFT_TEXT]
        if files is not None and not isinstance(files, list):
            files = []
        if isinstance(files, list) and len(files) > _MAX_DRAFT_FILES:
            files = files[:_MAX_DRAFT_FILES]
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        _draft_mark("after_get_session")
        unchanged = False
        with edit_session(
            sid,
            session=s,
            touch_updated_at=False,
            skip_index=True,
            save_when=lambda _session: not unchanged,
        ) as s:
            _draft_mark("acquired_lock")
            current_draft = dict(getattr(s, "composer_draft", {}) or {})
            next_draft = dict(current_draft)
            if text is not None:
                next_draft["text"] = text
            if files is not None:
                next_draft["files"] = files
            if next_draft == current_draft:
                unchanged = True
                saved_draft = current_draft
            else:
                s.composer_draft = next_draft
                # Draft persistence is not conversation activity. Touching updated_at
                # here makes the active-session external-refresh poll force-reload the
                # current chat every few seconds while the user is typing, and that
                # delayed reload can restore an older draft over newer local input.
                _draft_mark("before_save")
                saved_draft = s.composer_draft
        _draft_mark("released_lock")
        payload = {"ok": True, "draft": saved_draft}
        if unchanged:
            payload["unchanged"] = True
        _draft_mark("before_json")
        j(handler, payload)
        _draft_mark("after_json")
        _draft_stages.append(("end", _draft_time.monotonic()))
        if _draft_stages[-1][1] - _draft_t0 > 0.2:
            parts = " ".join(
                f"{n}={((t - prev[1]) * 1000):.1f}ms"
                for (n, t), prev in zip(_draft_stages[1:], _draft_stages[:-1], strict=True)
            )
            handler._safe_webui_print(
                "[SLOW] /api/session/draft total=%.1fms stages: %s" % (
                    (_draft_stages[-1][1] - _draft_t0) * 1000,
                    parts,
                )
            )
        return True

    if parsed.path == "/api/session/update":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        try:
            s = _get_or_materialize_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(handler, "Read-only imported sessions cannot be updated from WebUI", 403)
        old_ws = getattr(s, "workspace", "")
        old_model = getattr(s, "model", None)
        old_provider = getattr(s, "model_provider", None)
        try:
            new_ws = str(resolve_trusted_workspace(body.get("workspace", s.workspace)))
        except ValueError as e:
            return bad(handler, str(e))
        with edit_session(body["session_id"], session=s) as s:
            s.workspace = new_ws
            if "model" in body or "model_provider" in body:
                model, provider = _session_model_state_from_request(
                    body.get("model", s.model),
                    body.get("model_provider") if "model_provider" in body else None,
                    getattr(s, "model_provider", None),
                )
                if model is not None:
                    s.model = model
                s.model_provider = provider
                if (
                    str(old_model or "") != str(getattr(s, "model", "") or "")
                    or str(old_provider or "") != str(getattr(s, "model_provider", "") or "")
                ):
                    s.context_length = _resolve_context_length_for_session_model(
                        getattr(s, "model", None),
                        getattr(s, "model_provider", None),
                    )
                    s.threshold_tokens = 0
                    s.last_prompt_tokens = 0
                    from api.config import _evict_session_agent

                    _evict_session_agent(body["session_id"])
        if str(old_ws or "") != str(new_ws or ""):
            try:
                from api.terminal import close_terminal
                close_terminal(body["session_id"])
            except Exception:
                logger.debug("Failed to close workspace terminal after workspace update")
        set_last_workspace(new_ws)
        return j(handler, {"session": s.compact() | {"messages": s.messages}})
    if parsed.path == "/api/session/worktree/remove":
        sid = body.get("session_id", "")
        if not sid or not isinstance(sid, str) or not sid.strip():
            return bad(handler, "session_id must be a non-empty string", status=400)
        sid = sid.strip()
        if not is_safe_session_id(sid):
            return bad(handler, "Invalid session_id", 400)
        try:
            s = get_session(sid, metadata_only=True)
        except KeyError:
            return bad(handler, "Session not found", status=404)
        force = bool(body.get("force", False))
        try:
            from api.worktrees import remove_worktree_for_session

            result = remove_worktree_for_session(s, force=force)
            return j(handler, result)
        except ValueError as exc:
            return bad(handler, str(exc), status=400)
        except Exception as exc:
            logger.exception("failed to remove worktree for session %s", sid)
            return bad(handler, _sanitize_error(exc), status=500)

    if parsed.path == "/api/session/delete":
        sid = body.get("session_id", "")
        if not sid:
            return bad(handler, "session_id is required")
        if not is_safe_session_id(sid):
            return bad(handler, "Invalid session_id", 400)
        cli_meta_for_delete = _lookup_cli_session_metadata(sid)
        if cli_meta_for_delete.get("read_only"):
            return bad(handler, "Read-only imported sessions cannot be deleted from WebUI", 400)
        # A delegated subagent child (#5307) is view-only and owned by the
        # delegate runner. Deleting it here would call delete_cli_session() and
        # erase the child's state.db transcript — refuse it.
        if _session_is_subagent_view_only(sid):
            return bad(handler, "Subagent sessions are view-only and cannot be deleted from WebUI", 400)
        is_messaging_session = _is_messaging_session_id(sid)
        worktree_retained = _worktree_retained_payload_for_session_id(sid)
        try:
            event_profile = getattr(get_session(sid, metadata_only=True), "profile", None)
        except KeyError:
            event_profile = None
        except Exception:
            logger.debug("Failed to resolve profile for deleted session %s", sid, exc_info=True)
            event_profile = None
        try:
            deletion = delete_session_state(sid, messaging=is_messaging_session)
        except SessionActiveError:
            return bad(
                handler,
                "Stop the active response before deleting this session",
                409,
            )
        except ValueError:
            return bad(handler, "Invalid session_id", 400)
        except Exception:
            logger.exception("Failed to delete session state for %s", sid)
            return bad(handler, "Failed to delete session", 500)
        _publish_session_list_changed("session_delete", profile=event_profile)
        return j(
            handler,
            {
                "ok": True,
                "state_db_cleanup_failed": deletion.state_db_cleanup_failed,
                **worktree_retained,
            },
        )

    if parsed.path == "/api/session/clear":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(handler, "Subagent sessions are view-only and cannot be modified from WebUI", 400)
        try:
            s = get_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        sid = body["session_id"]
        with edit_session(sid, session=s) as s:
            had_sidecar_messages = bool(s.messages or [])
            # Clear is a full truncate-to-empty: route through the SAME helper the
            # /api/session/truncate handler uses (single source of truth) so the
            # display + context arrays are emptied AND the truncation watermark is
            # set via _truncation_watermark_for([]) == 0.0 — the #2914
            # truncate-to-empty sentinel that blocks state.db replay. Before this,
            # /clear wiped s.messages but left the watermark unset, so the
            # append-only state.db merge treated it as "keep everything" and the
            # cleared history resurrected on the next /api/session read (#5532).
            from api.sessions.operations import truncate_session_at_keep
            truncate_session_at_keep(s, 0)
            s.tool_calls = []
            # A compressed-continuation child keeps its archived transcript in a
            # parent sidecar marked pre_compression_snapshot;
            # _webui_sidecar_lineage_messages_for_display() stitches that parent
            # back with truncation_watermark=None, so the 0.0 sentinel on the
            # CHILD does NOT stop the parent from resurrecting the cleared history
            # on refresh. Detach the compression lineage (#5532/#5553) — but ONLY
            # when the parent is actually a pre_compression_snapshot; a genuine
            # fork parent (session_source="fork" from /api/session/branch) must
            # keep its link so the child still nests + shows "Forked from"
            # (sessions.js:5720/5964/7105). Dropping every parent broke that
            # (#5532 Codex gate).
            _parent_sid = getattr(s, "parent_session_id", None)
            if _parent_sid:
                _parent_is_compression_snapshot = False
                try:
                    _parent = get_session(_parent_sid, metadata_only=True)
                    _parent_is_compression_snapshot = bool(
                        getattr(_parent, "pre_compression_snapshot", False)
                    )
                except Exception:
                    _parent_is_compression_snapshot = False
                if _parent_is_compression_snapshot:
                    s.parent_session_id = None
                    s.compression_anchor_visible_idx = None
                    s.compression_anchor_message_key = None
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            s.pending_user_source = None
            s.clear_generation = uuid.uuid4().hex if had_sidecar_messages else None
            # Reset the title via the rename helper so clearing a manually-named
            # session also clears manual_title/llm_title_generated — otherwise the
            # reused session keeps its manual-title protection and never auto-names
            # again (#3542 lifecycle gap).
            from api.sessions.operations import apply_session_title_rename
            apply_session_title_rename(s, "Untitled")
        persisted_clear = False
        try:
            persisted = json.loads(s.path.read_text(encoding="utf-8"))
            persisted_clear = (
                persisted.get("messages") == []
                and persisted.get("context_messages") == []
                and persisted.get("truncation_watermark") == 0.0
                and persisted.get("truncation_boundary") == 0.0
                and persisted.get("active_stream_id") is None
                and persisted.get("pending_user_message") is None
                and persisted.get("pending_attachments") == []
                and persisted.get("pending_started_at") is None
                and persisted.get("pending_user_source") is None
                and persisted.get("clear_generation") == s.clear_generation
            )
        except (OSError, json.JSONDecodeError, ValueError):
            logger.warning("session clear could not verify persisted empty state for %s", sid, exc_info=True)
        if had_sidecar_messages and persisted_clear:
            try:
                s.path.with_suffix('.json.bak').unlink(missing_ok=True)
            except OSError:
                logger.warning("session clear could not remove stale backup for %s", sid, exc_info=True)
        # Evict cached agent outside the per-session lock.  Eviction may run a
        # boundary memory commit for batch-extraction providers, and provider
        # I/O must not hold the session mutation lock.
        from api.config import _evict_session_agent
        _evict_session_agent(sid)
        return j(handler, {"ok": True, "session": s.compact()})

    if parsed.path == "/api/session/truncate":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(handler, "Subagent sessions are view-only and cannot be modified from WebUI", 400)
        if body.get("keep_count") is None:
            return bad(handler, "Missing required field(s): keep_count")
        try:
            s = get_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        # Validate keep_count before it reaches the destructive `messages[:keep]`
        # slice. A non-numeric value would raise ValueError and surface as a
        # confusing 500; a NEGATIVE value slices as `messages[:-N]`, which
        # silently DELETES the most recent N messages (e.g. keep_count=-5 on a
        # 3-message session wipes the whole transcript) and then persists it via
        # save(). Mirror the explicit guard the /api/session/branch handler
        # already applies to its own keep_count. (Opus pre-release follow-up.)
        try:
            keep = int(body["keep_count"])
        except (ValueError, TypeError):
            return bad(handler, "keep_count must be an integer")
        if keep < 0:
            return bad(handler, "keep_count must be non-negative")
        with edit_session(body["session_id"], session=s) as s:
            from api.sessions.operations import truncate_session_at_keep

            old_msg_count, old_ctx_count = truncate_session_at_keep(s, keep)
            logger.info(
                "truncate %s: messages %d→%d, context_messages %d→%d, watermark=%.2f",
                body["session_id"], old_msg_count, len(s.messages or []),
                old_ctx_count, len(getattr(s, 'context_messages', None) or []),
                s.truncation_watermark or 0,
            )
        from api.config import _evict_session_agent
        _evict_session_agent(body["session_id"])
        return j(
            handler, {"ok": True, "session": s.compact() | {"messages": s.messages}}
        )

    if parsed.path == "/api/session/branch":
        # Fork a conversation from any message point (#465).
        # Accepts: {session_id, keep_count?, title?}
        #   keep_count: number of messages to copy (0=empty, undefined=full history)
        #   title: custom title (defaults to "<original title> (fork)")
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        # Reject non-string session_id explicitly so the failure surfaces as a
        # 400 instead of a generic 500 from get_session() raising TypeError.
        # (Opus pre-release follow-up.)
        if not isinstance(body["session_id"], str):
            return bad(handler, "session_id must be a string")
        source = _load_branch_source_or_refuse(handler, body["session_id"])
        if source is None:
            return True

        keep_count = body.get("keep_count")
        if keep_count is not None:
            try:
                keep_count = int(keep_count)
            except (ValueError, TypeError):
                return bad(handler, "keep_count must be an integer")
            # Negative slice (`messages[:-N]`) returns "all but last N", which
            # is a confusing fork semantic. Reject explicitly so the user
            # doesn't accidentally fork a session with the tail truncated when
            # they meant to copy the prefix. (Opus pre-release follow-up.)
            if keep_count < 0:
                return bad(handler, "keep_count must be non-negative")

        custom_title = body.get("title")
        if custom_title:
            custom_title = str(custom_title).strip()[:80] or None

        # Build messages slice in the same coordinate space exposed by GET
        # /api/session so frontend keep_count values from merged messaging
        # transcripts do not silently become full sidecar copies.
        try:
            if not getattr(source, "_branch_source_readonly", False): source.save()
        except Exception:
            pass
        cli_meta = _lookup_cli_session_metadata(source.session_id) if _session_requires_cli_metadata_lookup(source) else {}
        is_messaging_session = _is_messaging_session_record(source) or _is_messaging_session_record(cli_meta)
        cli_messages = get_cli_session_messages(source.session_id) if is_messaging_session else []
        source_messages = (
            _merged_session_messages_for_display(source, cli_messages)
            if is_messaging_session and cli_messages
            else list(source.messages or [])
        )
        if keep_count is not None:
            forked_messages = source_messages[:keep_count]
        else:
            forked_messages = list(source_messages)

        # Derive title
        if custom_title:
            branch_title = custom_title
        else:
            source_title = source.title or "Untitled"
            branch_title = f"{source_title} (fork)"

        # Create new session inheriting workspace/model/profile
        from api.sessions.operations import truncate_context_for_display_keep

        fork_keep = keep_count if keep_count is not None else len(source_messages)
        forked_context = truncate_context_for_display_keep(
            getattr(source, "context_messages", None),
            source_messages,
            fork_keep,
        )
        branch = Session(
            workspace=source.workspace,
            model=source.model,
            model_provider=getattr(source, "model_provider", None),
            profile=getattr(source, "profile", None),
            title=branch_title,
            messages=forked_messages,
            project_id=getattr(source, "project_id", None),
            personality=getattr(source, "personality", None),
            enabled_toolsets=getattr(source, "enabled_toolsets", None),
            context_length=getattr(source, "context_length", None),
            threshold_tokens=getattr(source, "threshold_tokens", None),
            # context_messages — truncated to fork prefix (not full parent copy)
            context_messages=copy.deepcopy(forked_context),
            # Gateway routing — inherit from source
            gateway_routing=copy.deepcopy(getattr(source, "gateway_routing", None)),
            # Context engine — inherit state so branch's context engine starts correctly
            context_engine=getattr(source, "context_engine", None),
            context_engine_state=copy.deepcopy(getattr(source, "context_engine_state", None) or {}),
            parent_session_id=source.session_id,
            session_source="fork",
        )
        # Empty branches intentionally match new_session's memory-only contract;
        # non-empty branches must persist before becoming cache-visible.
        _publish_materialized_session(branch, persist=bool(forked_messages))
        if forked_messages:
            publish_session_list_changed(
                "session_branch",
                profile=getattr(branch, "profile", None),
                session_id=getattr(branch, "session_id", None),
            )

        return j(handler, {
            "session_id": branch.session_id,
            "title": branch_title,
            "parent_session_id": source.session_id,
        })

    if parsed.path == "/api/session/compress/start":
        return _handle_session_compress_start(handler, body)

    if parsed.path == "/api/session/compress":
        return _handle_session_compress(handler, body)

    if parsed.path == "/api/session/conversation-rounds":
        return _handle_conversation_rounds(handler, body)

    if parsed.path == "/api/session/handoff-summary":
        return _handle_handoff_summary(handler, body)

    if parsed.path == "/api/session/retry":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(handler, "Subagent sessions are view-only and cannot be modified from WebUI", 400)
        try:
            from api.sessions.operations import retry_last
            result = retry_last(body["session_id"])
            return j(handler, {"ok": True, **result})
        except KeyError:
            return bad(handler, "Session not found", 404)
        except ValueError as e:
            return j(handler, {"error": str(e)})

    if parsed.path == "/api/session/undo":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(handler, "Subagent sessions are view-only and cannot be modified from WebUI", 400)
        try:
            from api.sessions.operations import undo_last
            result = undo_last(body["session_id"])
            return j(handler, {"ok": True, **result})
        except KeyError:
            return bad(handler, "Session not found", 404)
        except ValueError as e:
            return j(handler, {"error": str(e)})

    # ── YOLO mode toggle (POST) ──
    # Session-scoped only — stored in-memory on the server side.
    # Important lifecycle notes:
    #   • Page reload: state PERSISTS (frontend re-fetches via GET endpoint)
    #   • Cross-tab: state is SHARED (same server-side flag per session)
    #   • Server restart: state is LOST (in-memory only)
    #   • Cross-session: isolated (each session has its own flag)
    # Fixes #467
    if parsed.path == "/api/session/yolo":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        enabled = bool(body.get("enabled", True))
        if enabled:
            enable_session_yolo(sid)
            # Also resolve any pending approvals for this session so the
            # agent doesn't stay stuck waiting on an already-dismissed card.
            try:
                from tools.approval import _pending as _p, _lock as _l
                with _l:
                    _p.pop(sid, None)
            except Exception:
                pass
            resolve_gateway_approval(sid, "once", resolve_all=True)
        else:
            disable_session_yolo(sid)
        return j(handler, {"ok": True, "yolo_enabled": enabled})

    if parsed.path == "/api/btw":
        return _handle_btw(handler, body)

    if parsed.path == "/api/background":
        return _handle_background(handler, body)

    if parsed.path == "/api/goal":
        return _handle_goal_command(handler, body)

    if parsed.path == "/api/bg-task-complete-ack":
        return _handle_bg_task_complete_ack(handler, body)

    if parsed.path == "/api/chat/start":
        return _handle_chat_start(handler, body, diag=diag)

    if parsed.path == "/api/chat":
        return _handle_chat_sync(handler, body)

    if parsed.path == "/api/chat/steer":
        from api.streaming import _handle_chat_steer
        return _handle_chat_steer(handler, body)

    if parsed.path == "/api/terminal/start":
        return _handle_terminal_start(handler, body)

    if parsed.path == "/api/terminal/input":
        return _handle_terminal_input(handler, body)

    if parsed.path == "/api/terminal/resize":
        return _handle_terminal_resize(handler, body)

    if parsed.path == "/api/terminal/close":
        return _handle_terminal_close(handler, body)

    # ── Cron API (POST) ──
    # See GET-side comment above: wrap in cron_profile_context so writes go
    # to the TLS-active profile's jobs.json instead of the process default.
    if parsed.path == "/api/crons/create":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_create(handler, body)

    if parsed.path == "/api/crons/update":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_update(handler, body)

    if parsed.path == "/api/crons/delete":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_delete(handler, body)

    if parsed.path == "/api/crons/run":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_run(handler, body)

    if parsed.path == "/api/crons/pause":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_pause(handler, body)

    if parsed.path == "/api/crons/resume":
        from api.profiles import cron_profile_context

        with cron_profile_context():
            _ensure_agent_cron_import_path()
            return _handle_cron_resume(handler, body)

    # ── Git workspace ops (POST) ──
    if parsed.path == "/api/git/stage":
        return _handle_git_stage(handler, body)

    if parsed.path == "/api/git/unstage":
        return _handle_git_unstage(handler, body)

    if parsed.path == "/api/git/discard":
        return _handle_git_discard(handler, body)

    if parsed.path == "/api/git/commit-message":
        return _handle_git_commit_message(handler, body)

    if parsed.path == "/api/git/commit-message-selected":
        return _handle_git_commit_message_selected(handler, body)

    if parsed.path == "/api/git/commit":
        return _handle_git_commit(handler, body)

    if parsed.path == "/api/git/commit-selected":
        return _handle_git_commit_selected(handler, body)

    if parsed.path == "/api/git/fetch":
        return _handle_git_remote_action(handler, body, "fetch")

    if parsed.path == "/api/git/pull":
        return _handle_git_remote_action(handler, body, "pull")

    if parsed.path == "/api/git/push":
        return _handle_git_remote_action(handler, body, "push")

    if parsed.path == "/api/git/checkout":
        return _handle_git_checkout(handler, body)

    if parsed.path == "/api/git/stash-checkout":
        return _handle_git_stash_checkout(handler, body)

    # ── File ops (POST) ──
    if parsed.path == "/api/file/delete":
        return _handle_file_delete(handler, body)

    if parsed.path == "/api/file/save":
        return _handle_file_save(handler, body)

    if parsed.path == "/api/file/office-save":
        return _handle_office_file_save(handler, body)

    if parsed.path == "/api/file/create":
        return _handle_file_create(handler, body)

    if parsed.path == "/api/file/rename":
        return _handle_file_rename(handler, body)

    if parsed.path == "/api/file/move":
        return _handle_file_move(handler, body)

    if parsed.path == "/api/file/create-dir":
        return _handle_create_dir(handler, body)

    if parsed.path == "/api/file/reveal":
        return _handle_file_reveal(handler, body)

    if parsed.path == "/api/file/path":
        return _handle_file_path(handler, body)

    if parsed.path == "/api/file/open-vscode":
        return _handle_file_open_vscode(handler, body)

    # ── Workspace management (POST) ──
    if parsed.path == "/api/workspaces/add":
        return _handle_workspace_add(handler, body)

    if parsed.path == "/api/workspaces/remove":
        return _handle_workspace_remove(handler, body)

    if parsed.path == "/api/workspaces/rename":
        return _handle_workspace_rename(handler, body)

    if parsed.path == "/api/workspaces/reorder":
        return _handle_workspace_reorder(handler, body)

    # ── Approval (POST) ──
    if parsed.path == "/api/approval/respond":
        return _handle_approval_respond(handler, body)

    # ── Clarify (POST) ──
    if parsed.path == "/api/clarify/respond":
        return _handle_clarify_respond(handler, body)

    # ── Commands (POST) ──
    if parsed.path == "/api/commands/bundles/resolve":
        from api.commands import resolve_bundle_command

        command = str(body.get("command", "") or "").strip()
        if not command:
            return bad(handler, "command is required")

        try:
            return j(handler, resolve_bundle_command(command))
        except KeyError:
            return bad(handler, "Bundle command not found", 404)
        except ValueError as e:
            return bad(handler, str(e), 400)
        except RuntimeError as e:
            return bad(handler, _sanitize_error(e), 500)

    if parsed.path == "/api/commands/exec":
        from api.commands import execute_agent_command, execute_plugin_command

        command = str(body.get("command", "") or "").strip()
        if not command:
            return bad(handler, "command is required")

        try:
            return j(handler, {"output": execute_agent_command(command)})
        except KeyError:
            pass
        except ValueError as e:
            return bad(handler, str(e), 400)
        except RuntimeError as e:
            return bad(handler, _sanitize_error(e), 500)

        try:
            return j(handler, {"output": execute_plugin_command(command)})
        except ValueError as e:
            return bad(handler, str(e), 400)
        except KeyError:
            return bad(handler, "Plugin command not found", 404)
        except RuntimeError as e:
            return bad(handler, _sanitize_error(e), 500)

    # ── Skills (POST) ──
    if parsed.path == "/api/skills/save":
        return _handle_skill_save(handler, body)

    if parsed.path == "/api/skills/delete":
        return _handle_skill_delete(handler, body)

    if parsed.path == "/api/skills/toggle":
        return _handle_skill_toggle(handler, body)

    # ── Memory (POST) ──
    if parsed.path == "/api/memory/write":
        return _handle_memory_write(handler, body)

    if parsed.path in {"/api/gateway/start", "/api/gateway/stop", "/api/gateway/restart"}:
        return _handle_gateway_lifecycle(handler, parsed.path.rsplit("/", 1)[-1], body)

    # ── Profile API (POST) ──
    if parsed.path == "/api/profile/switch":
        name = body.get("name", "").strip()
        if not name:
            return bad(handler, "name is required")
        try:
            from api.auth import ensure_trusted_auth_session
            from api.profiles import switch_profile, _validate_profile_name
            from api.helpers import build_profile_cookie
            if name != 'default':
                _validate_profile_name(name)
            session_info = ensure_trusted_auth_session(handler)
            if getattr(handler, '_trusted_auth_session_rejected', False):
                return bad(handler, 'Authentication required', 401)
            bound_profile = str((session_info or {}).get("bound_profile") or "").strip() or None
            if bound_profile and name != bound_profile:
                return bad(handler, "Profile is bound to the current session", 403)
            # process_wide=False: don't mutate the process-global _active_profile.
            # Per-client profile is managed via cookie + thread-local (#798).
            result = switch_profile(name, process_wide=False)
            # Invalidate the models cache so the very next /api/models request
            # rebuilds from the new profile's config.yaml rather than returning
            # the old profile's cached model list (#1200 — profile-switch model bug).
            from api.config import invalidate_models_cache
            invalidate_models_cache()
            try:
                from api.gateway_watcher import restart_watcher_for_profile
                restart_watcher_for_profile(name)
            except Exception as exc:
                logger.warning("Failed to restart gateway watcher for profile %s: %s", name, exc)
            session_cookie_value = getattr(handler, '_trusted_auth_session_cookie_value', None)
            if session_cookie_value:
                if bound_profile and name == bound_profile:
                    return j(handler, result)
                extra_header = build_profile_cookie(name, session_cookie_value=session_cookie_value)
            else:
                extra_header = build_profile_cookie(name, handler)
            return j(handler, result, extra_headers={
                'Set-Cookie': extra_header,
            })
        except PermissionError as e:
            return bad(handler, _sanitize_error(e), 403)
        except (ValueError, FileNotFoundError) as e:
            return bad(handler, _sanitize_error(e), 404)
        except RuntimeError as e:
            return bad(handler, str(e), 409)

    if parsed.path == "/api/profile/create":
        name = body.get("name", "").strip()
        if not name:
            return bad(handler, "name is required")
        import re as _re

        if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name):
            return bad(
                handler,
                "Invalid profile name: lowercase letters, numbers, hyphens, underscores only",
            )
        clone_from = body.get("clone_from")
        if clone_from is not None:
            clone_from = str(clone_from).strip()
            if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", clone_from):
                return bad(handler, "Invalid clone_from name")
        base_url = body.get("base_url", "").strip() if body.get("base_url") else None
        api_key = body.get("api_key", "").strip() if body.get("api_key") else None
        default_model = body.get("default_model", "").strip() if body.get("default_model") else None
        model_provider = body.get("model_provider", "").strip() if body.get("model_provider") else None
        if base_url and not base_url.startswith(("http://", "https://")):
            return bad(handler, "base_url must start with http:// or https://")
        try:
            from api.profiles import create_profile_api

            result = create_profile_api(
                name,
                clone_from=clone_from,
                clone_config=bool(body.get("clone_config", False)),
                base_url=base_url,
                api_key=api_key,
                default_model=default_model,
                model_provider=model_provider,
            )
            return j(handler, {"ok": True, "profile": result})
        except PermissionError as e:
            return bad(handler, _sanitize_error(e), 403)
        except (ValueError, FileExistsError, RuntimeError) as e:
            return bad(handler, str(e))

    if parsed.path == "/api/profile/delete":
        name = body.get("name", "").strip()
        if not name:
            return bad(handler, "name is required")
        try:
            from api.profiles import delete_profile_api, _validate_profile_name

            _validate_profile_name(name)
            result = delete_profile_api(name)
            return j(handler, result)
        except PermissionError as e:
            return bad(handler, _sanitize_error(e), 403)
        except (ValueError, FileNotFoundError) as e:
            return bad(handler, _sanitize_error(e))
        except RuntimeError as e:
            return bad(handler, str(e), 409)

    # ── Settings (POST) ──
    if parsed.path == "/api/settings":
        from api.auth import (
            create_session,
            get_password_hash,
            is_auth_enabled,
            parse_cookie,
            set_auth_cookie,
            verify_password,
            verify_session,
        )

        if "bot_name" in body:
            body["bot_name"] = (str(body["bot_name"]) or "").strip() or "Hermes"

        auth_enabled_before = is_auth_enabled()
        password_auth_enabled_before = auth_enabled_before and get_password_hash() is not None
        current_cookie = parse_cookie(handler)
        logged_in_before = bool(current_cookie and verify_session(current_cookie))
        requested_password = bool(
            isinstance(body.get("_set_password"), str)
            and body.get("_set_password", "").strip()
        )
        requested_passwordless = bool(body.pop("_passwordless", False))
        requested_clear_password = bool(body.get("_clear_password") or requested_passwordless)
        if requested_passwordless:
            body["_clear_password"] = True

        current_password = body.pop("_current_password", None)

        # #1560: HERMES_WEBUI_PASSWORD env var takes precedence in
        # api.auth.get_password_hash(), so writing password_hash to settings.json
        # has no effect on auth. Refuse loudly with 409 instead of silently
        # succeeding — the previous behaviour returned 200 + a green save toast
        # while every subsequent login still required the env-var password.
        if requested_password or requested_clear_password:
            if os.getenv("HERMES_WEBUI_PASSWORD", "").strip():
                return bad(
                    handler,
                    "HERMES_WEBUI_PASSWORD env var is set — it overrides the settings password. "
                    "Unset the env var and restart the server before changing the password here.",
                    409,
                )

        max_tokens_provided = "max_tokens" in body
        max_tokens_status = None
        max_tokens_value = body.pop("max_tokens", None) if max_tokens_provided else None

        # First password creation decides who owns a previously passwordless
        # WebUI. While auth is disabled, the generic /api/settings route is also
        # unauthenticated, so gate bootstrap password setup the same way as
        # onboarding setup: local/private networks only, unless the operator
        # explicitly opts into remote bootstrap with HERMES_WEBUI_ONBOARDING_OPEN.
        if requested_password and not auth_enabled_before:
            if not _onboarding_gate_allows(handler, auth_enabled_before):
                return bad(
                    handler,
                    "First password setup is only available from local networks when auth is not enabled. "
                    "To bootstrap this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.",
                    403,
                )

        # Auth-disable safety: when password auth is currently enabled, require
        # the current password to change, clear, or switch to passwordless.
        if auth_enabled_before and password_auth_enabled_before and (requested_password or requested_clear_password):
            if not isinstance(current_password, str) or not current_password:
                return bad(
                    handler,
                    "Current password is required to change or disable authentication.",
                    403,
                )
            if not verify_password(current_password):
                return bad(
                    handler,
                    "Current password is incorrect.",
                    403,
                )

        if requested_passwordless:
            from api.auth import _passkey_feature_flag_enabled
            from api.auth import registered_credentials

            if not _passkey_feature_flag_enabled():
                return bad(handler, "Passkey support is disabled. Enable HERMES_WEBUI_PASSKEY before going passwordless.", 409)
            if not registered_credentials():
                return bad(handler, "Register a passkey before going passwordless.", 409)
        elif requested_clear_password:
            from api.auth import clear_credentials

            clear_credentials()

        # Handle auth_disabled_acknowledged setting
        ack = body.pop("_auth_disabled_acknowledged", None)
        if ack is not None and not is_auth_enabled():
            body["auth_disabled_acknowledged"] = bool(ack)
        elif is_auth_enabled() or requested_password:
            body["auth_disabled_acknowledged"] = False

        from api.config import get_max_tokens_status, set_max_tokens

        saved = save_settings(body)
        saved["persisted_speech_keys"] = persisted_speech_settings_keys()
        if max_tokens_provided:
            max_tokens_status = set_max_tokens(max_tokens_value)
        saved.pop("password_hash", None)  # never expose hash to client
        saved.update(max_tokens_status if max_tokens_provided else get_max_tokens_status())

        # Settings that change which sessions appear in the sidebar must
        # invalidate the session-list cache directly. Relying on the cache's
        # settings-file mtime stamp is fragile: a toggle that writes the
        # settings file within the same mtime granularity as a cached entry (and
        # produces the default-valued key, e.g. show_cli_sessions back to its
        # True default) can leave a stale row set served for up to the cache TTL.
        # This is the root cause of the intermittent gateway_sync test flake
        # (a freshly-inserted CLI/gateway session occasionally absent from
        # /api/sessions right after the visibility toggle). Invalidate explicitly.
        if any(
            k in body
            for k in (
                "show_cli_sessions",
                "show_claude_code_sessions",
                "show_cron_sessions",
                "show_webhook_sessions",
                "show_previous_messaging_sessions",
            )
        ):
            try:
                _clear_session_list_cache()
            except Exception:
                pass
            try:
                from api.sessions.store import clear_cli_sessions_cache
                clear_cli_sessions_cache()
            except Exception:
                pass

        auth_enabled_after = is_auth_enabled()
        auth_just_enabled = bool(
            requested_password and auth_enabled_after and not auth_enabled_before
        )
        logged_in_after = logged_in_before
        new_cookie = None

        if auth_just_enabled and not logged_in_before:
            new_cookie = create_session()
            logged_in_after = True

        saved["auth_enabled"] = auth_enabled_after
        saved["password_auth_enabled"] = get_password_hash() is not None
        saved["logged_in"] = logged_in_after
        saved["auth_just_enabled"] = auth_just_enabled
        try:
            from api.auth import _passkey_feature_flag_enabled as _pffe
            from api.auth import registered_credentials as _rc
            if _pffe():
                saved["passkeys_enabled"] = bool(_rc())
                saved["passwordless_enabled"] = bool(_rc()) and not saved["password_auth_enabled"]
            else:
                saved["passkeys_enabled"] = False
                saved["passwordless_enabled"] = False
        except Exception:
            pass

        if not new_cookie:
            return j(handler, saved)

        response_body = json.dumps(saved, ensure_ascii=False, indent=2).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(response_body)))
        handler.send_header("Cache-Control", "no-store")
        set_auth_cookie(handler, new_cookie)
        _security_headers(handler)
        handler.end_headers()
        handler.wfile.write(response_body)
        return True

    if parsed.path == "/api/onboarding/oauth/start":
        if not _onboarding_gate_allows(handler):
            return bad(handler, "Onboarding OAuth is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.", 403)
        try:
            return j(handler, start_onboarding_oauth_flow(body), extra_headers={"Cache-Control": "no-store"})
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    if parsed.path == "/api/onboarding/oauth/cancel":
        try:
            return j(handler, cancel_onboarding_oauth_flow(body), extra_headers={"Cache-Control": "no-store"})
        except ValueError as e:
            return bad(handler, str(e))

    if parsed.path == "/api/onboarding/setup":
        # Writing API keys to disk - restrict to local/private networks unless auth is active.
        # In Docker, requests arrive from the bridge network (172.x.x.x), not 127.0.0.1,
        # even when the user accesses via localhost:8787 on the host.
        # Behind a reverse proxy (nginx/Caddy/Traefik) or SSH tunnel, X-Forwarded-For
        # carries the real origin IP — read it first before falling back to the raw socket addr.
        # HERMES_WEBUI_ONBOARDING_OPEN=1 lets operators on remote servers explicitly bypass
        # the check when they control network access themselves (e.g. firewall + VPN).
        if not _onboarding_gate_allows(handler):
            return bad(handler, "Onboarding setup is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.", 403)
        try:
            return j(handler, apply_onboarding_setup(body))
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    if parsed.path == "/api/onboarding/complete":
        # Marking onboarding complete flips the first-run wizard off (persists
        # onboarding_completed=True). Gate it on the same local-network check as
        # the other onboarding mutators so an unauthenticated public client on a
        # passwordless bind can't hide the first-run wizard. (#3765)
        if not _onboarding_gate_allows(handler):
            return bad(handler, "Onboarding is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.", 403)
        return j(handler, complete_onboarding())

    if parsed.path == "/api/onboarding/probe":
        # Probe a self-hosted provider endpoint (#1499).  Validates the
        # configured base URL is reachable + parses /models, returns the
        # model catalog so the wizard can populate its dropdown.
        # Read-only: no config.yaml or .env writes happen here.  Same local-
        # network gate as /api/onboarding/setup (also writing-adjacent in
        # spirit because it carries an api_key the user typed).
        if not _onboarding_gate_allows(handler):
            return bad(handler, "Onboarding probe is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.", 403)
        provider = str((body or {}).get("provider") or "").strip().lower()
        base_url = str((body or {}).get("base_url") or "")
        api_key = str((body or {}).get("api_key") or "").strip() or None
        try:
            return j(handler, probe_provider_endpoint(provider, base_url, api_key))
        except Exception as e:
            return bad(handler, f"probe failed: {e}", 500)

    # ── Session pin (POST) ──
    if parsed.path == "/api/session/pin":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(handler, "Subagent sessions are view-only and cannot be modified from WebUI", 400)
        try:
            s = get_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        pin_requested = bool(body.get("pinned", True))
        # TOCTOU guard (Opus stage-389): the count check and the pin write
        # must happen under the same lock, otherwise two parallel pin
        # requests can both pass `len(pinned_ids) >= 3` against the same
        # snapshot and both succeed, leaving the user with 4 pins. The check
        # must be careful not to nest `all_sessions()` (which acquires LOCK
        # internally) inside a `with LOCK:` block — that's a deadlock since
        # LOCK is a non-reentrant `threading.Lock`. We snapshot the
        # persisted index outside the lock, then re-check the in-memory
        # mutation set inside the lock and commit the pin atomically.
        if pin_requested and not getattr(s, "pinned", False):
            # Pre-snapshot from persisted index (acquires LOCK internally,
            # so must run outside our own LOCK acquire below).
            persisted_rows = [
                existing for existing in all_sessions()
                if _session_counts_toward_pin_quota(existing)
            ]
            with LOCK:
                # Final authoritative count: merge persisted pinned rows with the
                # in-memory SESSIONS snapshot. Count logical sidebar-visible pin
                # lineages rather than raw session rows so continuation siblings
                # in the same visible lineage do not consume extra pin quota.
                candidate_rows = list(persisted_rows)
                candidate_rows.extend(
                    existing.compact() for existing in SESSIONS.values()
                    if _session_counts_toward_pin_quota(existing)
                )
                target_row = s.compact()
                candidate_rows.append(target_row)
                pinned_lineage_ids = _visible_pinned_lineage_ids(candidate_rows)
                target_lineage = _session_row_lineage_root_id(
                    target_row,
                    {
                        str(_session_field(row, "session_id", "") or ""): row
                        for row in candidate_rows
                        if _session_field(row, "session_id", None)
                    },
                )
                pinned_lineage_ids.discard(target_lineage)
                pinned_sessions_limit = int(load_settings().get("pinned_sessions_limit", 3) or 3)
                if len(pinned_lineage_ids) >= pinned_sessions_limit:
                    return bad(handler, f"Up to {pinned_sessions_limit} sessions can be pinned. Unpin one before pinning another.", 400)
                # Mark in-memory pin state under LOCK so concurrent pin
                # requests see the increment immediately, even before
                # save() finishes flushing to disk.
                s.pinned = True
            with edit_session(body["session_id"], session=s) as s:
                s.pinned = True
        else:
            with edit_session(body["session_id"], session=s) as s:
                s.pinned = pin_requested
        publish_session_list_changed(
            "session_pin",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", body["session_id"]),
        )
        return j(handler, {"ok": True, "session": s.compact()})

    # ── Session archive (POST) ──
    if parsed.path == "/api/session/archive":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(handler, "Subagent sessions are view-only and cannot be archived from WebUI", 400)
        try:
            s = get_session(sid)
        except KeyError:
            cli_meta = _lookup_cli_session_metadata(sid)
            if not cli_meta:
                return bad(handler, "Session not found", 404)
            if cli_meta.get("read_only"):
                return bad(handler, "Read-only imported sessions cannot be archived from WebUI", 400)
            # Delegated subagent children (#5307) are view-only and owned by the
            # delegate runner — never materialize one into a writable WebUI
            # sidecar via the archive fallback (the 3rd of the shared
            # import_cli_session write paths).
            _arch_source_tag = (cli_meta.get("source_tag") or cli_meta.get("raw_source") or "").strip().lower()
            if _arch_source_tag == "subagent" or _is_subagent_child_session_id(sid):
                return bad(handler, "Subagent sessions cannot be archived from WebUI", 400)
            if _is_messaging_session_record(cli_meta):
                s = Session(
                    session_id=sid,
                    title=cli_meta.get("title") or title_from(get_cli_session_messages(sid), "CLI Session"),
                    workspace=get_last_workspace(),
                    messages=[],
                    model=cli_meta.get("model") or "unknown",
                    created_at=cli_meta.get("created_at"),
                    updated_at=cli_meta.get("updated_at"),
                )
                apply_cli_source_metadata(
                    s,
                    cli_meta,
                    is_cli_session=is_cli_session_row(cli_meta),
                )
                s.save(touch_updated_at=False)
            else:
                msgs = get_cli_session_messages(sid)
                if not msgs:
                    return bad(handler, "Session not found", 404)
                s = import_cli_session(
                    sid,
                    cli_meta.get("title") or title_from(msgs, "CLI Session"),
                    msgs,
                    cli_meta.get("model") or "unknown",
                    profile=cli_meta.get("profile"),
                    created_at=cli_meta.get("created_at"),
                    updated_at=cli_meta.get("updated_at"),
                    source_metadata=cli_meta,
                )
                apply_cli_source_metadata(
                    s,
                    cli_meta,
                    is_cli_session=is_cli_session_row(cli_meta),
                )
        with edit_session(sid, session=s, touch_updated_at=False) as s:
            s.archived = bool(body.get("archived", True))
        publish_session_list_changed(
            "session_archive",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", sid),
        )
        return j(handler, {"ok": True, "session": s.compact(), **_worktree_retained_payload(s)})

    # ── Session move to project (POST) ──
    if parsed.path == "/api/session/move":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        try:
            s = _get_or_materialize_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(handler, "Read-only imported sessions cannot be moved from WebUI", 403)
        # #1614: refuse moves into a project owned by another profile.
        target_pid = body.get("project_id") or None
        if target_pid:
            # Use the session's own profile for authorization, not the global
            # active profile. A session belongs to a specific profile set at
            # creation; projects from that profile should always be assignable,
            # regardless of which profile is "active" at the process level.
            # Matches the same principle as the profile chip fix — prefer
            # session-scoped state over global active profile. (#3325 follow-up)
            _session_profile = getattr(s, 'profile', None) or get_active_profile_name()
            target = next(
                (p for p in load_projects() if p["project_id"] == target_pid),
                None,
            )
            if not target:
                return bad(handler, "Project not found", 404)
            if not _profiles_match(target.get("profile"), _session_profile):
                return bad(handler, "Project not found", 404)
        # #3746: use the repository's bounded edit. The streaming thread holds
        # the same owner lock during checkpoint writes; contention becomes an
        # actionable HTTP 503 instead of outlasting the client's request timeout.
        try:
            with edit_session(
                body["session_id"],
                session=s,
                lock_timeout=5,
            ) as s:
                s.project_id = target_pid
        except SessionBusyError:
            return j(
                handler,
                {"error": "Session is busy (streaming). Please try again in a moment."},
                status=503,
            )
        publish_session_list_changed(
            "session_move",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", body["session_id"]),
        )
        return j(handler, {"ok": True, "session": s.compact()})

    # ── Project CRUD (POST) ──
    if parsed.path == "/api/projects/create":
        try:
            require(body, "name")
        except ValueError as e:
            return bad(handler, str(e))
        import re as _re

        name = body["name"].strip()[:128]
        if not name:
            return bad(handler, "name required")
        color = body.get("color")
        if color and not _re.match(r"^#[0-9a-fA-F]{3,8}$", color):
            return bad(handler, "Invalid color format")
        projects = load_projects()
        # #3331 follow-up (Codex+Opus gate): validate the optional client-supplied
        # `profile` before stamping it, mirroring /api/profile/switch — otherwise a
        # client could create a project tagged with an arbitrary/unknown profile,
        # producing hidden cross-profile rows that can't be managed normally.
        _requested_profile = str(body.get('profile') or "").strip()
        if _requested_profile and _requested_profile != "default":
            from api.profiles import _PROFILE_ID_RE
            if not _PROFILE_ID_RE.fullmatch(_requested_profile):
                return bad(handler, "invalid profile")
        proj = {
            "project_id": uuid.uuid4().hex[:12],
            "name": name,
            "color": color,
            "profile": _requested_profile or get_active_profile_name() or 'default',
            "created_at": time.time(),
        }
        projects.append(proj)
        save_projects(projects)
        return j(handler, {"ok": True, "project": proj})

    if parsed.path == "/api/projects/rename":
        try:
            require(body, "project_id", "name")
        except ValueError as e:
            return bad(handler, str(e))
        import re as _re

        projects = load_projects()
        proj = next(
            (p for p in projects if p["project_id"] == body["project_id"]), None
        )
        if not proj:
            return bad(handler, "Project not found", 404)
        # #1614: a project can only be renamed by the profile that owns it.
        active_profile = get_active_profile_name()
        if not _profiles_match(proj.get("profile"), active_profile):
            return bad(handler, "Project not found", 404)
        proj["name"] = body["name"].strip()[:128]
        if "color" in body:
            color = body["color"]
            if color and not _re.match(r"^#[0-9a-fA-F]{3,8}$", color):
                return bad(handler, "Invalid color format")
            proj["color"] = color
        save_projects(projects)
        return j(handler, {"ok": True, "project": proj})

    if parsed.path == "/api/projects/delete":
        try:
            require(body, "project_id")
        except ValueError as e:
            return bad(handler, str(e))
        projects = load_projects()
        proj = next(
            (p for p in projects if p["project_id"] == body["project_id"]), None
        )
        if not proj:
            return bad(handler, "Project not found", 404)
        # #1614: a project can only be deleted by the profile that owns it.
        active_profile = get_active_profile_name()
        if not _profiles_match(proj.get("profile"), active_profile):
            return bad(handler, "Project not found", 404)
        projects = [p for p in projects if p["project_id"] != body["project_id"]]
        save_projects(projects)
        # Unassign all sessions that belonged to this project.
        # #3746: this loop is O(N) full-JSON read+save per session, and each
        # save() reserializes the entire messages array. For a project with many
        # messageful sessions that throughput alone can blow past the client's
        # 30s timeout. For an actively-streaming session we must NOT issue our own
        # s.save() — it would race the streaming thread's atomic writer and it
        # carries the largest in-memory message array. Instead we clear project_id
        # on the live cached Session object (under LOCK); the streaming thread owns
        # that object and persists it on its next checkpoint/final save (the worker
        # always does a final s.save() at turn completion), so the unlink still
        # lands without a competing write. (If the streaming session isn't in the
        # cache for some reason, fall back to a direct save.) Guard each per-session
        # update so one slow/failing session can't abort the whole request.
        if SESSION_INDEX_FILE.exists():
            try:
                index = json.loads(SESSION_INDEX_FILE.read_bytes())
                active_ids = _active_stream_ids()
                deferred_to_stream = []
                for entry in index:
                    if entry.get("project_id") != body["project_id"]:
                        continue
                    sid = entry.get("session_id")
                    try:
                        if entry.get("active_stream_id") in active_ids:
                            # Clear on the live cached object so the streaming
                            # thread's own next save persists project_id=None.
                            cleared_in_cache = False
                            with LOCK:
                                cached = SESSIONS.get(sid)
                                if cached is not None:
                                    cached.project_id = None
                                    cleared_in_cache = True
                            if cleared_in_cache:
                                deferred_to_stream.append(sid)
                                continue
                            # Not cached — fall through to a direct save.
                        s = get_session(sid)
                        s.project_id = None
                        s.save()
                    except Exception:
                        logger.debug("Failed to update session %s", sid)
                if deferred_to_stream:
                    logger.info(
                        "projects/delete: cleared project_id on %d streaming session(s) "
                        "in-cache; streaming thread will persist: %s",
                        len(deferred_to_stream), deferred_to_stream,
                    )
            except Exception:
                logger.debug("Failed to load session index for project unlink")
        return j(handler, {"ok": True})

    # ── Session import from JSON (POST) ──
    if parsed.path == "/api/session/import":
        return _handle_session_import(handler, body)

    # ── Self-update (POST) ──
    if parsed.path == "/api/updates/apply":
        target = body.get("target", "")
        if target not in ("webui", "agent"):
            return bad(handler, 'target must be "webui" or "agent"')
        # Honor an explicit validated body channel (the client sends the channel
        # the banner was offering) so a channel switch whose debounced autosave
        # hasn't landed can't make apply read the OLD saved channel (Codex gate).
        # Fall back to the saved setting when absent/invalid.
        _apply_channel = body.get("channel") if isinstance(body, dict) else None
        if _apply_channel not in ("stable", "experimental"):
            _apply_channel = None
        from api.updates import apply_update

        return j(handler, apply_update(target, _apply_channel))

    if parsed.path == "/api/updates/force":
        target = body.get("target", "")
        if target not in ("webui", "agent"):
            return bad(handler, 'target must be "webui" or "agent"')
        _force_channel = body.get("channel") if isinstance(body, dict) else None
        if _force_channel not in ("stable", "experimental"):
            _force_channel = None
        from api.updates import apply_force_update

        return j(handler, apply_force_update(target, _force_channel))

    if parsed.path == "/api/updates/clear_lock":
        # Manual-instruction recovery for the .git/index.lock case. The
        # endpoint NEVER removes a lock file from the server -- it returns
        # the diagnostic + the exact 'rm' command for the operator, and on
        # a re-click with the lock already gone, it re-runs the normal
        # non-destructive apply path. See apply_clear_lock for the v2.2
        # design rationale (round-2 gate cert: fcntl-flock cannot detect
        # git's O_CREAT|O_EXCL locks, so any auto-delete path races).
        target = body.get("target", "")
        if target not in ("webui", "agent"):
            return bad(handler, 'target must be "webui" or "agent"')
        from api.updates import apply_clear_lock

        return j(handler, apply_clear_lock(target))

    if parsed.path == "/api/updates/summary":
        from api.updates import summarize_update_payload

        updates = body.get("updates") if isinstance(body, dict) else {}
        target = body.get("target") if isinstance(body, dict) else None

        def _llm_update_summary(system_prompt: str, user_prompt: str) -> str:
            from api import profiles as profiles_api

            active_profile = profiles_api.get_active_profile_name() or "default"

            with profiles_api.profile_env_for_background_worker(
                active_profile,
                "update summary",
                logger_override=logger,
            ):
                from api.config import (
                    get_effective_default_model,
                    resolve_model_provider,
                    resolve_custom_provider_connection,
                )

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]

                _main_model, _main_provider, _main_base_url = resolve_model_provider(get_effective_default_model())
                _main_api_key = None
                try:
                    from api.auth import resolve_runtime_provider_with_anthropic_env_lock
                    from hermes_cli.runtime_provider import resolve_runtime_provider

                    _rt = resolve_runtime_provider_with_anthropic_env_lock(
                        resolve_runtime_provider,
                        requested=_main_provider,
                    )
                    _main_api_key = _rt.get("api_key")
                    if not _main_provider:
                        _main_provider = _rt.get("provider")
                    if not _main_base_url:
                        _main_base_url = _rt.get("base_url")
                except Exception as _e:
                    logger.debug("update summary runtime provider resolution failed: %s", _e)
                if isinstance(_main_provider, str) and _main_provider.startswith("custom:"):
                    _cp_key, _cp_base = resolve_custom_provider_connection(_main_provider)
                    if not _main_api_key and _cp_key:
                        _main_api_key = _cp_key
                    if not _main_base_url and _cp_base:
                        _main_base_url = _cp_base

                main_runtime = {
                    "provider": _main_provider,
                    "model": _main_model,
                    "base_url": _main_base_url,
                    "api_key": _main_api_key,
                }

                ensure_agent_runtime_current()
                try:
                    from agent.auxiliary_client import get_text_auxiliary_client

                    # Update summaries are a short text-compression/summarization task.
                    # Reuse the documented auxiliary.compression slot instead of
                    # inventing a WebUI-only auxiliary task name that users cannot
                    # discover in the Hermes Agent setup/config UI.
                    aux_client, aux_model = get_text_auxiliary_client(
                        "compression",
                        main_runtime=main_runtime,
                    )
                    if aux_client is not None and aux_model:
                        response = aux_client.chat.completions.create(
                            model=aux_model,
                            messages=messages,
                        )
                        return str(response.choices[0].message.content or "").strip()
                except Exception as _e:
                    logger.debug("update summary auxiliary model failed; falling back to main model: %s", _e)

                AIAgent = require_ai_agent_class()

                agent = AIAgent(
                    model=_main_model,
                    provider=_main_provider,
                    base_url=_main_base_url,
                    api_key=_main_api_key,
                    platform="webui",
                    quiet_mode=True,
                    enabled_toolsets=[],
                    session_id=f"updates-summary-{uuid.uuid4().hex[:8]}",
                )
                result = agent.run_conversation(
                    user_message=user_prompt,
                    system_message=system_prompt,
                    conversation_history=[],
                    task_id=f"updates-summary-{uuid.uuid4().hex[:8]}",
                )
                return str(result.get("final_response") or "").strip()

        return j(handler, summarize_update_payload(updates, llm_callback=_llm_update_summary, target=target))

    # ── CLI session import (POST) ──
    if parsed.path == "/api/session/import_cli":
        return _handle_session_import_cli(handler, body)

    # ── Auth endpoints (POST) ──
    if parsed.path == "/api/auth/login":
        from api.auth import (
            verify_password,
            create_session,
            set_auth_cookie,
            is_auth_enabled,
        )
        from api.auth import _check_login_rate, _record_login_attempt, _clear_login_attempts

        if not is_auth_enabled():
            return j(handler, {"ok": True, "message": "Auth not enabled"})
        client_ip = handler.client_address[0]
        if not _check_login_rate(client_ip):
            return j(
                handler,
                {"error": "Too many attempts. Try again in a minute."},
                status=429,
            )
        password = body.get("password", "")
        if not verify_password(password):
            _record_login_attempt(client_ip)
            return bad(handler, "Invalid password", 401)
        _clear_login_attempts(client_ip)
        cookie_val = create_session()
        body = json.dumps({"ok": True}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        set_auth_cookie(handler, cookie_val)
        handler.end_headers()
        handler.wfile.write(body)
        return True

    if parsed.path == "/api/auth/passkey/options":
        from api.auth import _passkey_feature_flag_enabled, is_auth_enabled
        from api.auth import PasskeyError, PasskeyRateLimitError, authentication_options

        if not _passkey_feature_flag_enabled():
            return j(handler, {"error": "Passkey support is disabled. Set HERMES_WEBUI_PASSKEY=1 or webui_passkey_enabled: true to enable."}, status=404)
        if not is_auth_enabled():
            return j(handler, {"error": "Auth not enabled"}, status=400)
        try:
            return j(handler, {"ok": True, "publicKey": authentication_options(handler)})
        except PasskeyRateLimitError as e:
            return bad(handler, str(e), status=429)
        except PasskeyError as e:
            return bad(handler, str(e), status=400)

    if parsed.path == "/api/auth/passkey/login":
        from api.auth import _passkey_feature_flag_enabled, create_session, is_auth_enabled, set_auth_cookie
        from api.auth import _check_login_rate, _record_login_attempt
        from api.auth import PasskeyError, finish_login

        if not _passkey_feature_flag_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        if not is_auth_enabled():
            return j(handler, {"error": "Auth not enabled"}, status=400)
        client_ip = handler.client_address[0]
        if not _check_login_rate(client_ip):
            return j(handler, {"error": "Too many attempts. Try again in a minute."}, status=429)
        try:
            finish_login(body, handler)
        except PasskeyError as e:
            _record_login_attempt(client_ip)
            return bad(handler, str(e), status=401)
        cookie_val = create_session()
        body = json.dumps({"ok": True}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        set_auth_cookie(handler, cookie_val)
        handler.end_headers()
        handler.wfile.write(body)
        return True

    if parsed.path == "/api/auth/passkey/register/options":
        from api.auth import _passkey_feature_flag_enabled
        from api.auth import PasskeyError, PasskeyRateLimitError, registration_options

        if not _passkey_feature_flag_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        ok, error, status = _require_passkey_registration_auth(handler)
        if not ok:
            return j(handler, {"error": error}, status=status)
        try:
            return j(handler, {"ok": True, "publicKey": registration_options(handler)})
        except PasskeyRateLimitError as e:
            return bad(handler, str(e), status=429)
        except PasskeyError as e:
            return bad(handler, str(e), status=400)

    if parsed.path == "/api/auth/passkey/register":
        from api.auth import _passkey_feature_flag_enabled
        from api.auth import PasskeyError, finish_registration, registered_credentials

        if not _passkey_feature_flag_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        ok, error, status = _require_passkey_registration_auth(handler)
        if not ok:
            return j(handler, {"error": error}, status=status)
        try:
            result = finish_registration(body, handler)
            result["credentials"] = registered_credentials()
            return j(handler, result)
        except PasskeyError as e:
            return bad(handler, str(e), status=400)

    if parsed.path == "/api/auth/passkey/delete":
        from api.auth import _passkey_feature_flag_enabled, get_password_hash
        from api.auth import PasskeyError, delete_credential, registered_credentials

        if not _passkey_feature_flag_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        try:
            credential_id = str(body.get("id") or "")
            creds = registered_credentials()
            if get_password_hash() is None and len(creds) <= 1 and any(c.get("id") == credential_id for c in creds):
                return bad(handler, "Set a password or disable auth before removing the last passkey.", 409)
            return j(handler, delete_credential(credential_id))
        except PasskeyError as e:
            return bad(handler, str(e), status=404)

    if parsed.path == "/api/auth/passkeys":
        from api.auth import _passkey_feature_flag_enabled
        from api.auth import registered_credentials

        if not _passkey_feature_flag_enabled():
            return j(handler, {"credentials": [], "disabled": True})
        return j(handler, {"credentials": registered_credentials()})

    if parsed.path == "/api/auth/logout":
        from api.auth import clear_auth_cookie, ensure_trusted_auth_session, get_trusted_auth_logout_url, invalidate_session, parse_cookie
        from api.helpers import clear_profile_cookie

        session_info = ensure_trusted_auth_session(handler)
        cookie_val = getattr(handler, '_trusted_auth_session_cookie_value', None) or parse_cookie(handler)
        if cookie_val:
            invalidate_session(cookie_val)
        payload = {"ok": True}
        if session_info and session_info.get("auth_type") == "trusted":
            logout_url = get_trusted_auth_logout_url()
            if logout_url:
                payload["trusted_logout_url"] = logout_url
        body = json.dumps(payload).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        clear_auth_cookie(handler)
        clear_profile_cookie(handler)
        handler.end_headers()
        handler.wfile.write(body)
        return True

    # ── Checkpoints / Rollback (POST) ──
    if parsed.path == "/api/rollback/restore":
        if not body:
            return bad(handler, "request body is required")
        workspace = body.get("workspace", "")
        checkpoint = body.get("checkpoint", "")
        if not workspace or not checkpoint:
            return bad(handler, "workspace and checkpoint are required")
        try:
            from api.rollback import restore_checkpoint
            return j(handler, restore_checkpoint(workspace, checkpoint))
        except ValueError as e:
            return bad(handler, str(e))
        except Exception as e:
            logger.exception("rollback/restore failed")
            return bad(handler, str(e), status=500)

    return False  # 404


def handle_patch(handler, parsed) -> bool:
    """Handle all PATCH routes. Returns True if handled, False for 404."""
    if not _check_csrf(handler):
        return j(handler, {"error": _csrf_rejection_error(handler)}, status=403)
    proxy_result = _handle_extension_sidecar_proxy(
        handler,
        parsed,
        "PATCH",
        read_request_body=True,
    )
    if proxy_result is not False:
        return proxy_result
    body = read_body(handler)
    if not _guard_request_session_visibility(handler, parsed, body=body, method="PATCH"):
        return True
    if parsed.path.startswith("/api/mcp/servers/"):
        name = parsed.path[len("/api/mcp/servers/"):]
        return _handle_mcp_server_toggle(handler, name, body)
    if parsed.path.startswith("/api/kanban/"):
        from api.kanban_bridge import handle_kanban_patch

        result = handle_kanban_patch(handler, parsed, body)
        if result is False:
            return _kanban_unknown_endpoint(handler, parsed, "PATCH")
        return True
    return False


def handle_delete(handler, parsed) -> bool:
    """Handle all DELETE routes. Returns True if handled, False for 404."""
    if not _check_csrf(handler):
        return j(handler, {"error": _csrf_rejection_error(handler)}, status=403)
    proxy_result = _handle_extension_sidecar_proxy(
        handler,
        parsed,
        "DELETE",
        read_request_body=True,
    )
    if proxy_result is not False:
        return proxy_result
    body = read_body(handler)
    if not _guard_request_session_visibility(handler, parsed, body=body, method="DELETE"):
        return True
    if parsed.path.startswith("/api/mcp/servers/"):
        name = parsed.path[len("/api/mcp/servers/"):]
        return _handle_mcp_server_delete(handler, name)
    if parsed.path == "/api/prompts":
        pid = str(body.get("id") or "").strip()
        if not pid:
            return bad(handler, "id is required")
        prompts = [p for p in _load_saved_prompts() if p.get("id") != pid]
        _save_saved_prompts(prompts)
        return j(handler, {"ok": True})

    if parsed.path.startswith("/api/kanban/"):
        from api.kanban_bridge import handle_kanban_delete

        result = handle_kanban_delete(handler, parsed, body)
        if result is False:
            return _kanban_unknown_endpoint(handler, parsed, "DELETE")
        return True
    return False


def handle_put(handler, parsed) -> bool:
    """Handle all PUT routes. Returns True if handled, False for 404."""
    if not _check_csrf(handler):
        return j(handler, {"error": "Cross-origin request rejected"}, status=403)
    proxy_result = _handle_extension_sidecar_proxy(
        handler,
        parsed,
        "PUT",
        read_request_body=True,
    )
    if proxy_result is not False:
        return proxy_result
    body = read_body(handler)
    if not _guard_request_session_visibility(handler, parsed, body=body, method="PUT"):
        return True
    if parsed.path.startswith("/api/mcp/servers/"):
        name = parsed.path[len("/api/mcp/servers/"):]
        return _handle_mcp_server_update(handler, name, body)
    return False

# ── GET route helpers ─────────────────────────────────────────────────────────

# MIME types for static file serving. Hoisted to module scope to avoid
# rebuilding the dict on every request.
_STATIC_MIME = {
    "css": "text/css",
    "js": "application/javascript",
    "html": "text/html",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "ico": "image/x-icon",
    "gif": "image/gif",
    "webp": "image/webp",
    "woff": "font/woff",
    "woff2": "font/woff2",
}
# MIME types that are text-based and should carry charset=utf-8
_TEXT_MIME_TYPES = {"text/css", "application/javascript", "text/html", "image/svg+xml", "text/plain"}

# MIME types worth gzipping. Image and font formats (png/jpg/webp/woff2) are
# already compressed; gzip would only add CPU and a few bytes of framing.
_COMPRESSIBLE_MIME = {
    "text/css", "application/javascript", "text/html", "image/svg+xml",
    "application/json", "text/plain",
}

# In-process cache for raw bytes, compressed bytes, and ETag. The cache is keyed
# by absolute path and invalidated on (size, high-precision mtime) change, so a
# redeploy is picked up without a process restart. Missing/random paths never
# enter the cache; memory cost is bounded by the static/ tree's served files.
_STATIC_CACHE: dict = {}
_STATIC_CACHE_LOCK = threading.Lock()


def _serve_static(handler, parsed):
    static_root = api_config.get_static_root().resolve()
    # Strip the leading '/static/' prefix, then resolve and sandbox
    rel = parsed.path[len("/static/") :]
    static_file = (static_root / rel).resolve()
    try:
        static_file.relative_to(static_root)
    except ValueError:
        return j(handler, {"error": "not found"}, status=404)
    if not static_file.exists() or not static_file.is_file():
        return j(handler, {"error": "not found"}, status=404)
    ext = static_file.suffix.lower()
    ct = _STATIC_MIME.get(ext.lstrip("."), "text/plain")
    ct_header = f"{ct}; charset=utf-8" if ct in _TEXT_MIME_TYPES else ct

    # Look up or populate the per-file cache (raw, optional gzip, ETag).
    # Keyed by absolute path; invalidated by (size, nanosecond mtime).
    st = static_file.stat()
    sig = (st.st_size, st.st_mtime_ns)
    cache_key = str(static_file)
    raw = gz = etag = None
    with _STATIC_CACHE_LOCK:
        cached = _STATIC_CACHE.get(cache_key)
        if cached and cached[0] == sig:
            _, raw, gz, etag = cached
    if raw is None:
        raw = static_file.read_bytes()
        # Weak ETag: equality semantics, derived from filesystem identity.
        etag = f'W/"{sig[0]:x}-{sig[1]:x}"'
        gz = (gzip.compress(raw, compresslevel=6)
              if ct in _COMPRESSIBLE_MIME and len(raw) > 1024
              else None)
        with _STATIC_CACHE_LOCK:
            _STATIC_CACHE[cache_key] = (sig, raw, gz, etag)

    # The page template substitutes __WEBUI_VERSION__ at request time (see the
    # `/`/`/index.html`/`/session/` branch above), and static/sw.js's
    # SHELL_ASSETS list relies on the same convention. So a fingerprinted URL
    # is safe to cache aggressively: any redeploy changes the URL.
    version_values = parse_qs(parsed.query, keep_blank_values=True).get("v", [""])
    has_fingerprint = bool(version_values[0])
    cache_control = (
        "public, max-age=31536000, immutable" if has_fingerprint
        else "public, max-age=300"
    )

    # 304 short-circuit on conditional GET.
    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", cache_control)
        if gz is not None:
            handler.send_header("Vary", "Accept-Encoding")
        handler.end_headers()
        return True

    accept_enc = (handler.headers.get("Accept-Encoding") or "").lower()
    use_gzip = gz is not None and "gzip" in accept_enc
    body = gz if use_gzip else raw

    handler.send_response(200)
    handler.send_header("Content-Type", ct_header)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("ETag", etag)
    handler.send_header("Cache-Control", cache_control)
    if gz is not None:
        handler.send_header("Vary", "Accept-Encoding")
    if use_gzip:
        handler.send_header("Content-Encoding", "gzip")
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _handle_session_export(handler, parsed):
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    active_profile = get_active_profile_name()
    if not _profiles_match(getattr(s, "profile", None), active_profile):
        return bad(handler, "Session not found", 404)
    safe = redact_session_data(s.__dict__)
    qs = parse_qs(parsed.query)
    fmt = qs.get("format", ["json"])[0].lower()
    if fmt == "html":
        from api.sessions.export import render_session_html
        theme = qs.get("theme", ["dark"])[0].lower()
        palette: dict | None = None
        raw_palette = qs.get("palette", [""])[0]
        if raw_palette:
            try:
                import base64 as _b64
                decoded = _b64.b64decode(raw_palette, validate=False).decode("utf-8")
                parsed_palette = json.loads(decoded)
                if isinstance(parsed_palette, dict):
                    # Cap payload so a hostile client can't blow up the response.
                    if len(parsed_palette) <= 64:
                        palette = parsed_palette
            except Exception:
                palette = None
        payload = render_session_html(safe, theme=theme, palette=palette)
        content_type = "text/html; charset=utf-8"
        ext = "html"
    else:
        payload = json.dumps(safe, ensure_ascii=False, indent=2)
        content_type = "application/json; charset=utf-8"
        ext = "json"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header(
        "Content-Disposition", f'attachment; filename="hermes-{sid}.{ext}"'
    )
    handler.send_header("Content-Length", str(len(payload.encode("utf-8"))))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(payload.encode("utf-8"))
    return True


def _session_search_message_text(message):
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def _session_search_preview(text, query, max_len=124):
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not normalized or not q:
        return ""
    idx = normalized.lower().find(q.lower())
    if idx < 0:
        return ""

    max_len = max(32, int(max_len or 124))
    if len(normalized) <= max_len:
        return normalized

    context = max(12, (max_len - len(q)) // 2)
    start = max(0, idx - context)
    end = min(len(normalized), idx + len(q) + context)
    if start > 0:
        while start < idx and normalized[start] != " ":
            start += 1
        if start >= idx:
            start = max(0, idx - context)
    if end < len(normalized):
        while end > idx + len(q) and normalized[end - 1] != " ":
            end -= 1
        if end <= idx + len(q):
            end = min(len(normalized), idx + len(q) + context)
    excerpt = normalized[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(normalized):
        excerpt = excerpt + "..."
    return excerpt


def _handle_sessions_search(handler, parsed):
    qs = parse_qs(parsed.query)
    q = qs.get("q", [""])[0].lower().strip()
    content_search = qs.get("content", ["1"])[0] == "1"
    from api.profiles import get_active_profile_name
    active_profile = get_active_profile_name()
    all_profiles = _all_profiles_enabled(parsed)
    sessions = all_sessions()
    if not all_profiles:
        sessions = [
            s for s in sessions
            if _profiles_match(s.get("profile"), active_profile)
        ]
    # Reject a malformed depth instead of letting int() raise ValueError and
    # surface as a confusing 500. Clamp to >= 0 so a negative value can't reach
    # the messages[:depth] slice below — messages[:-n] would silently exclude
    # the most recent messages from the content search instead of capping it.
    # (depth == 0 keeps its existing meaning: search the full transcript.)
    try:
        depth = max(0, int(qs.get("depth", ["5"])[0]))
    except (ValueError, TypeError):
        depth = 5
    # Read the redaction setting ONCE for the whole response (mirrors the
    # /api/sessions read-once optimization, #4662) and thread it through every
    # branch + the shared title-field redactor so search rows redact the same
    # fields as the sidebar list.
    try:
        _search_redact_enabled = bool(load_settings().get("api_redact_enabled", True))
    except Exception:
        _search_redact_enabled = True  # fail safe: redact when settings unreadable
    if not q:
        safe_sessions = []
        for s in sessions:
            item = dict(s)
            if isinstance(item.get("title"), str):
                item["title"] = _redact_text(item["title"], _enabled=_search_redact_enabled)
            _redact_sidebar_title_fields(item, _search_redact_enabled)
            safe_sessions.append(item)
        return j(handler, {
            "sessions": safe_sessions,
            "all_profiles": all_profiles,
            "active_profile": active_profile,
        })
    results = []
    for s in sessions:
        title_match = q in (s.get("title") or "").lower()
        if title_match:
            item = dict(s, match_type="title")
            if isinstance(item.get("title"), str):
                item["title"] = _redact_text(item["title"], _enabled=_search_redact_enabled)
            _redact_sidebar_title_fields(item, _search_redact_enabled)
            results.append(item)
            continue
        if content_search:
            try:
                sess = get_session(s["session_id"])
                msgs = sess.messages[:depth] if depth else sess.messages
                for m in msgs:
                    c = _session_search_message_text(m)
                    if q in str(c).lower():
                        item = dict(s, match_type="content")
                        preview = _session_search_preview(c, q)
                        if preview:
                            item["match_preview"] = _redact_text(preview, _enabled=_search_redact_enabled)
                        if isinstance(item.get("title"), str):
                            item["title"] = _redact_text(item["title"], _enabled=_search_redact_enabled)
                        _redact_sidebar_title_fields(item, _search_redact_enabled)
                        results.append(item)
                        break
            except (KeyError, Exception):
                pass
    return j(handler, {
        "sessions": results,
        "query": q,
        "count": len(results),
        "all_profiles": all_profiles,
        "active_profile": active_profile,
    })


def _handle_list_dir(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session(sid)
        workspace = s.workspace
    except KeyError:
        # Fallback for CLI sessions not loaded in WebUI memory
        try:
            cli_meta = None
            for cs in get_cli_sessions():
                if cs["session_id"] == sid:
                    cli_meta = cs
                    break
            if not cli_meta:
                return bad(handler, "Session not found", 404)
            workspace = cli_meta.get("workspace", "")
        except Exception:
            return bad(handler, "Session not found", 404)
    try:
        rel_path = qs.get("path", ["."])[0]
        entries = list_dir(Path(workspace), rel_path)
        return j(
            handler,
            {
                "entries": entries,
                "signature": dir_signature(Path(workspace), rel_path, entries),
                "path": rel_path,
            },
        )
    except (FileNotFoundError, ValueError) as e:
        return bad(handler, _sanitize_error(e), 404)


def _read_json_request_body(handler, *, max_bytes: int = 4096) -> dict:
    try:
        length = _safe_content_length(handler, max_bytes)
    except (ValueError, OverflowError) as exc:
        raise ValueError(_sanitize_error(exc)) from exc
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid JSON body") from exc
    return payload if isinstance(payload, dict) else {}


def _handle_escape_authorize(handler, parsed, body: dict | None = None):
    if handler.command != "POST":
        return bad(handler, "method not allowed", 405)
    if not handler.headers.get("Origin"):
        return bad(handler, "browser origin required", 403)
    if not _check_csrf(handler):
        return bad(handler, _csrf_rejection_error(handler), 403)
    if body is None:
        try:
            body = _read_json_request_body(handler)
        except ValueError as exc:
            return bad(handler, _sanitize_error(exc), 400)
    qs = parse_qs(parsed.query)
    sid = str(body.get("session_id") or qs.get("session_id", [""])[0] or "").strip()
    rel = str(body.get("path") or qs.get("path", [""])[0] or "").strip()
    token = str(body.get("token") or qs.get("token", [""])[0] or "").strip()
    if token:
        return bad(handler, "token must not be provided", 400)
    if not sid:
        return bad(handler, "session_id is required")
    if not rel:
        return bad(handler, "path is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        payload = authorize_escape_target(Path(s.workspace), sid, rel)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    return j(handler, payload)


def _handle_escape_list_dir(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel_path = qs.get("path", ["."])[0]
    try:
        payload = list_authorized_escape_dir(Path(s.workspace), sid, token, rel_path)
        return j(handler, payload)
    except FileNotFoundError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)


def _handle_escape_file_read(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    try:
        return j(handler, read_authorized_escape_file_content(Path(s.workspace), sid, token, rel))
    except FileNotFoundError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ImportError as exc:
        # Optional Office parsers absent on a lean install — mirror
        # _handle_file_read: a 503 with the install hint, not a 500 traceback.
        return bad(handler, _sanitize_error(exc), 503)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)


def _handle_escape_file_raw(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    force_download = qs.get("download", [""])[0] == "1"
    try:
        anchor_root, target = raw_authorized_escape_target(Path(s.workspace), sid, token, rel)
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    if not target.exists() or not target.is_file():
        return j(handler, {"error": "not found"}, status=404)
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, "application/octet-stream")
    inline_preview = qs.get("inline", [""])[0] == "1"
    dangerous_types = {"text/html", "application/xhtml+xml", "image/svg+xml"}
    html_inline_ok = inline_preview and mime == "text/html"
    disposition = "attachment" if force_download or (mime in dangerous_types and not html_inline_ok) else "inline"
    sandbox_csp = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
    # Content-Security-Policy sandboxing is carried through the csp=sandbox_csp handoff below.
    csp = sandbox_csp if (inline_preview and not force_download and disposition == "inline") else None
    if html_inline_ok:
        return _serve_inline_html_preview(handler, target, "no-store", csp=sandbox_csp, anchor_root=anchor_root)
    return _serve_file_bytes(handler, target, mime, disposition, "no-store", csp=csp, anchor_root=anchor_root)


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




def _handle_approval_pending(handler, parsed):
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    with _lock:
        _head, _total, _changed = reconcile_gateway_pending_mirror_locked(sid)
        queue = _pending.get(sid)
        # Support both the new list format and a legacy single-dict value.
        if isinstance(queue, list):
            p = queue[0] if queue else None
            total = len(queue)
        elif queue:
            p = queue
            total = 1
        else:
            p = None
            total = 0
        if p is None:
            gw_queue = _gateway_queues.get(sid) or []
            if gw_queue:
                raw = getattr(gw_queue[0], "data", None) or {}
                if raw:
                    p = raw
                    total = len(gw_queue)
                else:
                    logger.warning("Gateway queue entry for %s has no .data attribute", sid)
    if p:
        return j(handler, {"pending": dict(p), "pending_count": total})
    return j(handler, {"pending": None, "pending_count": 0})


def _handle_approval_sse_stream(handler, parsed):
    """SSE endpoint for real-time approval notifications.

    Long-lived connection that pushes approval events the moment they arrive,
    replacing the 1.5s polling loop.  The frontend uses EventSource and falls
    back to HTTP polling if the connection fails.
    """
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")

    # Subscribe AND snapshot atomically under a single _lock acquisition so a
    # submit_pending() that fires between the two cannot be lost. If we
    # snapshot first then subscribe (the naive ordering), an approval that
    # arrives in the gap is appended to _pending (after our snapshot) AND
    # notified to subscribers (before we joined) — leaving the client unaware
    # until the next event arrives.
    q = queue.Queue(maxsize=16)
    initial_pending = None
    initial_count = 0
    with _lock:
        _approval_sse_subscribers.setdefault(sid, []).append(q)
        reconcile_gateway_pending_mirror_locked(sid)
        q_list = _pending.get(sid)
        if isinstance(q_list, list):
            initial_pending = dict(q_list[0]) if q_list else None
            initial_count = len(q_list)
        elif q_list:
            initial_pending = dict(q_list)
            initial_count = 1

    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'close')
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

    from api.streaming import _sse

    # Push initial state immediately so the client doesn't miss anything.
    _sse(handler, 'initial', {"pending": initial_pending, "pending_count": initial_count})

    try:
        while True:
            try:
                payload = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                # Keepalive — SSE comment line prevents proxy/CDN timeout.
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            if payload is None:
                break  # signal to close
            _sse(handler, 'approval', payload)
    except _CLIENT_DISCONNECT_ERRORS:
        pass  # client went away — normal for long-lived connections
    finally:
        _approval_sse_unsubscribe(sid, q)


def _handle_approval_inject(handler, parsed):
    """Inject a fake pending approval -- loopback-only, used by automated tests."""
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    key = qs.get("pattern_key", ["test_pattern"])[0]
    cmd = qs.get("command", ["rm -rf /tmp/test"])[0]
    if sid:
        submit_pending(
            sid,
            {
                "command": cmd,
                "pattern_key": key,
                "pattern_keys": [key],
                "description": "test pattern",
            },
        )
        return j(handler, {"ok": True, "session_id": sid})
    return j(handler, {"error": "session_id required"}, status=400)


def _handle_clarify_pending(handler, parsed):
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    pending = get_clarify_pending(sid)
    if pending:
        return j(handler, {"pending": pending})
    return j(handler, {"pending": None})


def _handle_clarify_sse_stream(handler, parsed):
    """SSE endpoint for real-time clarify notifications.

    Long-lived connection that pushes clarify events the moment they arrive,
    replacing the 1.5s polling loop.  The frontend uses EventSource and falls
    back to HTTP polling if the connection fails.
    """
    if clarify_sse_subscribe is None:
        return bad(handler, "clarify SSE not available")

    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")

    # Subscribe AND snapshot atomically.  We import clarify's _lock so that
    # subscribe and the snapshot read happen under the same mutex — same
    # pattern as the approval SSE handler.
    #
    # NOTE: We must NOT call clarify.get_pending() here — it acquires _lock
    # internally, which would deadlock since clarify._lock is a non-reentrant
    # threading.Lock.  Instead, read _gateway_queues / _pending inline under
    # the lock we already hold.
    from api.clarify import (
        _lock as _clarify_lock,
        _clarify_sse_subscribers as _clarify_subs,
        _gateway_queues as _clarify_gateway_queues,
        _pending as _clarify_pending,
    )
    q = queue.Queue(maxsize=16)
    initial_pending = None
    initial_count = 0
    with _clarify_lock:
        _clarify_subs.setdefault(sid, []).append(q)
        gw_q = _clarify_gateway_queues.get(sid) or []
        if gw_q:
            initial_pending = dict(gw_q[0].data)
            initial_count = len(gw_q)
        else:
            _legacy = _clarify_pending.get(sid)
            if _legacy:
                initial_pending = dict(_legacy)
                initial_count = 1

    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'close')
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

    from api.streaming import _sse

    # Push initial state immediately so the client doesn't miss anything.
    _sse(handler, 'initial', {"pending": initial_pending, "pending_count": initial_count})

    try:
        while True:
            try:
                payload = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            if payload is None:
                break
            _sse(handler, 'clarify', payload)
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    finally:
        clarify_sse_unsubscribe(sid, q)


def _handle_session_sse_stream(handler, parsed):
    """SSE endpoint for the persistent per-session channel (Option X).

    Subscribes to ``api.background_process.SESSION_CHANNELS[sid]`` — a channel
    that lives across agent turns (unlike STREAMS, which is torn down at
    end-of-turn). Used to deliver ``bg_task_complete`` events that fire while
    no agent turn is active.

    Lifecycle: opened by the frontend at session mount, closed at unmount or
    on tab close. Multiple tabs share one SessionChannel (refcounted via
    subscribe/unsubscribe). 30s SSE keepalive comments keep the proxy alive.
    Reaper-driven idle TTL (default 4h) prevents zombie channels.
    """
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")

    # The (re)subscribing tab reports its last-known message_count via
    # ?known_count=N so the on-subscribe self-heal can detect a server-initiated
    # turn that started AND finished entirely inside this tab's SSE gap (see the
    # "server-initiated turn finished during the gap" self-heal block below).
    # Absent/blank/non-numeric => None ("tab didn't report", never triggers).
    _known_count_raw = parse_qs(parsed.query).get("known_count", [""])[0]
    try:
        subscriber_known_count = int(_known_count_raw) if _known_count_raw != "" else None
    except (TypeError, ValueError):
        subscriber_known_count = None

    from api.background_process import (
        subscribe_to_session_channel,
        active_stream_id_for_session,
        persisted_message_count_for_session,
        should_emit_session_updated,
    )

    # Atomic get-or-create + subscribe under SESSION_CHANNELS_LOCK. Doing these
    # two steps separately (get_or_create_session_channel then ch.subscribe)
    # left a TOCTOU gap where the reaper — which also holds
    # SESSION_CHANNELS_LOCK and collects idle 0-subscriber channels in one
    # critical section — could collect the channel between the two calls,
    # orphaning this subscriber on a channel no longer in SESSION_CHANNELS.
    # bg_task_complete emits would then never reach this queue. See
    # subscribe_to_session_channel for the full rationale (PR #2971 Greptile P1).
    ch, q = subscribe_to_session_channel(sid, maxsize=64)

    # NOTE: ``subscribe_to_session_channel`` above acquires a subscriber slot
    # that MUST be released on every exit path. Header setup
    # (``send_response`` / ``send_header`` / ``end_headers`` /
    # ``_sse_set_write_deadline``) and the initial-frame + on-subscribe
    # recovery writes below all touch the socket and can raise a member of
    # ``_CLIENT_DISCONNECT_ERRORS`` (BrokenPipeError / ConnectionResetError) if
    # the client drops immediately after subscribing. If that happened outside
    # this try/finally the ``ch.unsubscribe(q)`` cleanup would be skipped,
    # permanently leaking a subscriber. Because
    # ``SessionChannel.reaper_should_collect()`` refuses to collect any channel
    # with ``sub_count > 0``, a single ghost subscriber blocks the reaper
    # forever and the channel zombies in SESSION_CHANNELS. So EVERYTHING from
    # the subscribe onward — header setup included — runs inside one
    # try/finally that unconditionally unsubscribes.
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('X-Accel-Buffering', 'no')
        # #3103: omit the Connection header — rely on the HTTP/1.1 keep-alive
        # default, matching the other long-lived SSE handlers (gateway/session
        # events) that fixed the reconnect-storm. An explicit value here is a
        # third, inconsistent approach (greptile flag).
        end_sse_headers(handler)
        _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

        from api.streaming import _sse

        # Push an initial frame so the client has confirmation the channel is
        # live (mirrors approval/clarify which send an 'initial' frame). No
        # snapshot data is needed — this channel only carries forward-looking
        # events, not pending state.
        _sse(handler, 'initial', {"session_id": sid})

        # ── Open-tab live-view self-heal (root cause: lost server_turn_started) ──
        # The `server_turn_started` fan-out (routes.start_session_turn) is a
        # fire-and-forget SessionChannel.emit with NO replay buffer: it reaches
        # only the subscribers connected at the exact emit instant. A tab whose
        # per-session EventSource was momentarily absent at that instant — a
        # transient SSE drop, a reverse-proxy idle-timeout, or browser
        # connection-pool starvation (all common behind a corporate proxy) —
        # misses the frame permanently, so a SERVER-initiated wakeup turn never
        # renders live and the user must hard-refresh (the reported defect). The
        # server-side wakeup itself ran and persisted fine; only the live-view
        # was lost. On (re)subscribe, if the session has a live run RIGHT NOW,
        # replay a synthetic `server_turn_started` to THIS new subscriber so the
        # open tab attaches its existing chat-stream renderer (attachLiveStream)
        # and self-heals with no refresh. `recovered: True` lets the frontend
        # use the replay (reconnecting) attach so the renderer picks up the
        # in-progress stream from the run journal rather than expecting token 0.
        # Idempotent: the frontend dedupes by (session_id, stream_id) — if the
        # original frame WAS delivered this is a harmless no-op there.
        try:
            recover_stream_id = active_stream_id_for_session(sid)
            if recover_stream_id:
                pending_started_at = None
                try:
                    recover_session = get_session(sid, metadata_only=True)
                    pending_started_at = getattr(recover_session, "pending_started_at", None)
                except Exception:
                    logger.debug(
                        "session-stream recovery could not read pending_started_at for %s",
                        sid,
                        exc_info=True,
                    )
                _sse(handler, 'server_turn_started', {
                    "session_id": sid,
                    "stream_id": recover_stream_id,
                    "pending_started_at": pending_started_at,
                    "source": "subscribe_recovery",
                    "recovered": True,
                })
            else:
                # ── Server-initiated turn that FINISHED during the SSE gap ──
                # The block above only heals a turn that is live RIGHT NOW. But
                # a server-initiated turn (self-wake / cron / restart hook) can
                # start AND finish entirely inside the gap: the fire-and-forget
                # `server_turn_started` reached no subscriber, and by the time
                # this tab reconnects the run has already cleared from
                # ACTIVE_RUNS — so active_stream_id_for_session returns None and
                # nothing above replays. The turn IS persisted, but this tab's
                # transcript stays stale until a hard refresh (the reported
                # visible-tab defect). Detect it by comparing the persisted
                # message_count against what this (re)subscribing tab last knew
                # (?known_count). If the server is AHEAD, emit a lightweight
                # `session-updated` frame so the tab does an INCREMENTAL,
                # swap-in-place message sync (frontend reuses #5189's
                # keepStaleUntilLoaded loadSession path — NO clear+refetch, so
                # the #5177/#5189 blank-gap jump is not reintroduced). Carries
                # only counts (no transcript) to stay cheap. Skipped
                # entirely when the tab didn't report a count or the persisted
                # count is unknown (legacy sidecar) → never a spurious reload.
                if subscriber_known_count is not None:
                    persisted_count = persisted_message_count_for_session(sid)
                    if should_emit_session_updated(subscriber_known_count, persisted_count):
                        _sse(handler, 'session-updated', {
                            "session_id": sid,
                            "message_count": persisted_count,
                            "known_count": subscriber_known_count,
                            "source": "subscribe_recovery",
                        })
        except _CLIENT_DISCONNECT_ERRORS:
            # Client vanished mid-recovery — re-raise so the outer handler
            # treats it as a normal disconnect and the finally still cleans up.
            raise
        except Exception:
            logger.debug(
                "session-stream on-subscribe recovery failed for %s", sid,
                exc_info=True,
            )

        while True:
            try:
                payload = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            if payload is None:
                break
            event_name, data = payload
            _sse(handler, event_name, data)
    except _CLIENT_DISCONNECT_ERRORS:
        pass  # client went away — normal for long-lived connections
    finally:
        ch.unsubscribe(q)


def _handle_clarify_inject(handler, parsed):
    """Inject a fake pending clarify prompt -- loopback-only, used by automated tests."""
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    question = qs.get("question", ["Which option?"])[0]
    choices = qs.get("choices", [])
    if sid:
        submit_clarify_pending(
            sid,
            {
                "question": question,
                "choices_offered": choices,
                "session_id": sid,
                "kind": "clarify",
            },
        )
        return j(handler, {"ok": True, "session_id": sid})
    return j(handler, {"error": "session_id required"}, status=400)


















_PROJECT_CONTEXT_HERMES_NAMES = (".hermes.md", "HERMES.md")
# Mirror the agent's lowercase filename variants (agents.md / claude.md) so the
# tab does not under-report on case-sensitive filesystems.
_PROJECT_CONTEXT_CWD_NAMES = (
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "claude.md",
    ".cursorrules",
)
# Modern Cursor rules live in a directory of .mdc files, which the agent also loads.
_PROJECT_CONTEXT_CURSOR_RULES_GLOB = ".cursor/rules/*.mdc"
_PROJECT_CONTEXT_MAX_BYTES = 20_000


def _strip_project_context_frontmatter(content: str) -> str:
    """Strip a leading YAML frontmatter block, mirroring the agent's loader.

    The agent removes a leading ``---\\n ... \\n---`` block before injecting a
    context file. Without this the tab would display frontmatter the agent never
    sends to the model.
    """
    if not content.startswith("---"):
        return content
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return "".join(lines[idx + 1:]).lstrip("\n")
    return content


def _project_context_git_root(start: Path) -> Path | None:
    """Return the nearest git root for project context discovery."""
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _project_context_candidates(workspace: Path) -> list[Path]:
    """Mirror the agent's first-match project context file priority.

    #4164: in a non-git workspace the agent's ``_find_hermes_md`` walks all
    the way up to filesystem root because its ``stop_at = git_root`` is
    ``None`` — so a workspace at ``/tmp/x/project/subdir`` could surface
    ``/tmp/x/HERMES.md`` (a file *outside* the user's workspace) in the
    Project Context tab.

    The WebUI tab is a read-only mirror of what the agent injects, so the
    safest bound that does not over-promise is: when there is no git root,
    treat the workspace itself as the stop boundary. The cwd is still
    scanned (preserving the in-workspace AGENTS.md / HERMES.md behavior),
    but we no longer walk into the user's home directory or ``/tmp``.

    The agent-side walk in ``agent/prompt_builder._find_hermes_md`` should
    be bounded the same way for full parity; until that ships the WebUI
    will under-report context files that live *above* a non-git workspace,
    which is strictly less surprising than over-reporting them.
    """
    cwd = workspace.resolve()
    candidates: list[Path] = []
    git_root = _project_context_git_root(cwd)
    # When inside a git tree, walk up to the git root as before. When not,
    # bound the walk at the workspace itself so we never surface files
    # above the user's workspace.
    stop_at = git_root if git_root is not None else cwd

    for directory in [cwd, *cwd.parents]:
        for name in _PROJECT_CONTEXT_HERMES_NAMES:
            candidates.append(directory / name)
        if directory == stop_at:
            break

    for name in _PROJECT_CONTEXT_CWD_NAMES:
        candidates.append(cwd / name)

    # Modern Cursor rules: the agent reads .cursor/rules/*.mdc in addition to
    # the legacy .cursorrules file. Sort for deterministic ordering.
    try:
        candidates.extend(sorted(cwd.glob(_PROJECT_CONTEXT_CURSOR_RULES_GLOB)))
    except OSError:
        pass

    return candidates


def _memory_project_context_workspace(parsed) -> Path | None:
    qs = parse_qs(parsed.query or "") if parsed is not None else {}
    sid = qs.get("session_id", [""])[0]
    if sid:
        try:
            # A blank session workspace (freshly-created/draft sessions) must not
            # fall through to Path("").resolve(), which returns the server's own
            # CWD and would surface the install's AGENTS.md/HERMES.md as if it
            # were the user's project context.
            ws = (get_session(sid).workspace or "").strip()
            if not ws:
                return None
            return Path(ws).expanduser().resolve()
        except Exception:
            return None

    raw_workspace = qs.get("workspace", [""])[0] or os.environ.get("TERMINAL_CWD", "") or get_last_workspace()
    if not raw_workspace:
        return None
    try:
        return Path(resolve_trusted_workspace(raw_workspace)).expanduser().resolve()
    except Exception:
        logger.debug("Skipping project context for untrusted workspace %s", raw_workspace, exc_info=True)
        return None


def _read_active_project_context(workspace: Path | None) -> dict:
    payload = {
        "content": "",
        "path": "",
        "mtime": None,
        "workspace": str(workspace) if workspace else "",
        "shadowed": [],
    }
    if not workspace:
        return payload
    try:
        if not workspace.exists() or not workspace.is_dir():
            return payload
    except OSError:
        return payload

    seen: set[str] = set()
    readable: list[dict] = []
    for candidate in _project_context_candidates(workspace):
        try:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            key = os.path.normcase(str(resolved)).casefold()
            if key in seen:
                continue
            seen.add(key)
            content = resolved.read_text(encoding="utf-8", errors="replace")
            # Mirror what the agent actually injects: strip YAML frontmatter and
            # cap each source, so the tab reports the effective context rather
            # than raw file bytes the agent never sends to the model.
            content = _strip_project_context_frontmatter(content)
            if len(content) > _PROJECT_CONTEXT_MAX_BYTES:
                content = content[:_PROJECT_CONTEXT_MAX_BYTES]
            if not content.strip():
                continue
            readable.append(
                {
                    "name": resolved.name,
                    "path": str(resolved),
                    "content": content,
                    "mtime": resolved.stat().st_mtime,
                }
            )
        except Exception:
            logger.debug("Could not read project context candidate %s", candidate, exc_info=True)

    if not readable:
        return payload

    active = readable[0]
    payload.update(
        {
            "content": active["content"],
            "path": active["path"],
            "mtime": active["mtime"],
            "name": active["name"],
            "shadowed": [
                {
                    "name": item["name"],
                    "path": item["path"],
                    "mtime": item["mtime"],
                    "shadowed_by": active["name"],
                    "shadowed_by_path": active["path"],
                }
                for item in readable[1:]
            ],
        }
    )
    return payload


def _handle_memory_read(handler, parsed=None):
    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        mem_dir = home / "memories"
    except ImportError:
        home = Path.home() / ".hermes"
        mem_dir = home / "memories"
    mem_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"
    soul_file = home / "SOUL.md"
    memory = (
        mem_file.read_text(encoding="utf-8", errors="replace")
        if mem_file.exists()
        else ""
    )
    user = (
        user_file.read_text(encoding="utf-8", errors="replace")
        if user_file.exists()
        else ""
    )
    soul = (
        soul_file.read_text(encoding="utf-8", errors="replace")
        if soul_file.exists()
        else ""
    )
    project_context = _read_active_project_context(_memory_project_context_workspace(parsed))
    return j(
        handler,
        {
            "memory": _redact_text(memory),
            "user": _redact_text(user),
            "soul": _redact_text(soul),
            "project_context": _redact_text(project_context["content"]),
            "memory_path": str(mem_file),
            "user_path": str(user_file),
            "soul_path": str(soul_file),
            "project_context_path": project_context["path"],
            "project_context_name": project_context.get("name", ""),
            "project_context_workspace": project_context["workspace"],
            "memory_mtime": mem_file.stat().st_mtime if mem_file.exists() else None,
            "user_mtime": user_file.stat().st_mtime if user_file.exists() else None,
            "soul_mtime": soul_file.stat().st_mtime if soul_file.exists() else None,
            "project_context_mtime": project_context["mtime"],
            "project_context_shadowed": project_context["shadowed"],
            "external_notes_enabled": _external_notes_sources_enabled(),
        },
    )


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


def _handle_skill_save(handler, body):
    try:
        require(body, "name", "content")
    except ValueError as e:
        return bad(handler, str(e))
    skill_name = body["name"].strip().lower().replace(" ", "-")
    if not skill_name or "/" in skill_name or ".." in skill_name:
        return bad(handler, "Invalid skill name")
    category = body.get("category", "").strip()
    if category and ("/" in category or ".." in category):
        return bad(handler, "Invalid category")
    skills_dir = _active_skills_dir()

    if category:
        skill_dir = skills_dir / category / skill_name
    else:
        skill_dir = skills_dir / skill_name
    # Validate resolved path stays within the active profile skills dir.
    try:
        skill_dir.resolve().relative_to(skills_dir.resolve())
    except ValueError:
        return bad(handler, "Invalid skill path")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    if skill_file.is_symlink():
        return bad(handler, "Cannot save to a symlinked skill file")
    skill_file.write_text(body["content"], encoding="utf-8")
    _SKILLS_STATS_CACHE.clear()
    return j(handler, {"ok": True, "name": skill_name, "path": str(skill_file)})


def _handle_skill_delete(handler, body):
    try:
        require(body, "name")
    except ValueError as e:
        return bad(handler, str(e))
    import shutil

    skill_name = str(body["name"]).strip().lower().replace(" ", "-")
    if not skill_name or "/" in skill_name or ".." in skill_name:
        return bad(handler, "Invalid skill name")
    skills_dir = _active_skills_dir()
    matches = [p for p in skills_dir.rglob("SKILL.md") if p.parent.name == skill_name]
    if not matches:
        return bad(handler, "Skill not found", 404)
    skill_dir = matches[0].parent
    shutil.rmtree(str(skill_dir))
    _SKILLS_STATS_CACHE.clear()
    return j(handler, {"ok": True, "name": body["name"]})


def _normalize_names_list(names) -> list[str]:
    """Normalize a config value (None/str/list) into a deduplicated str list."""
    if names is None:
        return []
    if isinstance(names, str):
        names = [names]
    elif not isinstance(names, list):
        names = list(names) if names else []
    return list(dict.fromkeys(str(d).strip() for d in names if str(d).strip()))


def _toggle_name_in_list(names, name: str, enabled: bool) -> list[str]:
    """Add or remove *name* from *names*, returning a new list."""
    names = _normalize_names_list(names)
    if enabled:
        return [d for d in names if d != name]
    if name not in names:
        names.append(name)
    return names


def _handle_skill_toggle(handler, body):
    """Toggle a skill's enabled/disabled state in the active profile's config.yaml.

    Writes through to ``skills.platform_disabled.webui`` when that key exists
    so the toggle takes effect for WebUI sessions (the agent's
    ``get_disabled_skill_names`` checks platform-specific lists first when
    ``HERMES_SESSION_PLATFORM`` is set).
    """
    try:
        require(body, "name", "enabled")
    except ValueError as e:
        return bad(handler, str(e))

    name = body["name"].strip()
    enabled = bool(body["enabled"])

    # Validate the skill exists in the filesystem
    skills_dir = _active_skills_dir()
    search_dirs = _active_skill_search_dirs(skills_dir)
    skill_dir, skill_md = _find_skill_in_dirs(name, search_dirs)
    if not skill_md:
        return bad(handler, f"Skill '{name}' not found", 404)

    config_path = _active_profile_config_path()
    with _cfg_lock:
        cfg = _load_yaml_config_file(config_path)

        # Ensure skills section exists as a dict
        if "skills" not in cfg or not isinstance(cfg["skills"], dict):
            cfg["skills"] = {}
        skills_cfg = cfg["skills"]

        # Always update the global disabled list
        skills_cfg["disabled"] = _toggle_name_in_list(
            skills_cfg.get("disabled"), name, enabled
        )

        # Write-through to platform_disabled.webui if it exists so that the
        # toggle takes effect for WebUI sessions (the agent checks the
        # platform-specific list first when HERMES_SESSION_PLATFORM=webui).
        platform_disabled = skills_cfg.get("platform_disabled")
        if isinstance(platform_disabled, dict) and "webui" in platform_disabled:
            platform_disabled["webui"] = _toggle_name_in_list(
                platform_disabled["webui"], name, enabled
            )

        cfg["skills"] = skills_cfg
        _save_yaml_config_file(config_path, cfg)

    reload_config()  # outside with block — reload_config() acquires the lock itself
    _SKILLS_STATS_CACHE.clear()
    return j(handler, {"ok": True, "name": name, "enabled": enabled})


def _handle_memory_write(handler, body):
    try:
        require(body, "section", "content")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        mem_dir = home / "memories"
    except ImportError:
        home = Path.home() / ".hermes"
        mem_dir = home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    section = body["section"]
    if section == "memory":
        target = mem_dir / "MEMORY.md"
    elif section == "user":
        target = mem_dir / "USER.md"
    elif section == "soul":
        target = home / "SOUL.md"
    else:
        return bad(handler, 'section must be "memory", "user", or "soul"')
    # Refuse to write through a symlinked target file: a symlink planted at the
    # memory path (e.g. via a restored/imported workspace) would otherwise let a
    # memory write clobber an arbitrary file outside the memories directory. This
    # mirrors the symlink-rejection hardening already shipped for skills/plugins
    # (#4217/#4234/#4240).
    if target.is_symlink():
        return bad(handler, "Cannot write to a symlinked memory file")
    try:
        target.write_text(body["content"], encoding="utf-8")
    except OSError as exc:
        if not isinstance(exc, PermissionError) and getattr(exc, "errno", None) != errno.EROFS:
            raise
        mode_hint = ""
        try:
            mode_hint = f" (mode {target.stat().st_mode & 0o777:o})"
        except OSError:
            pass
        return bad(
            handler,
            (
                f"{target.name} is not writable{mode_hint}: {target}. "
                "Run chmod 644 on the file or fix ownership on the shared volume."
            ),
            403,
        )
    return j(handler, {"ok": True, "section": section, "path": str(target)})


def _normalize_message_for_import_refresh(message: object) -> object:
    """Normalize message payloads for import refresh prefix checks.

    The strict dict comparison previously failed when existing messages held
    integer timestamps while refreshed messages held floating-point timestamps.
    Strip timing keys before comparison so we can safely treat semantic
    prefixes as equivalent.
    """
    if not isinstance(message, dict):
        return message
    normalized = dict(message)
    normalized.pop("timestamp", None)
    normalized.pop("_ts", None)
    return normalized


def _message_has_cli_tool_metadata(message: object) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("role") == "assistant" and message.get("tool_calls"):
        return True
    if message.get("role") == "tool" and (message.get("tool_call_id") or message.get("tool_name") or message.get("name")):
        return True
    return False


def _strip_cli_tool_metadata_for_refresh(message: object) -> object:
    if not isinstance(message, dict):
        return _normalize_message_for_import_refresh(message)
    normalized = _normalize_message_for_import_refresh(message)
    if not isinstance(normalized, dict):
        return normalized
    for key in ("tool_calls", "tool_call_id", "tool_name", "name"):
        normalized.pop(key, None)
    return normalized


def _is_cli_tool_metadata_enrichment(existing_messages: list, fresh_messages: list) -> bool:
    """Return True when fresh messages only add CLI tool metadata.

    Older imports from get_cli_session_messages() persisted assistant/tool rows
    without tool_calls, tool_call_id, or tool_name. After #1772 the refreshed
    transcript can have the same length but richer metadata, so re-imports must
    rebuild the stored sidecar even without a new row.
    """
    if not isinstance(existing_messages, list) or not isinstance(fresh_messages, list):
        return False
    if len(existing_messages) != len(fresh_messages):
        return False
    if any(_message_has_cli_tool_metadata(m) for m in existing_messages):
        return False
    if not any(_message_has_cli_tool_metadata(m) for m in fresh_messages):
        return False
    for idx, existing_message in enumerate(existing_messages):
        if _strip_cli_tool_metadata_for_refresh(existing_message) != _strip_cli_tool_metadata_for_refresh(fresh_messages[idx]):
            return False
    return True


def _is_messages_refresh_prefix_match(existing_messages: list, fresh_messages: list) -> bool:
    """Return True when existing_messages is a prefix of fresh_messages by value.

    This is a semantic comparison intended for import refresh, not deep
    structural equality. It intentionally ignores timing fields that may differ
    in type/precision between storage layers.
    """
    if not isinstance(existing_messages, list) or not isinstance(fresh_messages, list):
        return False
    if len(existing_messages) > len(fresh_messages):
        return False
    for idx, existing_message in enumerate(existing_messages):
        fresh_message = fresh_messages[idx]
        if _normalize_message_for_import_refresh(existing_message) != _normalize_message_for_import_refresh(fresh_message):
            return False
    return True


def _handle_session_import_cli(handler, body):
    """Import a single CLI session into the WebUI store."""
    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))

    sid = str(body["session_id"])
    requested_profile = _normalize_import_profile_value((body or {}).get("profile"))
    if requested_profile == "":
        return bad(handler, "invalid profile", 400)
    allow_all_profiles = _request_wants_all_profiles_import(body)
    if allow_all_profiles and _is_isolated_profile_mode():
        return bad(handler, "all_profiles import is not allowed in isolated profile mode", 403)
    if allow_all_profiles and not requested_profile:
        return bad(handler, "profile is required for all_profiles import", 400)

    # Check if already imported — refresh messages from CLI store if new ones arrived
    existing = Session.load(sid)
    if existing:
        # Cross-profile boundary: an unqualified (non-all-profiles) request must not
        # read or refresh a session that belongs to another profile, even though the
        # WebUI session store (SESSION_DIR) is a single global directory. This mirrors
        # the /api/session detail and /api/session/export profile-scoping gates.
        # An explicit all_profiles import is still allowed, but only when the request's
        # profile matches the stored session's profile.
        existing_profile = getattr(existing, "profile", None)
        if allow_all_profiles:
            if requested_profile and not _profiles_match(existing_profile, requested_profile):
                return bad(handler, "Session not found in CLI store", 404)
        elif not _session_visible_to_active_profile(existing_profile, handler):
            return bad(handler, "Session not found in CLI store", 404)
        refresh_profile = requested_profile or existing_profile
        cli_meta = _resolve_cli_import_metadata(
            sid,
            requested_profile=refresh_profile,
            allow_all_profiles=allow_all_profiles,
        )
        fresh_msgs = get_cli_session_messages(
            sid,
            profile=(cli_meta or {}).get("profile") or refresh_profile,
        )
        changed = False
        try:
            with edit_session(
                sid,
                session=existing,
                touch_updated_at=False,
                save_when=lambda _session: changed,
            ) as current:
                # Authorization was checked before reading the foreign store so an
                # unauthorized request cannot use refresh as a metadata oracle. Check
                # the repository-current record again before applying that data.
                current_profile = getattr(current, "profile", None)
                if allow_all_profiles:
                    if requested_profile and not _profiles_match(
                        current_profile, requested_profile
                    ):
                        return bad(handler, "Session not found in CLI store", 404)
                elif not _session_visible_to_active_profile(current_profile, handler):
                    return bad(handler, "Session not found in CLI store", 404)

                existing = current
                if fresh_msgs and len(fresh_msgs) > len(existing.messages):
                    # Prefix-equality guard: only extend if existing messages are a prefix of
                    # the fresh CLI messages. Prevents silently dropping WebUI-added messages
                    # on hybrid sessions (user sent messages via WebUI while CLI continued).
                    if _is_messages_refresh_prefix_match(existing.messages, fresh_msgs):
                        existing.messages = fresh_msgs
                        changed = True
                elif fresh_msgs and _is_cli_tool_metadata_enrichment(existing.messages, fresh_msgs):
                    # Same row count, richer payload: rebuild sidecars imported before
                    # CLI tool metadata was preserved (#1772).
                    existing.messages = fresh_msgs
                    changed = True
                if cli_meta:
                    # A subagent child must never be flipped to CLI-classified /
                    # writable on an existing-session refresh either (#5307).
                    _existing_is_sa = (
                        (existing.source_tag or existing.raw_source or "").strip().lower() == "subagent"
                        or (cli_meta.get("source_tag") or cli_meta.get("raw_source") or "").strip().lower() == "subagent"
                        or _is_subagent_child_session_id(sid)
                    )
                    updates = {
                        "is_cli_session": (False if _existing_is_sa else True),
                        "source_tag": existing.source_tag or cli_meta.get("source_tag"),
                        "raw_source": existing.raw_source or cli_meta.get("raw_source") or cli_meta.get("source_tag"),
                        "session_source": existing.session_source or cli_meta.get("session_source"),
                        "source_label": existing.source_label or cli_meta.get("source_label"),
                        "parent_session_id": existing.parent_session_id or cli_meta.get("parent_session_id"),
                    }
                    # A subagent child is view-only: also coerce read_only=True on the
                    # persisted sidecar so a stale writable (pre-fix) sidecar can't be
                    # used to start a WebUI turn (#5307).
                    if _existing_is_sa:
                        updates["read_only"] = True
                    for attr, value in updates.items():
                        if getattr(existing, attr, None) != value:
                            setattr(existing, attr, value)
                            changed = True
                else:
                    _existing_is_sa = (
                        (existing.source_tag or existing.raw_source or "").strip().lower() == "subagent"
                        or _is_subagent_child_session_id(sid)
                    )
        except KeyError:
            return bad(handler, "Session not found in CLI store", 404)
        if changed:
            publish_session_list_changed(
                "session_import_cli",
                profile=getattr(existing, "profile", None),
            )
        return j(
            handler,
            {
                "session": existing.compact()
                | {
                    "messages": existing.messages,
                    "is_cli_session": (False if _existing_is_sa else True),
                    # Greptile #4911 follow-up: read read_only from
                    # the persisted Session, NOT from cli_meta.  This
                    # refresh path is for an already-WebUI-owned
                    # session; the WebUI's persisted view is the
                    # source of truth for the response, not the
                    # foreign store's current value.  (Mirrors the
                    # GET /api/session fix.)
                    "read_only": bool(getattr(existing, "read_only", False)),
                },
                "imported": False,
            },
        )

    # Fetch messages from CLI store
    cli_meta = _resolve_cli_import_metadata(
        sid,
        requested_profile=requested_profile,
        allow_all_profiles=allow_all_profiles,
    )
    profile = cli_meta.get("profile") if cli_meta else (requested_profile if allow_all_profiles else None)
    msgs = get_cli_session_messages(sid, profile=profile)
    if not msgs:
        return bad(handler, "Session not found in CLI store", 404)

    # Get profile, model, timestamps, and title from CLI session metadata
    created_at = cli_meta.get("created_at") if cli_meta else None
    updated_at = cli_meta.get("updated_at") if cli_meta else None
    cli_title = cli_meta.get("title") if cli_meta else None
    cli_source_tag = cli_meta.get("source_tag") if cli_meta else None
    model = cli_meta.get("model", "unknown") if cli_meta else "unknown"
    cli_raw_source = cli_meta.get("raw_source") if cli_meta else None
    cli_session_source = cli_meta.get("session_source") if cli_meta else None
    cli_source_label = cli_meta.get("source_label") if cli_meta else None
    cli_user_id = cli_meta.get("user_id") if cli_meta else None
    cli_chat_id = cli_meta.get("chat_id") if cli_meta else None
    cli_chat_type = cli_meta.get("chat_type") if cli_meta else None
    cli_thread_id = cli_meta.get("thread_id") if cli_meta else None
    cli_session_key = cli_meta.get("session_key") if cli_meta else None
    cli_platform = cli_meta.get("platform") if cli_meta else None
    cli_parent_session_id = cli_meta.get("parent_session_id") if cli_meta else None
    cli_read_only = bool((cli_meta or {}).get("read_only"))
    # Delegated subagent children (#5307) are recovered VIEW-ONLY: they must
    # never be materialized as a writable WebUI sidecar via this endpoint, or a
    # subsequent chat-start/composer write would take ownership of a session
    # that belongs to the delegate runner. Treat them like an explicitly
    # read-only source (return the read-only stub payload, do not import), and
    # keep them out of the _isExternalSession frontend gates (is_cli_session=False).
    _sa_child = _is_subagent_child_session_id(sid)
    # Also treat a resolved-metadata subagent source as view-only: with
    # all_profiles=true, cli_meta is resolved from the requested (possibly
    # non-active) profile, so the active-profile state.db check (_sa_child)
    # can miss it (#5307 cross-profile edge).
    _cli_sa = (cli_source_tag or cli_raw_source or "").strip().lower() == "subagent"
    _sa_child = _sa_child or _cli_sa
    _read_only_view = cli_read_only or _sa_child

    # Use the CLI session title if available (e.g., cron job name), otherwise derive from messages
    title = cli_title or title_from(msgs, "CLI Session")

    # Auto-assign cron sessions to the dedicated "Cron Jobs" project (#1079),
    # gated on whether this profile has opted into project organization (#5379)
    cron_project_id = None
    if is_cron_session(sid, cli_source_tag):
        cron_project_id = ensure_cron_project(create=_profile_has_user_projects())

    if _read_only_view:
        session_payload = {
            "session_id": sid,
            "title": title,
            "workspace": str(get_last_workspace()),
            "model": model,
            "message_count": len(msgs),
            "created_at": created_at,
            "updated_at": updated_at,
            "last_message_at": updated_at or created_at,
            "pinned": False,
            "archived": False,
            "project_id": None,
            "profile": profile,
            # Subagent children (#5307) are recovered view-only and must NOT be
            # CLI-classified (keeps them out of the frontend _isExternalSession
            # gates); other explicitly-read-only sources keep is_cli_session=True.
            "is_cli_session": (False if _sa_child else True),
            "source_tag": cli_source_tag,
            "raw_source": cli_raw_source or cli_source_tag,
            "session_source": cli_session_source,
            "source_label": cli_source_label,
            "parent_session_id": cli_parent_session_id,
            "read_only": True,
            "messages": msgs,
            "tool_calls": [],
        }
        return j(handler, {"session": session_payload, "imported": False})

    s = import_cli_session(
        sid,
        title,
        msgs,
        model,
        profile=profile,
        created_at=created_at,
        updated_at=updated_at,
        parent_session_id=cli_parent_session_id,
        source_metadata={
            "project_id": cron_project_id,
            "is_cli_session": True,
            "source_tag": cli_source_tag,
            "raw_source": cli_raw_source or cli_source_tag,
            "session_source": cli_session_source,
            "source_label": cli_source_label,
            "user_id": cli_user_id,
            "chat_id": cli_chat_id,
            "chat_type": cli_chat_type,
            "thread_id": cli_thread_id,
            "session_key": cli_session_key,
            "platform": cli_platform,
            "model_provider": (cli_meta or {}).get("model_provider"),
        },
    )
    publish_session_list_changed(
        "session_import_cli",
        profile=getattr(s, "profile", None),
    )
    _queue_generated_title_for_imported_session(
        s,
        {
            "title": cli_title,
            "source_tag": cli_source_tag,
            "raw_source": cli_raw_source,
            "session_source": cli_session_source,
            "source_label": cli_source_label,
            "read_only": cli_read_only,
        },
    )
    return j(
        handler,
        {
            "session": s.compact()
            | {
                "messages": msgs,
                "is_cli_session": True,
            },
            "imported": True,
        },
    )


def _handle_session_import(handler, body):
    """Import a session from a JSON export. Creates a new session with a new ID."""
    if not body or not isinstance(body, dict):
        return bad(handler, "Request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list):
        return bad(handler, 'JSON must contain a "messages" array')
    title = body.get("title", "Imported session")
    try:
        workspace = str(resolve_trusted_workspace(body.get("workspace", str(DEFAULT_WORKSPACE))))
    except (TypeError, ValueError) as e:
        return bad(handler, str(e))
    model = body.get("model", DEFAULT_MODEL)
    s = Session(
        title=title,
        workspace=workspace,
        model=model,
        messages=messages,
        tool_calls=body.get("tool_calls", []),
        profile=get_active_profile_name(),
    )
    s.pinned = body.get("pinned", False)
    _publish_materialized_session(s, persist=True)
    publish_session_list_changed("session_import")
    return j(handler, {"ok": True, "session": s.compact() | {"messages": s.messages}})


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

def _handle_mcp_servers_list(handler):
    """List configured MCP servers with safe, read-only runtime visibility."""
    cfg = get_config_for_profile_home(get_active_hermes_home())
    servers = cfg.get("mcp_servers", {})
    if not isinstance(servers, dict):
        servers = {}
    runtime = _mcp_runtime_status_by_name()
    result = [
        _server_summary(name, scfg, runtime.get(str(name)))
        for name, scfg in servers.items()
    ]
    return j(handler, {
        "servers": result,
        "toggle_supported": True,
        "reload_required": True,
    })


def _handle_mcp_server_delete(handler, name):
    """Delete an MCP server by name."""
    from urllib.parse import unquote
    name = unquote(name)
    if not name:
        return bad(handler, "name is required")
    cfg = get_config()
    servers = cfg.get("mcp_servers", {})
    if not isinstance(servers, dict):
        servers = {}
    if name not in servers:
        return bad(handler, f"MCP server '{name}' not found", 404)
    del servers[name]
    cfg["mcp_servers"] = servers
    _save_yaml_config_file(_get_config_path(), cfg)
    reload_config()
    return j(handler, {"ok": True, "deleted": name})


def _handle_mcp_server_toggle(handler, name, body):
    """Toggle enabled state for an MCP server (PATCH /api/mcp/servers/{name})."""
    from urllib.parse import unquote
    name = unquote(name)
    if not name:
        return bad(handler, "name is required")
    if "enabled" not in body:
        return bad(handler, "enabled field is required")
    enabled = bool(body["enabled"])
    cfg = get_config()
    servers = cfg.get("mcp_servers", {})
    if not isinstance(servers, dict):
        servers = {}
    if name not in servers:
        return bad(handler, f"MCP server '{name}' not found", 404)
    if not isinstance(servers[name], dict):
        return bad(handler, f"MCP server '{name}' has invalid config", 400)
    servers[name]["enabled"] = enabled
    cfg["mcp_servers"] = servers
    _save_yaml_config_file(_get_config_path(), cfg)
    reload_config()
    return j(handler, {"ok": True, "name": name, "enabled": enabled})


_MASKED_PLACEHOLDER = "••••••"


def _strip_masked_values(submitted, existing):
    """Remove masked placeholder values from submitted dict, keeping originals."""
    if not isinstance(submitted, dict) or not isinstance(existing, dict):
        return submitted
    cleaned = {}
    for k, v in submitted.items():
        if isinstance(v, str) and v == _MASKED_PLACEHOLDER:
            if k in existing and isinstance(existing[k], str):
                cleaned[k] = existing[k]  # preserve original real value
                continue
        elif isinstance(v, dict) and k in existing and isinstance(existing[k], dict):
            cleaned[k] = _strip_masked_values(v, existing[k])
        else:
            cleaned[k] = v
    return cleaned


def _handle_mcp_server_update(handler, name, body):
    """Add or update an MCP server."""
    from urllib.parse import unquote
    name = unquote(name)
    if not name:
        return bad(handler, "name is required")
    # Validate: must have url (http) or command (stdio)
    server_cfg = {}
    cfg = get_config()
    servers = cfg.get("mcp_servers", {})
    if not isinstance(servers, dict):
        servers = {}
    existing_cfg = servers.get(name, {})
    if body.get("url"):
        server_cfg["url"] = body["url"].strip()
        if body.get("headers"):
            server_cfg["headers"] = _strip_masked_values(body["headers"], existing_cfg.get("headers", {}))
    elif body.get("command"):
        server_cfg["command"] = body["command"].strip()
        if body.get("args"):
            server_cfg["args"] = body["args"] if isinstance(body["args"], list) else [body["args"]]
        if body.get("env"):
            server_cfg["env"] = _strip_masked_values(body["env"], existing_cfg.get("env", {}))
    else:
        return bad(handler, "url or command is required")
    if body.get("timeout") is not None:
        try:
            server_cfg["timeout"] = int(body["timeout"])
        except (ValueError, TypeError):
            pass
    servers[name] = server_cfg
    cfg["mcp_servers"] = servers
    _save_yaml_config_file(_get_config_path(), cfg)
    reload_config()
    return j(handler, {"ok": True, "server": _server_summary(name, server_cfg)})
