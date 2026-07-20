"""CLI projection caches and profile contexts.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())


def _copy_cli_sessions(sessions: list) -> list:
    return copy.deepcopy(sessions)


def _cli_sessions_cache_invalidation_stamp() -> int:
    with _CLI_SESSIONS_CACHE_LOCK:
        return int(_CLI_SESSIONS_CACHE_INVALIDATION_VERSION)


def _cli_sessions_cache_claim_rebuild(cache_key: tuple) -> tuple[threading.Event, bool]:
    with _CLI_SESSIONS_CACHE_LOCK:
        current = _CLI_SESSIONS_CACHE_INFLIGHT.get(cache_key)
        if current is not None:
            return current, False
        event = threading.Event()
        _CLI_SESSIONS_CACHE_INFLIGHT[cache_key] = event
        return event, True


def _cli_sessions_cache_done(cache_key: tuple, event: threading.Event | None) -> None:
    with _CLI_SESSIONS_CACHE_LOCK:
        if event is None:
            return
        if _CLI_SESSIONS_CACHE_INFLIGHT.get(cache_key) is event:
            _CLI_SESSIONS_CACHE_INFLIGHT.pop(cache_key, None)
    if event is not None:
        event.set()


def _cache_cli_sessions_if_current(
    cache_key: tuple,
    ttl: float,
    invalidation_stamp: int,
    sessions: list,
) -> bool:
    with _CLI_SESSIONS_CACHE_LOCK:
        if _CLI_SESSIONS_CACHE_INVALIDATION_VERSION != invalidation_stamp:
            return False
        _CLI_SESSIONS_CACHE[cache_key] = (
            time.monotonic() + ttl,
            invalidation_stamp,
            _copy_cli_sessions(sessions),
        )
        _CLI_SESSIONS_CACHE.move_to_end(cache_key)
        while len(_CLI_SESSIONS_CACHE) > _CLI_SESSIONS_CACHE_MAX_ENTRIES:
            _CLI_SESSIONS_CACHE.popitem(last=False)
    return True


def _copy_fresh_cli_sessions_cache_entry(cache_key: tuple):
    with _CLI_SESSIONS_CACHE_LOCK:
        cached_entry = _CLI_SESSIONS_CACHE.get(cache_key)
        if cached_entry is None:
            return None
        if len(cached_entry) == 3:
            cached_expires_at, cached_stamp, cached_sessions = cached_entry
        else:
            cached_expires_at, cached_sessions = cached_entry
            cached_stamp = _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
        if cached_stamp != _CLI_SESSIONS_CACHE_INVALIDATION_VERSION:
            _CLI_SESSIONS_CACHE.pop(cache_key, None)
            return None
        if cached_expires_at <= time.monotonic():
            return None
        # LRU: a fresh hit is the most-recently-used entry.
        _CLI_SESSIONS_CACHE.move_to_end(cache_key)
        return _copy_cli_sessions(cached_sessions)


def _load_and_cache_cli_sessions(
    *,
    cache_key: tuple,
    ttl: float,
    invalidation_stamp: int,
    load_sessions,
    stale_sessions,
    stale_stamp,
    all_profiles: bool,
    db_path,
) -> list:
    try:
        sessions = load_sessions()
    except Exception as _cli_err:
        logger.warning(
            "get_cli_sessions() failed — check state.db schema or path (%s): %s",
            "all profiles" if all_profiles else db_path, _cli_err,
        )
        if stale_sessions is not None and stale_stamp == _cli_sessions_cache_invalidation_stamp():
            return stale_sessions
        return []
    _cache_cli_sessions_if_current(
        cache_key,
        ttl,
        invalidation_stamp,
        sessions,
    )
    return _copy_cli_sessions(sessions)


def _reload_cli_sessions_after_inflight(
    *,
    cache_key: tuple,
    ttl: float,
    stale_sessions,
    stale_stamp,
    load_sessions,
    all_profiles: bool,
    db_path: str,
) -> list:
    while True:
        event, is_owner = _cli_sessions_cache_claim_rebuild(cache_key)
        if is_owner:
            break
        wait_finished = False
        try:
            wait_finished = bool(
                event.wait(
                    _CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS
                    if stale_sessions is not None
                    else _CLI_SESSIONS_CACHE_WAIT_SECONDS
                )
            )
        except Exception:
            pass
        cached_sessions = _copy_fresh_cli_sessions_cache_entry(cache_key)
        if cached_sessions is not None:
            return cached_sessions
        if stale_sessions is not None and stale_stamp == _cli_sessions_cache_invalidation_stamp():
            return stale_sessions
        if not wait_finished:
            fallback_invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
            return _load_and_cache_cli_sessions(
                cache_key=cache_key,
                ttl=ttl,
                invalidation_stamp=fallback_invalidation_stamp,
                load_sessions=load_sessions,
                stale_sessions=stale_sessions,
                stale_stamp=stale_stamp,
                all_profiles=all_profiles,
                db_path=db_path,
            )
    try:
        invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
        return _load_and_cache_cli_sessions(
            cache_key=cache_key,
            ttl=ttl,
            invalidation_stamp=invalidation_stamp,
            load_sessions=load_sessions,
            stale_sessions=stale_sessions,
            stale_stamp=stale_stamp,
            all_profiles=all_profiles,
            db_path=db_path,
        )
    finally:
        _cli_sessions_cache_done(cache_key, event)


def _cli_sessions_cache_ttl_seconds() -> float:
    # #4842: widen the freshness window while a turn is streaming so the fixed
    # streaming poll cadence doesn't force a rebuild on every poll. Paired
    # with the streaming-freeze cache key (so the key is stable across polls
    # mid-stream), this bounds the heavy CLI/cron projection to one rebuild per
    # streaming-TTL window instead of one per poll. Mirrors the route-level
    # #4808 TTL widening.
    try:
        if _cli_sessions_streaming_freeze_marker() is not None:
            return max(0.0, float(_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, float(_CLI_SESSIONS_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        return 5.0


def _path_cache_key(path) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except Exception:
        return str(path)


def _path_stat_cache_key(path):
    if path is None:
        return None
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _callable_accepts_include_claude_code(callable_obj) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    if 'include_claude_code' in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _sqlite_content_fingerprint(db_path: Path):
    """Return a commit-reliable content fingerprint for a state.db.

    The stat-only key below (mtime_ns + size of the .db/-wal/-shm files) is NOT
    reliable for cache invalidation: in WAL mode a commit lands in the -wal file,
    and under fast sequential writes the (mtime_ns, size) of the sidecars can
    COLLIDE with a previously cached stamp (same nanosecond bucket + a WAL frame
    that lands at the same offset/size after a prior checkpoint truncation), so a
    freshly-committed gateway/CLI session is intermittently served from the stale
    Python cache. PRAGMA data_version does NOT help here either — read from a
    fresh per-request connection it always reports that connection's own initial
    value and never advances (verified). A cheap content fingerprint over the
    sessions/messages tables, read on a fresh connection, DOES advance on every
    commit (incl. external gateway writes) and is immune to mtime granularity.
    Cost is a pair of indexed COUNT/MAX queries (sub-ms), far cheaper than the
    full uncached session scan this key gates.
    """
    try:
        if not Path(db_path).exists():
            return None
    except OSError:
        return None
    try:
        import sqlite3
        # Read-only + a tiny busy timeout: a fingerprint read must NEVER stall the
        # /api/sessions hot path when state.db is briefly locked by a writer.
        # On lock (or any error) we return None and the caller falls back to the
        # cheap file-stat stamp, so correctness degrades gracefully to the prior
        # behavior rather than blocking for the default multi-second busy timeout.
        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=0.05
            )
        except Exception:
            return None
        try:
            conn.execute("PRAGMA busy_timeout=50")
            parts = []
            for table in ("sessions", "messages"):
                try:
                    # MAX(rowid) is an O(1) index lookup (no table scan) and
                    # advances on every INSERT. Pair it with the table's largest
                    # rowid + a count-free total: we deliberately avoid COUNT(*)
                    # which forces a full SCAN on large messages tables (~tens of
                    # ms per sidebar refresh on a big store). MAX(rowid) misses a
                    # pure DELETE-without-insert, but the file-stat fallback in
                    # _sqlite_file_stat_cache_key still moves on a delete commit,
                    # and a delete never makes a MISSING row appear (the flake we
                    # fix is an ADDED row not showing up). It also misses a plain
                    # `UPDATE sessions SET title/message_count` with no message
                    # insert (state_sync.py sync) — those fall back to the stat
                    # stamp + 5s TTL, i.e. the prior behavior (a title-only rename
                    # can lag <=5s); no regression vs the old stat-only key.
                    row = conn.execute(
                        f"SELECT MAX(rowid) FROM {table}"
                    ).fetchone()
                    parts.append(row[0] if row else None)
                except Exception:
                    parts.append(None)
            return tuple(parts)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return None


def _sqlite_file_stat_cache_key(db_path: Path):
    """Return a commit-reliable invalidation key for a SQLite DB.

    Combines a content fingerprint (the authoritative signal — advances on every
    commit, immune to mtime-granularity collisions that flaked the gateway_sync
    test) with the cheap file stat stamps as a belt-and-suspenders fallback for
    the case where the fingerprint can't be read.
    """
    return (
        _sqlite_content_fingerprint(db_path),
        _path_stat_cache_key(db_path),
        _path_stat_cache_key(Path(f"{db_path}-wal")),
        _path_stat_cache_key(Path(f"{db_path}-shm")),
    )


def _cli_sessions_streaming_freeze_marker():
    """Return a stable cache-key marker while any turn is actively streaming.

    The CLI/cron sidebar projection (``_load_cli_sessions_uncached``) is gated by
    ``_CLI_SESSIONS_CACHE``, whose key folds in ``_sqlite_file_stat_cache_key`` →
    ``_sqlite_content_fingerprint`` (``MAX(rowid) FROM messages``). During an
    active chat turn the gateway/CLI writes a message row per streamed delta, so
    that fingerprint advances on essentially every ``/api/sessions`` poll — busting
    the CLI cache and re-running the expensive candidate-join + projection (and the
    lineage-metadata pass) on every poll, while contending for the same SQLite/global
    lock the streaming worker holds. That is the multi-second ``get_cli_sessions``
    in #4842 (and #4672/#4808).

    The route-level session-list cache already freezes its own key during streaming
    (#4808 ``_session_list_cache_streaming_freeze_marker``), but that freeze never
    reached this *inner* CLI-sessions cache, so the heavy CLI/cron query still
    re-ran whenever the outer cache validated. This marker mirrors the route-level
    one: keyed only on the *set* of active stream ids, it is constant while the same
    turn(s) stream (so the projection is reused across polls) and changes the instant
    a stream starts/stops (so the just-finished turn's rows are picked up promptly).
    A streaming session's own CLI/cron title/count is not what this projection
    returns (the streaming session is overlaid live by the route layer), and any
    in-app structural mutation invalidates the cache directly via
    ``clear_cli_sessions_cache``. Externally-driven changes that don't fire that
    listener (a scheduled cron completing, an external CLI writing rows) surface
    after the streaming TTL expires and a subsequent refresh occurs rather than
    instantly — a bounded, self-healing lag that is the deliberate latency/CPU
    trade-off of the freeze. (#4842)
    """
    try:
        active = _active_stream_ids()
    except Exception:
        return None
    if not active:
        return None
    try:
        return ("streaming", tuple(sorted(str(x) for x in active)))
    except Exception:
        return ("streaming",)


def _resolve_cli_sessions_context(source_filter=None, include_claude_code: bool = True):
    # Use the active WebUI profile's HERMES_HOME to find state.db.
    # The active profile is determined by what the user has selected in the UI
    # (stored in the server's runtime config). This means:
    #   - default profile  -> ~/.hermes/state.db
    #   - named profile X  -> ~/.hermes/profiles/X/state.db
    # We resolve the active profile's home directory rather than just using
    # HERMES_HOME (which is the server's launch profile, not necessarily the
    # active one after a profile switch).
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()

    try:
        from api.profiles import get_active_profile_name
        cli_profile = get_active_profile_name()
    except Exception:
        cli_profile = None

    db_path = hermes_home / 'state.db'
    projects_dir = _default_claude_code_projects_dir()
    # #4842: while a turn streams, freeze the volatile state.db component of the
    # key so per-message writes don't bust the CLI cache and re-run the heavy
    # CLI/cron projection on every poll (mirrors the route-level #4808 freeze).
    # The wider streaming TTL in get_cli_sessions() still permits a periodic
    # rebuild after that window, and structural mutations invalidate via
    # clear_cli_sessions_cache().
    _streaming_marker = _cli_sessions_streaming_freeze_marker()
    db_state_key = _streaming_marker if _streaming_marker is not None else _sqlite_file_stat_cache_key(db_path)
    cache_key = (
        str(hermes_home),
        str(cli_profile or ''),
        str(db_path),
        str(source_filter or ''),
        db_state_key,
        bool(include_claude_code),
        _path_cache_key(projects_dir),
        _path_stat_cache_key(projects_dir),
        _path_stat_cache_key(SESSION_INDEX_FILE),
    )
    return hermes_home, db_path, cli_profile, cache_key


def _all_profiles_cli_contexts() -> tuple[list[tuple[Path, Path, str | None]], tuple]:
    """Return per-profile CLI scan contexts plus a cache key fragment."""
    try:
        from api.profiles import (
            _profiles_root,
            get_active_profile_name,
            get_hermes_home_for_profile,
            list_profiles_api,
        )
    except Exception:
        return [], ()

    contexts: list[tuple[Path, Path, str | None]] = []
    cache_entries: list[tuple[str, str, object]] = []
    seen_homes: set[str] = set()

    def _add_context(profile_name) -> None:
        try:
            hermes_home = Path(get_hermes_home_for_profile(profile_name)).expanduser().resolve()
        except Exception:
            return
        home_key = _path_cache_key(hermes_home)
        if not home_key or home_key in seen_homes:
            return
        seen_homes.add(home_key)
        db_path = hermes_home / 'state.db'
        profile_value = str(profile_name or 'default').strip() or 'default'
        contexts.append((hermes_home, db_path, profile_value))
        cache_entries.append((home_key, profile_value, _sqlite_file_stat_cache_key(db_path)))

    try:
        _add_context(get_active_profile_name())
    except Exception:
        pass
    try:
        for row in list_profiles_api():
            if not isinstance(row, dict):
                continue
            _add_context(row.get('name'))
    except Exception:
        logger.debug("All-profiles CLI context enumeration failed", exc_info=True)
    try:
        for entry in _profiles_root().iterdir():
            if not entry.is_dir():
                continue
            _add_context(entry.name)
    except Exception:
        logger.debug("All-profiles CLI directory enumeration failed", exc_info=True)

    return contexts, tuple(cache_entries)

__all__ = ['_copy_cli_sessions', '_cli_sessions_cache_invalidation_stamp', '_cli_sessions_cache_claim_rebuild', '_cli_sessions_cache_done', '_cache_cli_sessions_if_current', '_copy_fresh_cli_sessions_cache_entry', '_load_and_cache_cli_sessions', '_reload_cli_sessions_after_inflight', '_cli_sessions_cache_ttl_seconds', '_path_cache_key', '_path_stat_cache_key', '_callable_accepts_include_claude_code', '_sqlite_content_fingerprint', '_sqlite_file_stat_cache_key', '_cli_sessions_streaming_freeze_marker', '_resolve_cli_sessions_context', '_all_profiles_cli_contexts']
