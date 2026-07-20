"""Profile, stream, and persistence identity for external sidebar caching."""

from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path

from api.config import HOME, SESSION_INDEX_FILE
from .claude_code import (
    _default_claude_code_projects_dir,
)
from .records import (
    _active_stream_ids,
)
from .state_db_identity import state_db_cache_key
from . import external_sidebar_cache as _cache

logger = logging.getLogger(__name__)


def _cli_sessions_cache_ttl_seconds() -> float:
    # #4842: widen the freshness window while a turn is streaming so the fixed
    # streaming poll cadence doesn't force a rebuild on every poll. Paired
    # with the streaming-freeze cache key (so the key is stable across polls
    # mid-stream), this bounds the heavy CLI/cron projection to one rebuild per
    # streaming-TTL window instead of one per poll. Mirrors the route-level
    # #4808 TTL widening.
    try:
        if _cli_sessions_streaming_freeze_marker() is not None:
            return max(0.0, float(_cache._CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, float(_cache._CLI_SESSIONS_CACHE_TTL_SECONDS))
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


def _cli_sessions_streaming_freeze_marker():
    """Return a stable cache-key marker while any turn is actively streaming.

    The CLI/cron sidebar projection (``_load_cli_sessions_uncached``) is gated by
    ``_CLI_SESSIONS_CACHE``, whose key folds in ``state_db_cache_key`` →
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
    db_state_key = _streaming_marker if _streaming_marker is not None else state_db_cache_key(db_path)
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
            profiles_root as _profiles_root,
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
        cache_entries.append((home_key, profile_value, state_db_cache_key(db_path)))

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
