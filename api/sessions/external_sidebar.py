"""Stable read-only external-session sidebar Interface."""

# Projection, cache, and context ownership now live in dedicated modules.  The
# imports here intentionally preserve the historical patch/import surface.
# ruff: noqa: F401

from __future__ import annotations

import collections
import copy
import inspect
import json
import logging
import os
import threading
import time
from pathlib import Path

from api.agent_sessions import (
    is_cli_session_row,
    normalize_agent_session_source,
    read_importable_agent_session_rows,
)
from api.config import HOME, SESSION_DIR, SESSION_INDEX_FILE
from api.workspace import get_last_workspace
from .claude_code import (
    CLAUDE_CODE_SOURCE,
    _default_claude_code_projects_dir,
    _normalize_cli_session_source_filter,
    get_claude_code_sessions,
)
from .projects import (
    _profile_has_user_projects,
    ensure_cron_project,
    ensure_webhook_project,
    is_cron_session,
    is_webhook_session,
)
from .records import (
    Session,
    _active_stream_ids,
    _load_webui_deleted_session_tombstone,
    is_safe_session_id,
)
from .state_db_identity import state_db_cache_key

logger = logging.getLogger(__name__)
from . import external_sidebar_cache as _cache
from .external_sidebar_cache import (
    _CLI_SESSIONS_CACHE,
    _CLI_SESSIONS_CACHE_INFLIGHT,
    _CLI_SESSIONS_CACHE_INVALIDATION_VERSION,
    _CLI_SESSIONS_CACHE_LOCK,
    _CLI_SESSIONS_CACHE_MAX_ENTRIES,
    _CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS,
    _CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS,
    _CLI_SESSIONS_CACHE_TTL_SECONDS,
    _CLI_SESSIONS_CACHE_WAIT_SECONDS,
    _cache_cli_sessions_if_current,
    _cli_sessions_cache_claim_rebuild,
    _cli_sessions_cache_done,
    _cli_sessions_cache_invalidation_stamp,
    _copy_cli_sessions,
    _copy_fresh_cli_sessions_cache_entry,
    _load_and_cache_cli_sessions,
    _reload_cli_sessions_after_inflight,
    clear_cli_sessions_cache,
)
from .external_sidebar_context import (
    _all_profiles_cli_contexts,
    _callable_accepts_include_claude_code,
    _cli_sessions_cache_ttl_seconds,
    _cli_sessions_streaming_freeze_marker,
    _path_cache_key,
    _path_stat_cache_key,
    _resolve_cli_sessions_context,
)
from .external_sidebar_projection import (
    CLI_VISIBLE_SESSION_LIMIT,
    CRON_PROJECT_CHIP_LIMIT,
    WEBHOOK_PROJECT_CHIP_LIMIT,
    _SIDECAR_METADATA_CACHE,
    _SIDECAR_METADATA_CACHE_LOCK,
    _SIDECAR_METADATA_CACHE_MAX,
    _load_cli_sessions_uncached,
    _state_projection_sidecar_metadata,
    clear_sidecar_metadata_cache,
)


def get_cli_sessions(
    source_filter=None,
    *,
    all_profiles: bool = False,
    include_claude_code: bool = True,
) -> list:
    """Read CLI sessions from the agent's SQLite store and return them as
    dicts in a format the WebUI sidebar can render alongside local sessions.

    Returns empty list if the SQLite DB is missing or any error occurs -- the
    bridge is purely additive and never crashes the WebUI.
    """
    source_filter = _normalize_cli_session_source_filter(source_filter)
    if all_profiles:
        contexts, context_cache_key = _all_profiles_cli_contexts()
        db_path = "all profiles"
        # #4842: freeze the volatile per-profile state.db component while
        # streaming so a streamed message row in one profile doesn't bust the
        # all-profiles CLI cache and re-run every profile's heavy projection.
        _streaming_marker = _cli_sessions_streaming_freeze_marker()
        if _streaming_marker is not None:
            context_cache_key = ('streaming-frozen', _streaming_marker)
        cache_key = (
            'all_profiles',
            source_filter or '',
            bool(include_claude_code),
            context_cache_key,
            _path_cache_key(_default_claude_code_projects_dir()),
            _path_stat_cache_key(_default_claude_code_projects_dir()),
            _path_stat_cache_key(SESSION_INDEX_FILE),
        )
    else:
        resolve_kwargs = {}
        resolve_supports_include_claude_code = _callable_accepts_include_claude_code(
            _resolve_cli_sessions_context
        )
        if resolve_supports_include_claude_code:
            resolve_kwargs['include_claude_code'] = include_claude_code
        hermes_home, db_path, cli_profile, cache_key = _resolve_cli_sessions_context(
            source_filter,
            **resolve_kwargs,
        )
        if not resolve_supports_include_claude_code:
            cache_key = cache_key + (bool(include_claude_code),)
    ttl = _cli_sessions_cache_ttl_seconds()
    now = time.monotonic()

    def _load_sessions():
        loader_supports_include_claude_code = _callable_accepts_include_claude_code(
            _load_cli_sessions_uncached
        )
        if all_profiles:
            merged: list[dict] = []
            for idx, (ctx_home, ctx_db_path, ctx_profile) in enumerate(contexts):
                load_kwargs = {
                    'source_filter': source_filter,
                    'visible_session_limit': None,
                    'cron_project_limit': None,
                    'webhook_project_limit': None,
                }
                if loader_supports_include_claude_code:
                    load_kwargs['include_claude_code'] = include_claude_code and idx == 0
                merged.extend(
                    _load_cli_sessions_uncached(
                        ctx_home,
                        ctx_db_path,
                        ctx_profile,
                        **load_kwargs,
                    )
                )
            return merged
        load_kwargs = {'source_filter': source_filter}
        if loader_supports_include_claude_code:
            load_kwargs['include_claude_code'] = include_claude_code
        return _load_cli_sessions_uncached(
            hermes_home,
            db_path,
            cli_profile,
            **load_kwargs,
        )

    if ttl > 0:
        stale_sessions = None
        stale_stamp = None
        with _cache._CLI_SESSIONS_CACHE_LOCK:
            cached_entry = _cache._CLI_SESSIONS_CACHE.get(cache_key)
            if cached_entry is not None:
                if len(cached_entry) == 3:
                    cached_expires_at, cached_stamp, cached_sessions = cached_entry
                else:
                    cached_expires_at, cached_sessions = cached_entry
                    cached_stamp = _cache._CLI_SESSIONS_CACHE_INVALIDATION_VERSION
                if cached_stamp != _cache._CLI_SESSIONS_CACHE_INVALIDATION_VERSION:
                    _cache._CLI_SESSIONS_CACHE.pop(cache_key, None)
                elif cached_expires_at > now:
                    # LRU: a fresh hit is the most-recently-used entry.
                    _cache._CLI_SESSIONS_CACHE.move_to_end(cache_key)
                    return _cache._copy_cli_sessions(cached_sessions)
                else:
                    stale_sessions = _cache._copy_cli_sessions(cached_sessions)
                    stale_stamp = cached_stamp
        event, is_owner = _cache._cli_sessions_cache_claim_rebuild(cache_key)
        if is_owner:
            try:
                invalidation_stamp = _cache._cli_sessions_cache_invalidation_stamp()
                return _cache._load_and_cache_cli_sessions(
                    cache_key=cache_key,
                    ttl=ttl,
                    invalidation_stamp=invalidation_stamp,
                    load_sessions=_load_sessions,
                    stale_sessions=stale_sessions,
                    stale_stamp=stale_stamp,
                    all_profiles=all_profiles,
                    db_path=db_path,
                )
            finally:
                _cache._cli_sessions_cache_done(cache_key, event)
        return _cache._reload_cli_sessions_after_inflight(
            cache_key=cache_key,
            ttl=ttl,
            stale_sessions=stale_sessions,
            stale_stamp=stale_stamp,
            load_sessions=_load_sessions,
            all_profiles=all_profiles,
            db_path=db_path,
        )

    try:
        return _load_sessions()
    except Exception as _cli_err:
        logger.warning(
            "get_cli_sessions() failed — check state.db schema or path (%s): %s",
            "all profiles" if all_profiles else db_path, _cli_err,
        )
        return []
