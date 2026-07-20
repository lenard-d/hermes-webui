"""Cached CLI-session listing.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

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
        with _CLI_SESSIONS_CACHE_LOCK:
            cached_entry = _CLI_SESSIONS_CACHE.get(cache_key)
            if cached_entry is not None:
                if len(cached_entry) == 3:
                    cached_expires_at, cached_stamp, cached_sessions = cached_entry
                else:
                    cached_expires_at, cached_sessions = cached_entry
                    cached_stamp = _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
                if cached_stamp != _CLI_SESSIONS_CACHE_INVALIDATION_VERSION:
                    _CLI_SESSIONS_CACHE.pop(cache_key, None)
                elif cached_expires_at > now:
                    # LRU: a fresh hit is the most-recently-used entry.
                    _CLI_SESSIONS_CACHE.move_to_end(cache_key)
                    return _copy_cli_sessions(cached_sessions)
                else:
                    stale_sessions = _copy_cli_sessions(cached_sessions)
                    stale_stamp = cached_stamp
        event, is_owner = _cli_sessions_cache_claim_rebuild(cache_key)
        if is_owner:
            try:
                invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
                return _load_and_cache_cli_sessions(
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
                _cli_sessions_cache_done(cache_key, event)
        return _reload_cli_sessions_after_inflight(
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

__all__ = ['get_cli_sessions']
