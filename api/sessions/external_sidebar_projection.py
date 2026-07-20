"""Projection of CLI, cron, webhook, and Claude Code sidebar rows."""

from __future__ import annotations

import collections
import json
import logging
import threading
from pathlib import Path

from api.agent_sessions import (
    is_cli_session_row,
    normalize_agent_session_source,
    read_importable_agent_session_rows,
)
from api.config import SESSION_DIR
from api.workspace import get_last_workspace
from .claude_code import (
    CLAUDE_CODE_SOURCE,
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
    _load_webui_deleted_session_tombstone,
    is_safe_session_id,
)

logger = logging.getLogger(__name__)

CLI_VISIBLE_SESSION_LIMIT = 20
CRON_PROJECT_CHIP_LIMIT = 200
WEBHOOK_PROJECT_CHIP_LIMIT = 200
_SIDECAR_METADATA_CACHE_LOCK = threading.Lock()
_SIDECAR_METADATA_CACHE: collections.OrderedDict[tuple, dict] = collections.OrderedDict()
_SIDECAR_METADATA_CACHE_MAX = 2000


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
