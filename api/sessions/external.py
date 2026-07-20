"""Read-only projections of CLI, Claude Code, cron, and webhook sessions."""

from __future__ import annotations

import collections
import copy
import datetime
import hashlib
import inspect
import json
import logging
import os
import threading
import time
from contextlib import closing
from pathlib import Path

from api.agent_sessions import (
    _is_continuation_session,
    is_cli_session_row,
    normalize_agent_session_source,
    open_state_db_readonly,
    read_importable_agent_session_rows,
)
from api.config import HOME, SESSION_DIR, SESSION_INDEX_FILE
from api.workspace import get_last_workspace
from .message_identity import _session_message_visible_key
from .projects import (
    _profile_has_user_projects,
    ensure_cron_project,
    ensure_webhook_project,
    is_cron_session,
    is_webhook_session,
)
from .state_db import _active_state_db_path
from .records import (
    Session,
    _active_stream_ids,
    _load_webui_deleted_session_tombstone,
    is_safe_session_id,
)
from .process_wakeup import _get_profile_home

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
_CLAUDE_CODE_PARSE_CACHE_LOCK = threading.Lock()
_CLAUDE_CODE_PARSE_CACHE: collections.OrderedDict[tuple, tuple] = collections.OrderedDict()
_CLAUDE_CODE_PARSE_CACHE_MAX = 1000
_SIDECAR_METADATA_CACHE_LOCK = threading.Lock()
_SIDECAR_METADATA_CACHE: collections.OrderedDict[tuple, dict] = collections.OrderedDict()
_SIDECAR_METADATA_CACHE_MAX = 2000

CLAUDE_CODE_SOURCE = 'claude_code'
CLAUDE_CODE_SOURCE_LABEL = 'Claude Code'
CLAUDE_CODE_MAX_FILES = 200
CLAUDE_CODE_MAX_FILE_BYTES = 10 * 1024 * 1024
CLAUDE_CODE_MAX_MESSAGES_PER_FILE = 1000
CLAUDE_CODE_MAX_CONTENT_CHARS = 200_000


def _normalize_cli_session_source_filter(source_filter) -> str | None:
    normalized = str(source_filter or '').strip().lower()
    if not normalized or normalized in {'all', 'any', '*'}:
        return None
    if normalized == 'claude-code':
        return CLAUDE_CODE_SOURCE
    return normalized


def _default_claude_code_projects_dir() -> Path | None:
    """Resolve the Claude Code projects directory without touching real home in tests."""
    override = os.getenv('HERMES_WEBUI_CLAUDE_PROJECTS_DIR')
    if override:
        return Path(override).expanduser()
    if os.getenv('HERMES_WEBUI_TEST_STATE_DIR'):
        return None
    return Path.home() / '.claude' / 'projects'


def _claude_code_session_id(path: Path) -> str:
    digest = hashlib.sha256(str(path.expanduser().resolve()).encode('utf-8')).hexdigest()[:24]
    return f'{CLAUDE_CODE_SOURCE}_{digest}'


def _parse_claude_code_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def _extract_claude_code_text(content) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content[:CLAUDE_CODE_MAX_CONTENT_CHARS]
    if isinstance(content, list):
        parts = []
        used = 0
        for item in content:
            text = ''
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get('text') or item.get('content') or ''
            if not text:
                continue
            text = str(text)
            remaining = CLAUDE_CODE_MAX_CONTENT_CHARS - used
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            used += len(parts[-1])
        return '\n'.join(parts)
    if isinstance(content, dict):
        return _extract_claude_code_text(content.get('text') or content.get('content'))
    return str(content)[:CLAUDE_CODE_MAX_CONTENT_CHARS]


def _parse_claude_code_jsonl(path: Path, *, max_messages: int = CLAUDE_CODE_MAX_MESSAGES_PER_FILE) -> tuple[list[dict], str | None, float | None, float | None]:
    messages: list[dict] = []
    summary_title = None
    first_ts = None
    last_ts = None
    try:
        with path.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if len(messages) >= max_messages:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                if not summary_title:
                    summary = raw.get('summary') or raw.get('title')
                    if isinstance(summary, str) and summary.strip():
                        summary_title = ' '.join(summary.split())[:80]
                records = raw.get('messages') if isinstance(raw.get('messages'), list) else None
                if records is None:
                    records = [raw.get('message') if isinstance(raw.get('message'), dict) else raw]
                for record in records:
                    if len(messages) >= max_messages:
                        break
                    if not isinstance(record, dict):
                        continue
                    msg = record.get('message') if isinstance(record.get('message'), dict) else record
                    role = str(msg.get('role') or record.get('role') or raw.get('role') or raw.get('type') or '').strip().lower()
                    if role == 'human':
                        role = 'user'
                    if role not in {'user', 'assistant', 'system', 'tool'}:
                        continue
                    content = _extract_claude_code_text(msg.get('content') if 'content' in msg else record.get('content'))
                    if not content.strip():
                        continue
                    ts = _parse_claude_code_timestamp(
                        msg.get('timestamp')
                        or record.get('timestamp')
                        or raw.get('timestamp')
                        or raw.get('created_at')
                    )
                    if ts is not None:
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        last_ts = ts if last_ts is None else max(last_ts, ts)
                    item = {'role': role, 'content': content}
                    if ts is not None:
                        item['timestamp'] = ts
                    messages.append(item)
    except Exception:
        return [], None, None, None
    return messages, summary_title, first_ts, last_ts


def _parse_claude_code_jsonl_cached(
    path: Path, *, max_messages: int = CLAUDE_CODE_MAX_MESSAGES_PER_FILE
) -> tuple[list[dict], str | None, float | None, float | None]:
    """``_parse_claude_code_jsonl`` memoized by the file's (path, mtime_ns, size, ctime_ns).

    The transcript files under ``~/.claude/projects`` are global and rarely
    change between sidebar builds, but parsing them dominates the cold
    /api/sessions latency (and repeats on every profile switch). Caching the
    parse result keyed by the file's stat signature collapses the warm cost to a
    single ``os.stat`` per file. A genuine append/edit bumps ``mtime_ns``/``size``
    /``ctime_ns`` and misses the cache, so staleness is impossible without
    re-parsing.

    ``max_messages`` is part of the key so a caller asking for a different cap
    never reads a result truncated to a smaller one.
    """
    try:
        st = path.stat()
        # Key on mtime_ns + size + ctime_ns: size is the strong discriminator for
        # append-only JSONL (any write changes it), and ctime_ns guards the rare
        # same-size, same-mtime in-place edit so a content change can never serve
        # a stale parse. A spurious ctime bump only costs one harmless re-parse.
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ctime_ns, int(max_messages))
    except OSError:
        # Can't stat -> fall back to a direct (uncached) parse; it will also
        # likely fail and return the empty tuple, matching prior behavior.
        return _parse_claude_code_jsonl(path, max_messages=max_messages)

    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        hit = _CLAUDE_CODE_PARSE_CACHE.get(key)
        if hit is not None:
            _CLAUDE_CODE_PARSE_CACHE.move_to_end(key)
            messages, summary_title, first_ts, last_ts = hit
            # Return a shallow copy of the message list so a caller mutating it
            # can't corrupt the cached entry; the per-message dicts are treated
            # as read-only by all current callers.
            return list(messages), summary_title, first_ts, last_ts

    parsed = _parse_claude_code_jsonl(path, max_messages=max_messages)

    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        # Re-check under lock in case a concurrent build populated it; either
        # entry is equally valid for the same stat signature.
        existing = _CLAUDE_CODE_PARSE_CACHE.get(key)
        if existing is None:
            _CLAUDE_CODE_PARSE_CACHE[key] = parsed
            _CLAUDE_CODE_PARSE_CACHE.move_to_end(key)
            while len(_CLAUDE_CODE_PARSE_CACHE) > _CLAUDE_CODE_PARSE_CACHE_MAX:
                _CLAUDE_CODE_PARSE_CACHE.popitem(last=False)
    messages, summary_title, first_ts, last_ts = parsed
    return list(messages), summary_title, first_ts, last_ts


def clear_claude_code_parse_cache() -> None:
    """Drop all memoized Claude Code transcript parses (test/lifecycle hook)."""
    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        _CLAUDE_CODE_PARSE_CACHE.clear()


def _iter_claude_code_jsonl_files(projects_dir: Path | str | None = None, *, max_files: int = CLAUDE_CODE_MAX_FILES, max_file_bytes: int = CLAUDE_CODE_MAX_FILE_BYTES):
    root = Path(projects_dir).expanduser() if projects_dir is not None else _default_claude_code_projects_dir()
    if root is None:
        return
    try:
        if root.is_symlink():
            return
        root = root.resolve(strict=False)
        if not root.exists() or not root.is_dir():
            return
        yielded = 0
        for project_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if yielded >= max_files:
                return
            try:
                if project_dir.is_symlink() or not project_dir.is_dir():
                    continue
                for path in sorted(project_dir.iterdir(), key=lambda p: p.name):
                    if yielded >= max_files:
                        return
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() != '.jsonl':
                        continue
                    try:
                        if path.stat().st_size > max_file_bytes:
                            continue
                    except OSError:
                        continue
                    yielded += 1
                    yield path
            except OSError:
                continue
    except OSError:
        return


def _claude_code_title(messages: list[dict], summary_title: str | None) -> str:
    if summary_title:
        return summary_title
    for msg in messages:
        if msg.get('role') == 'user':
            text = ' '.join(str(msg.get('content') or '').split())
            if text:
                return text[:80]
    return 'Claude Code Session'


def get_claude_code_sessions(projects_dir: Path | str | None = None, *, max_files: int = CLAUDE_CODE_MAX_FILES, max_file_bytes: int = CLAUDE_CODE_MAX_FILE_BYTES) -> list:
    """Read Claude Code JSONL sessions as read-only external-agent rows.

    The bridge is additive and defensive: it skips symlinks, oversized files,
    malformed lines, and per-file errors rather than crashing WebUI session
    listing. Tests pass ``projects_dir`` fixtures so Michael's real ~/.claude is
    never read during test runs.
    """
    sessions = []
    # ``get_last_workspace()`` is loop-invariant (the same active workspace for
    # every Claude Code row) but internally stats config.yaml + probes terminal
    # cwd, so calling it once per row was ~200 redundant stat()s on the cold
    # sidebar build (#4718). Resolve it a single time.
    cc_workspace = str(get_last_workspace())
    for path in _iter_claude_code_jsonl_files(projects_dir, max_files=max_files, max_file_bytes=max_file_bytes) or []:
        messages, summary_title, first_ts, last_ts = _parse_claude_code_jsonl_cached(path)
        if not messages:
            continue
        sid = _claude_code_session_id(path)
        # Match the truthiness fallback used in the assignments below: the old
        # inline code was ``first_ts or last_ts or path.stat().st_mtime``, which
        # also fell back to mtime for a falsy-but-not-None ``0.0`` timestamp
        # (epoch-0 / 1970 transcripts). An identity (``is None``) guard would
        # leave those rows with ``None`` instead of the file mtime, so use the
        # same ``not`` test the assignments use to stay bug-for-bug compatible.
        if not first_ts and not last_ts:
            try:
                _mtime = path.stat().st_mtime
            except OSError:
                _mtime = 0.0
        else:
            _mtime = None
        created_at = first_ts or last_ts or _mtime
        updated_at = last_ts or first_ts or _mtime
        sessions.append({
            'session_id': sid,
            'title': _claude_code_title(messages, summary_title),
            'workspace': cc_workspace,
            'model': 'claude-code',
            'message_count': len(messages),
            'created_at': created_at,
            'updated_at': updated_at,
            'last_message_at': updated_at,
            'pinned': False,
            'archived': False,
            'project_id': None,
            'profile': None,
            'source_tag': CLAUDE_CODE_SOURCE,
            'raw_source': CLAUDE_CODE_SOURCE,
            'session_source': 'external_agent',
            'source_label': CLAUDE_CODE_SOURCE_LABEL,
            'is_cli_session': True,
            'read_only': True,
        })
    sessions.sort(key=lambda s: s.get('last_message_at') or s.get('updated_at') or 0, reverse=True)
    return sessions


def get_claude_code_session_messages(sid, projects_dir: Path | str | None = None) -> list:
    """Return messages for one read-only Claude Code JSONL session."""
    sid = str(sid or '')
    if not sid.startswith(f'{CLAUDE_CODE_SOURCE}_'):
        return []
    for path in _iter_claude_code_jsonl_files(projects_dir) or []:
        if _claude_code_session_id(path) != sid:
            continue
        messages, _summary_title, _first_ts, _last_ts = _parse_claude_code_jsonl_cached(path)
        return messages
    return []


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


def _json_loads_if_string(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return value


def get_state_db_session_messages(
    sid,
    *,
    stitch_continuations: bool = False,
    profile=None,
    since_timestamp=None,
    include_inactive: bool = False,
    limit=None,
) -> list:
    """Read messages for a Hermes session from state.db.

    When *profile* is supplied, reads from that profile's state.db; otherwise
    falls back to the active profile's state.db.  This generic reader works for
    any session source, including WebUI-origin sessions that were later updated
    through another Hermes surface such as the Gateway API Server.  When
    ``stitch_continuations`` is true it preserves the historical CLI/external-agent
    behavior of walking compatible compression/close parent segments before reading
    messages.

    ``since_timestamp`` is an optional display-path optimization.  It limits the
    raw state.db scan to rows at or after a sidecar-derived timestamp floor while
    preserving the caller's normal merge/window logic.  Full-history callers must
    leave it unset.

    ``limit`` is an optional defensive row cap (applied after ORDER BY as a SQL
    LIMIT). It is a BACKSTOP against a pathological/huge state.db materializing
    unbounded rows into a Python list, NOT a semantic window: the display path
    counts visible rows post-reconciliation, so a true window LIMIT here would
    corrupt the sidecar/state.db merge (see _state_db_since_timestamp_for_limited_display,
    which deliberately does NOT SQL-LIMIT raw rows for that reason). Callers that
    need the full history for model-context reconstruction leave this unset.

    When the messages table exposes an ``active`` column, inactive rows are
    compacted/archived history and are intentionally excluded by default. WebUI
    reconciliation feeds this reader straight into the next model context; pulling
    ``active=0`` archive rows back in resurrects pre-compaction history and can
    make every later turn re-trigger compression. Pass ``include_inactive=True``
    only for explicit recovery/audit views.
    """
    try:
        import sqlite3
    except ImportError:
        return []

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return []

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            required = {'role', 'content', 'timestamp'}
            if not required.issubset(available):
                return []
            optional = [
                'tool_call_id',
                'tool_calls',
                'tool_name',
                'reasoning',
                'reasoning_details',
                'codex_reasoning_items',
                'reasoning_content',
                'codex_message_items',
            ]
            id_col = ['id'] if 'id' in available else []
            selected = id_col + ['role', 'content', 'timestamp'] + [c for c in optional if c in available]

            session_chain = [str(sid)]
            if stitch_continuations:
                cur.execute("PRAGMA table_info(sessions)")
                session_cols = {str(row['name']) for row in cur.fetchall()}
                if {'parent_session_id', 'end_reason', 'started_at', 'source'}.issubset(session_cols):
                    cur.execute(
                        """
                        SELECT id, source, started_at, parent_session_id, ended_at, end_reason
                        FROM sessions
                        WHERE id = ?
                        """,
                        (sid,),
                    )
                    rows_by_id = {}
                    row = cur.fetchone()
                    if row:
                        rows_by_id[str(row['id'])] = dict(row)
                        current_id = str(row['id'])
                        seen = {current_id}
                        for _ in range(20):
                            current = rows_by_id.get(current_id)
                            parent_id = current.get('parent_session_id') if current else None
                            if not parent_id or parent_id in seen:
                                break
                            cur.execute(
                                """
                                SELECT id, source, started_at, parent_session_id, ended_at, end_reason
                                FROM sessions
                                WHERE id = ?
                                """,
                                (parent_id,),
                            )
                            parent_row = cur.fetchone()
                            if not parent_row:
                                break
                            parent_dict = dict(parent_row)
                            rows_by_id[str(parent_row['id'])] = parent_dict
                            if not _is_continuation_session(parent_dict, current):
                                break
                            session_chain.insert(0, str(parent_row['id']))
                            current_id = str(parent_row['id'])
                            seen.add(current_id)

            placeholders = ', '.join('?' for _ in session_chain)
            params = list(session_chain)
            since_clause = ""
            if since_timestamp is not None:
                try:
                    since_ts = float(since_timestamp)
                except (TypeError, ValueError):
                    since_ts = None
                if since_ts is not None:
                    since_clause = " AND (timestamp IS NULL OR timestamp >= ?)"
                    params.append(since_ts)
            active_clause = ""
            if 'active' in available and not include_inactive:
                active_clause = " AND (active IS NULL OR active != 0)"
            # Defensive row cap (backstop only — see docstring). Applied as a
            # SQL LIMIT bound parameter (?) so the tail (newest) rows are
            # retained and a pathological state.db can't materialize unbounded
            # rows. None = unchanged full-history read for model-context callers.
            limit_clause = ""
            if limit is not None:
                try:
                    limit_int = max(1, int(limit))
                except (TypeError, ValueError):
                    limit_int = None
                if limit_int is not None:
                    # The query orders ASC (oldest first); to keep the NEWEST
                    # rows under the cap, take a descending-ordered subquery and
                    # re-sort ascending — a plain LIMIT would keep the oldest.
                    limit_clause = " ORDER BY timestamp DESC, id DESC LIMIT ?"
                    params.append(limit_int)
            if limit_clause:
                cur.execute(f"""
                    SELECT * FROM (
                        SELECT {', '.join(selected)}, session_id
                        FROM messages
                        WHERE session_id IN ({placeholders})
                        {since_clause}
                        {active_clause}
                        {limit_clause}
                    ) ORDER BY timestamp ASC, id ASC
                """, params)
            else:
                cur.execute(f"""
                    SELECT {', '.join(selected)}, session_id
                    FROM messages
                    WHERE session_id IN ({placeholders})
                    {since_clause}
                    {active_clause}
                    ORDER BY timestamp ASC, id ASC
                """, params)
            msgs = []
            for row in cur.fetchall():
                msg = {
                    'role': row['role'],
                    'content': row['content'],
                    'timestamp': row['timestamp'],
                }
                for col in optional:
                    if col not in row.keys():
                        continue
                    value = row[col]
                    if value in (None, ''):
                        continue
                    if col in {'tool_calls', 'reasoning_details', 'codex_reasoning_items', 'codex_message_items'}:
                        value = _json_loads_if_string(value)
                    msg[col] = value
                if msg.get('role') == 'tool' and msg.get('tool_name') and not msg.get('name'):
                    msg['name'] = msg['tool_name']
                msgs.append(msg)
    except Exception:
        return []
    return msgs


def get_state_db_session_message_prefix_summary(
    sid,
    before_timestamp,
    *,
    profile=None,
) -> dict | None:
    """Return prefix timestamp counts, or ``None`` when they cannot be proven.

    The projection intentionally avoids message content and tool-call columns so
    callers can reject impossible prefix matches before materializing visible
    identities. Missing databases are an authoritative empty prefix and are not
    created by this read path.
    """
    try:
        import sqlite3
    except ImportError:
        return None

    if not sid:
        return None
    try:
        before_ts = float(before_timestamp)
    except (TypeError, ValueError):
        return None

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return {"count": 0, "null_timestamp_count": 0}

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            if not {'session_id', 'timestamp'}.issubset(available):
                return None
            active_clause = ""
            if 'active' in available:
                active_clause = " AND (active IS NULL OR active != 0)"
            cur.execute(
                f"""
                SELECT
                    COUNT(CASE
                        WHEN timestamp IS NOT NULL AND timestamp < ? THEN 1
                    END) AS count,
                    COUNT(CASE WHEN timestamp IS NULL THEN 1 END) AS null_timestamp_count
                FROM messages
                WHERE session_id = ?
                {active_clause}
                """,
                (before_ts, str(sid)),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "count": int(row["count"]),
                "null_timestamp_count": int(row["null_timestamp_count"]),
            }
    except Exception:
        return None


def get_state_db_session_message_keys_before_timestamp(
    sid,
    before_timestamp,
    *,
    profile=None,
) -> list[tuple] | None:
    """Return visible-identity keys before ``before_timestamp`` in DB order.

    Missing timestamps are intentionally excluded because the bounded reader
    keeps them with ``timestamp IS NULL OR timestamp >= ?``.  The caller uses
    this as a conservative prefix-identity guard before taking the optimized
    tail-read path, so schemas that cannot prove the merge-visible identity
    force a full read.
    """
    try:
        import sqlite3
    except ImportError:
        return None

    if not sid:
        return None
    try:
        before_ts = float(before_timestamp)
    except (TypeError, ValueError):
        return None

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return []

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            if not {'id', 'session_id', 'role', 'content', 'timestamp', 'tool_calls'}.issubset(available):
                return None
            cur.execute(
                """
                SELECT
                    COALESCE(role, '') AS role,
                    COALESCE(content, '') AS content,
                    tool_calls
                FROM messages
                WHERE session_id = ? AND timestamp IS NOT NULL AND timestamp < ?
                ORDER BY timestamp ASC, id ASC
                """,
                (str(sid), before_ts),
            )
            return [
                _session_message_visible_key(
                    {
                        "role": row["role"],
                        "content": row["content"],
                        "tool_calls": _json_loads_if_string(row["tool_calls"]),
                    }
                )
                for row in cur.fetchall()
            ]
    except Exception:
        return None


def get_state_db_session_summary(sid, *, profile=None) -> dict:
    """Return a cheap message count/timestamp summary for one state.db session."""
    try:
        import sqlite3
    except ImportError:
        return {"message_count": 0, "last_message_at": 0.0}

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not sid or not db_path.exists():
        return {"message_count": 0, "last_message_at": 0.0}

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            if 'session_id' not in available:
                return {"message_count": 0, "last_message_at": 0.0}
            if 'timestamp' in available:
                cur.execute(
                    "SELECT COUNT(*) AS message_count, MAX(timestamp) AS last_message_at "
                    "FROM messages WHERE session_id = ?",
                    (str(sid),),
                )
                row = cur.fetchone()
                if not row:
                    return {"message_count": 0, "last_message_at": 0.0}
                return {
                    "message_count": max(0, int(row["message_count"] or 0)),
                    "last_message_at": float(row["last_message_at"] or 0) if row["last_message_at"] is not None else 0.0,
                }
            cur.execute("SELECT COUNT(*) AS message_count FROM messages WHERE session_id = ?", (str(sid),))
            row = cur.fetchone()
            return {
                "message_count": max(0, int(row["message_count"] or 0)) if row else 0,
                "last_message_at": 0.0,
            }
    except Exception:
        return {"message_count": 0, "last_message_at": 0.0}
