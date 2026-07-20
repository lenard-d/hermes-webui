"""Read-only CLI, cron, webhook, and Claude sidebar discovery.

The module owns the complete external-sidebar projection: profile-aware source
discovery, state-row materialization, UI-sidecar overlays, and the single-flight
cache whose generation prevents invalidated data from being republished.
"""

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
from .state_db import state_db_cache_key

logger = logging.getLogger(__name__)

CLI_VISIBLE_SESSION_LIMIT = 20
CRON_PROJECT_CHIP_LIMIT = 200
WEBHOOK_PROJECT_CHIP_LIMIT = 200
_CLI_SESSIONS_CACHE_TTL_SECONDS = 5.0
_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS = 45.0
_CLI_SESSIONS_CACHE_LOCK = threading.Lock()
_CLI_SESSIONS_CACHE_INFLIGHT: dict[tuple, threading.Event] = {}
_CLI_SESSIONS_CACHE_INVALIDATION_VERSION = 0
_CLI_SESSIONS_CACHE: collections.OrderedDict[tuple, tuple] = collections.OrderedDict()
_CLI_SESSIONS_CACHE_MAX_ENTRIES = 8
_CLI_SESSIONS_CACHE_WAIT_SECONDS = 0.25
_CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS = 0.10
_SIDECAR_METADATA_CACHE_LOCK = threading.Lock()
_SIDECAR_METADATA_CACHE: collections.OrderedDict[tuple, dict] = collections.OrderedDict()
_SIDECAR_METADATA_CACHE_MAX = 2000

def clear_cli_sessions_cache() -> None:
    with _CLI_SESSIONS_CACHE_LOCK:
        global _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
        _CLI_SESSIONS_CACHE_INVALIDATION_VERSION += 1
        _CLI_SESSIONS_CACHE.clear()
    # The sidecar-metadata projection cache is stat-keyed (self-invalidating on
    # any file change), but clear it alongside the CLI cache so an explicit
    # reset — a mutating sidebar action or test isolation — starts fully cold.
    clear_sidecar_metadata_cache()



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


def clear_sidecar_metadata_cache() -> None:
    """Drop all memoized sidebar-projection sidecar metadata (test/lifecycle hook)."""
    with _SIDECAR_METADATA_CACHE_LOCK:
        _SIDECAR_METADATA_CACHE.clear()


def _state_projection_sidecar_metadata(sid: str) -> dict:
    """Return UI-owned metadata (title + archived) for a state.db-projected row.

    Memoized by the sidecar file's (path, mtime_ns, size, ctime_ns) stat
    signature so the sidebar projection — which calls this once per row in both
    the visible pass and the up-to-200-row cron pass — pays a single os.stat per
    file on a warm build instead of an open() + 64KB read + JSON-key scan
    (#4842). A rename/archive/edit bumps the signature and invalidates just that
    entry, so a stale title/archived flag is impossible without re-reading.
    Returns a COPY so callers can't mutate the cached dict.

    NOTE: this stat-gates on ``SESSION_DIR / f'{sid}.json'`` because that file is
    ``Session.load_metadata_only``'s sole source for title+archived. If that ever
    stops being true (metadata moves to another store), this gate would short-
    circuit before the real source — update both together.
    """
    default = {"title": None, "archived": False}
    if not is_safe_session_id(sid):
        return dict(default)
    p = SESSION_DIR / f'{sid}.json'
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size, st.st_ctime_ns)
    except OSError:
        # No sidecar file (the common case for a pure state.db row) or it
        # vanished mid-build — nothing to project, and nothing worth caching.
        return dict(default)

    with _SIDECAR_METADATA_CACHE_LOCK:
        hit = _SIDECAR_METADATA_CACHE.get(key)
        if hit is not None:
            _SIDECAR_METADATA_CACHE.move_to_end(key)
            return dict(hit)

    metadata = dict(default)
    try:
        webui_meta = Session.load_metadata_only(sid)
    except Exception:
        webui_meta = None
    if webui_meta:
        title = getattr(webui_meta, 'title', None)
        if title:
            metadata["title"] = title
        metadata["archived"] = bool(getattr(webui_meta, 'archived', False))

    with _SIDECAR_METADATA_CACHE_LOCK:
        # Re-check under lock in case a concurrent build populated it; either
        # entry is equally valid for the same stat signature.
        if key not in _SIDECAR_METADATA_CACHE:
            _SIDECAR_METADATA_CACHE[key] = metadata
            _SIDECAR_METADATA_CACHE.move_to_end(key)
            while len(_SIDECAR_METADATA_CACHE) > _SIDECAR_METADATA_CACHE_MAX:
                _SIDECAR_METADATA_CACHE.popitem(last=False)
    return dict(metadata)


def _load_cli_sessions_uncached(
    hermes_home: Path,
    db_path: Path,
    _cli_profile,
    source_filter=None,
    *,
    visible_session_limit: int | None = None,
    cron_project_limit: int | None | bool = CRON_PROJECT_CHIP_LIMIT,
    webhook_project_limit: int | None | bool = WEBHOOK_PROJECT_CHIP_LIMIT,
    include_claude_code: bool = True,
) -> list:
    cli_sessions = []
    if source_filter in (None, CLAUDE_CODE_SOURCE) and include_claude_code:
        try:
            cli_sessions.extend(get_claude_code_sessions())
        except Exception:
            logger.debug("Claude Code session scan failed", exc_info=True)

    if source_filter == CLAUDE_CODE_SOURCE:
        return cli_sessions


    if not db_path.exists():
        return cli_sessions

    # Memoize the cron project ID for this scan so we don't pay a lock-acquire +
    # disk-read of projects.json per cron session in the loop below.
    # Resolved lazily on the first cron session we encounter.
    # [resolved, project_id_or_None] — a plain `[None]` sentinel can't tell
    # "not yet resolved" apart from "resolved to None" (the gated-closed
    # case), which would re-pay the load_projects() read on every cron row
    # in a cron-heavy zero-user-project scan — the exact I/O blowup #4842
    # fixed, reintroduced by this gate if left as a bare None check.
    _cron_pid_cache: list = [False, None]
    def _cron_pid():
        if not _cron_pid_cache[0]:
            _cron_pid_cache[0] = True
            _cron_pid_cache[1] = ensure_cron_project(create=_profile_has_user_projects())
        return _cron_pid_cache[1]

    # Memoize the cron jobs.json job_id -> name map for this scan. The two row
    # loops below each looked up a cron job's friendly name by re-reading and
    # re-parsing hermes_home/cron/jobs.json PER untitled cron row — up to ~200
    # full-file JSON parses on a cron-heavy profile (#4842). Parse it once,
    # lazily, on the first untitled cron row we hit. {} when absent/unreadable.
    _cron_job_names_cache: list = [None]  # list-as-cell; None = not yet resolved
    def _cron_job_names():
        if _cron_job_names_cache[0] is None:
            names: dict[str, str] = {}
            try:
                _jobs_path = hermes_home / 'cron' / 'jobs.json'
                if _jobs_path.exists():
                    _jobs_data = json.loads(_jobs_path.read_text(encoding='utf-8'))
                    for _j in _jobs_data.get('jobs', []):
                        _jid = _j.get('id')
                        _jname = _j.get('name')
                        if _jid and _jname:
                            names[str(_jid)] = _jname
            except Exception:
                pass  # degrade gracefully — fall back to the generic title
            _cron_job_names_cache[0] = names
        return _cron_job_names_cache[0]

    def _cron_title_from_jobs(sid: str):
        """Friendly cron job name for a cron_{job_id}_{ts} sid, or None."""
        if not sid.startswith('cron_'):
            return None
        parts = sid.split('_')
        if len(parts) < 3:
            return None
        return _cron_job_names().get(parts[1])

    # get_last_workspace() reads up to two files + an is_dir()/remote probe and
    # returns the SAME active workspace for every projected row, so calling it
    # per row was redundant I/O on the cold sidebar build (#4842; mirrors the
    # #4718 hoist on the Claude Code path). Resolve it once for this scan.
    _cli_workspace_cache: list = [None]  # list-as-cell; None = not yet resolved
    def _cli_workspace():
        if _cli_workspace_cache[0] is None:
            _cli_workspace_cache[0] = str(get_last_workspace())
        return _cli_workspace_cache[0]

    _webhook_pid_cache: list[str | None] = [None]
    def _webhook_pid():
        if _webhook_pid_cache[0] is None:
            _webhook_pid_cache[0] = ensure_webhook_project()
        return _webhook_pid_cache[0]

    def _state_row_project_id(sid: str, source: str | None) -> str | None:
        if is_cron_session(sid, source):
            return _cron_pid()
        if is_webhook_session(sid, source):
            return _webhook_pid()
        return None

    profile_value = _cli_profile or 'default'
    # A deleted WebUI session is tombstoned (see _record_webui_deleted_session_tombstone)
    # so recovery/audit/claim treat it as gone. The sidebar's own state.db projection
    # must honor the same tombstone, or a deleted WebUI session reappears here as an
    # "Agent" ghost the moment non-WebUI sessions are shown (#5498, second path). Only
    # suppress genuine WebUI rows with no live sidecar — a re-created/re-imported sid
    # (live {sid}.json) always beats a stale tombstone.
    try:
        _deleted_webui_tombstone = _load_webui_deleted_session_tombstone()
    except Exception:
        _deleted_webui_tombstone = frozenset()
    for row in read_importable_agent_session_rows(
        db_path,
        limit=visible_session_limit if visible_session_limit is not None else (
            CRON_PROJECT_CHIP_LIMIT if source_filter == 'cron'
            else WEBHOOK_PROJECT_CHIP_LIMIT if source_filter == 'webhook'
            else CLI_VISIBLE_SESSION_LIMIT
        ),
        log=logger,
        exclude_sources=("cron", "webhook") if source_filter is None else None,
        include_sources=None if source_filter is None else (source_filter,),
    ):
        sid = row['id']
        raw_ts = row['last_activity'] or row['started_at']
        # Prefer the CLI session's own profile from the DB; fall back to
        # the active CLI profile so sidebar filtering works either way.
        profile = profile_value  # CLI DB has no profile column; use active profile

        _source = row['source'] or 'cli'
        # Honor the deleted-WebUI tombstone: a WebUI row the user deleted must
        # not resurface in this projection (the #5498 ghost). Live sidecar wins.
        if (
            _source == 'webui'
            and sid in _deleted_webui_tombstone
            and not (SESSION_DIR / f"{sid}.json").exists()
        ):
            continue
        _source_meta = normalize_agent_session_source(_source)
        _title = row['title']
        if not _title and _source == 'cron':
            # Look up the human-friendly cron job name (cron_{job_id}_{ts}) from
            # the once-parsed jobs.json map instead of re-reading the file here.
            _title = _cron_title_from_jobs(sid) or _title
        # If a WebUI JSON file exists for this session (e.g. previously
        # imported or renamed in the sidebar), prefer its UI-owned metadata over
        # the state.db projection. This keeps archived cron/tool/API runs hidden
        # even when all_sessions() omits the hidden sidecar and the state row is
        # re-injected from Hermes state.db (#4397).
        _sidecar_meta = _state_projection_sidecar_metadata(sid)
        if _sidecar_meta.get('title'):
            _title = _sidecar_meta['title']
        _archived = bool(_sidecar_meta.get('archived'))
        _display_title = _title or f'{_source.title()} Session'
        cli_sessions.append({
            'session_id': sid,
            'title': _display_title,
            'workspace': _cli_workspace(),
            'model': row['model'] or None,
            'message_count': row['message_count'] or row['actual_message_count'] or 0,
            'created_at': row['started_at'],
            'updated_at': raw_ts,
            'pinned': False,
            'archived': _archived,
            'project_id': _state_row_project_id(sid, _source),
            'profile': profile,
            'source_tag': _source,
            'raw_source': row.get('raw_source') or _source_meta.get('raw_source'),
            'user_id': row.get('user_id'),
            'chat_id': row.get('chat_id') or row.get('origin_chat_id'),
            'chat_type': row.get('chat_type'),
            'thread_id': row.get('thread_id'),
            'session_key': row.get('session_key'),
            'platform': row.get('platform'),
            'session_source': row.get('session_source') or _source_meta.get('session_source'),
            'source_label': row.get('source_label') or _source_meta.get('source_label'),
            'parent_session_id': row.get('parent_session_id'),
            'parent_title': row.get('parent_title'),
            'parent_source': row.get('parent_source'),
            'relationship_type': row.get('relationship_type'),
            '_parent_lineage_root_id': row.get('_parent_lineage_root_id'),
            'end_reason': row.get('end_reason'),
            'actual_message_count': row.get('actual_message_count'),
            'user_message_count': row.get('actual_user_message_count'),
            '_lineage_root_id': row.get('_lineage_root_id'),
            '_lineage_tip_id': row.get('_lineage_tip_id'),
            '_compression_segment_count': row.get('_compression_segment_count'),
            'is_cli_session': is_cli_session_row({**row, **_source_meta}),
        })

    if source_filter is not None:
        return cli_sessions

    # --- Second pass: fetch cron sessions that may have been squeezed out
    # of the default window by more-recent non-cron sessions.
    # The normal sidebar query caps at CLI_VISIBLE_SESSION_LIMIT (20) rows;
    # once 20 newer sessions exist, older cron runs vanish from the payload
    # before _include_project_hidden_background_sidebar_sessions can rescue
    # them (#3172).  A separate, higher-capped cron-only pass ensures they
    # stay addressable under their project chip.
    if cron_project_limit is not False:
        existing_sids = {s['session_id'] for s in cli_sessions}
        try:
            for row in read_importable_agent_session_rows(
                db_path,
                limit=cron_project_limit,
                log=logger,
                exclude_sources=None,
                include_sources=("cron",),
            ):
                sid = row['id']
                if sid in existing_sids:
                    continue
                _source = row['source'] or 'cli'
                if _source != 'cron':
                    continue
                raw_ts = row['last_activity'] or row['started_at']
                _title = row['title']
                if not _title:
                    # Friendly cron job name from the once-parsed jobs.json map.
                    _title = _cron_title_from_jobs(sid) or _title
                _sidecar_meta = _state_projection_sidecar_metadata(sid)
                if _sidecar_meta.get('title'):
                    _title = _sidecar_meta['title']
                _archived = bool(_sidecar_meta.get('archived'))
                _display_title = _title or 'Cron Session'
                cli_sessions.append({
                    'session_id': sid,
                    'title': _display_title,
                    'workspace': _cli_workspace(),
                    'model': row['model'] or None,
                    'message_count': row['message_count'] or row['actual_message_count'] or 0,
                    'created_at': row['started_at'],
                    'updated_at': raw_ts,
                    'pinned': False,
                    'archived': _archived,
                    'project_id': _cron_pid(),
                    'profile': profile_value,
                    'source_tag': 'cron',
                    'raw_source': row.get('raw_source'),
                    'user_id': row.get('user_id'),
                    'chat_id': row.get('chat_id') or row.get('origin_chat_id'),
                    'chat_type': row.get('chat_type'),
                    'thread_id': row.get('thread_id'),
                    'session_key': row.get('session_key'),
                    'platform': row.get('platform'),
                    'session_source': row.get('session_source'),
                    'source_label': row.get('source_label'),
                    'parent_session_id': row.get('parent_session_id'),
                    'parent_title': row.get('parent_title'),
                    'parent_source': row.get('parent_source'),
                    'relationship_type': row.get('relationship_type'),
                    '_parent_lineage_root_id': row.get('_parent_lineage_root_id'),
                    'end_reason': row.get('end_reason'),
                    'actual_message_count': row.get('actual_message_count'),
                    'user_message_count': row.get('actual_user_message_count'),
                    '_lineage_root_id': row.get('_lineage_root_id'),
                    '_lineage_tip_id': row.get('_lineage_tip_id'),
                    '_compression_segment_count': row.get('_compression_segment_count'),
                    'is_cli_session': is_cli_session_row(row),
                })
                existing_sids.add(sid)
        except Exception:
            logger.debug("Cron project-chip second pass failed", exc_info=True)

    # --- Second pass: fetch webhook sessions that may have been squeezed out
    # of the default window. They stay hidden from the default sidebar but must
    # remain addressable under the Webhooks project chip.
    if webhook_project_limit is not False:
        existing_sids = {s['session_id'] for s in cli_sessions}
        try:
            for row in read_importable_agent_session_rows(
                db_path,
                limit=webhook_project_limit,
                log=logger,
                exclude_sources=None,
                include_sources=("webhook",),
            ):
                sid = row['id']
                if sid in existing_sids:
                    continue
                _source = row['source'] or 'webhook'
                if _source != 'webhook':
                    continue
                _source_meta = normalize_agent_session_source(_source)
                raw_ts = row['last_activity'] or row['started_at']
                _title = row['title']
                _sidecar_meta = _state_projection_sidecar_metadata(sid)
                if _sidecar_meta.get('title'):
                    _title = _sidecar_meta['title']
                _archived = bool(_sidecar_meta.get('archived'))
                _display_title = _title or 'Webhook Session'
                cli_sessions.append({
                    'session_id': sid,
                    'title': _display_title,
                    'workspace': str(get_last_workspace()),
                    'model': row['model'] or None,
                    'message_count': row['message_count'] or row['actual_message_count'] or 0,
                    'created_at': row['started_at'],
                    'updated_at': raw_ts,
                    'pinned': False,
                    'archived': _archived,
                    'project_id': _webhook_pid(),
                    'profile': profile_value,
                    'source_tag': 'webhook',
                    'raw_source': row.get('raw_source') or _source_meta.get('raw_source'),
                    'user_id': row.get('user_id'),
                    'chat_id': row.get('chat_id') or row.get('origin_chat_id'),
                    'chat_type': row.get('chat_type'),
                    'thread_id': row.get('thread_id'),
                    'session_key': row.get('session_key'),
                    'platform': row.get('platform'),
                    'session_source': row.get('session_source') or _source_meta.get('session_source'),
                    'source_label': row.get('source_label') or _source_meta.get('source_label'),
                    'parent_session_id': row.get('parent_session_id'),
                    'parent_title': row.get('parent_title'),
                    'parent_source': row.get('parent_source'),
                    'relationship_type': row.get('relationship_type'),
                    '_parent_lineage_root_id': row.get('_parent_lineage_root_id'),
                    'end_reason': row.get('end_reason'),
                    'actual_message_count': row.get('actual_message_count'),
                    'user_message_count': row.get('actual_user_message_count'),
                    '_lineage_root_id': row.get('_lineage_root_id'),
                    '_lineage_tip_id': row.get('_lineage_tip_id'),
                    '_compression_segment_count': row.get('_compression_segment_count'),
                    'is_cli_session': is_cli_session_row({**row, **_source_meta}),
                })
                existing_sids.add(sid)
        except Exception:
            logger.debug("Webhook project-chip second pass failed", exc_info=True)

    return cli_sessions


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
