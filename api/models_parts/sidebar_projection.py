"""Sidebar filtering, discoverability, and refresh selection.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _hide_from_default_sidebar(session: dict, *, show_cron: bool = False, show_webhook: bool = False) -> bool:
    """Return True for internal/background sessions hidden from the default list."""
    sid = str(session.get('session_id') or '')
    source = (
        session.get('source_tag')
        or session.get('source')
        or session.get('raw_source')
        or session.get('session_source')
    )
    if not show_cron and (source == 'cron' or sid.startswith('cron_')):
        return True
    if not show_webhook and source == 'webhook':
        return True
    if bool(session.get('pre_compression_snapshot')):
        return not bool(session.get('_show_pre_compression_snapshot'))
    return False


def _sidebar_message_count(session: dict) -> int:
    for key in ('message_count', 'actual_message_count'):
        try:
            value = int(session.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _sidebar_lineage_root_id(session: dict, sessions_by_id: dict[str, dict]) -> str:
    sid = str(session.get('session_id') or '')
    explicit = str(session.get('_lineage_root_id') or '').strip()
    if explicit:
        return explicit
    relationship_type = str(session.get('relationship_type') or '').strip().lower()
    if relationship_type == 'child_session':
        return sid
    root = sid
    parent = session.get('parent_session_id')
    source = str(session.get('session_source') or '').strip().lower()
    seen = {sid}
    if source == 'fork':
        return root
    while parent and parent not in seen and parent in sessions_by_id:
        root = str(parent)
        seen.add(root)
        parent = sessions_by_id.get(root, {}).get('parent_session_id')
    return root


def _has_live_sidebar_state(session: dict) -> bool:
    return bool(
        session.get('active_stream_id')
        or session.get('has_pending_user_message')
        or session.get('pending_user_message')
    )


def _is_intentionally_background_sidebar_session(session: dict) -> bool:
    sid = str(session.get('session_id') or '')
    source = (
        session.get('source_tag')
        or session.get('source')
        or session.get('raw_source')
        or session.get('session_source')
    )
    return source in {'cron', 'webhook'} or sid.startswith('cron_')


def _include_project_hidden_background_sidebar_sessions(
    candidates: list[dict],
    visible: list[dict],
) -> list[dict]:
    """Keep project-assigned background sessions addressable by project chips.

    Cron and webhook sessions stay hidden from the default sidebar, but if they
    have a project assignment they must still be present in the client cache so
    their dedicated project chips can reveal them (#3019).
    """
    visible_ids = {
        str(session.get('session_id'))
        for session in visible
        if session.get('session_id')
    }
    out = list(visible)
    for session in candidates:
        sid = str(session.get('session_id') or '')
        if not sid or sid in visible_ids:
            continue
        if not _is_intentionally_background_sidebar_session(session):
            continue
        if not session.get('project_id'):
            continue
        if _sidebar_message_count(session) <= 0:
            continue
        row = dict(session)
        row['default_hidden'] = True
        out.append(row)
    return out


def _preserve_messageful_sidebar_discoverability(
    candidates: list[dict],
    visible: list[dict],
) -> list[dict]:
    """Keep at least one messageful row per non-background conversation visible.

    The normal sidebar filters intentionally hide empty drafts, cron/background
    rows, and duplicate pre-compression snapshots. They must not make the only
    messageful representative of a conversation disappear. If every visible row
    for a lineage was filtered out, rescue the best hidden messageful row and
    mark it so callers can surface or audit the degraded state.
    """
    sessions_by_id = {
        str(session.get('session_id')): session
        for session in candidates
        if session.get('session_id')
    }
    covered_roots = {
        _sidebar_lineage_root_id(session, sessions_by_id)
        for session in visible
        if _sidebar_message_count(session) > 0
    }
    visible_ids = {
        str(session.get('session_id'))
        for session in visible
        if session.get('session_id')
    }
    rescue_by_root: dict[str, dict] = {}
    for session in candidates:
        sid = str(session.get('session_id') or '')
        if not sid or sid in visible_ids:
            continue
        if _sidebar_message_count(session) <= 0:
            continue
        if _is_intentionally_background_sidebar_session(session):
            continue
        root = _sidebar_lineage_root_id(session, sessions_by_id)
        if root in covered_roots:
            continue
        current = rescue_by_root.get(root)
        if current is None or (
            _sidebar_message_count(session), _session_sort_timestamp(session)
        ) > (
            _sidebar_message_count(current), _session_sort_timestamp(current)
        ):
            rescued = dict(session)
            rescued['discoverability_warning'] = 'rescued_messageful_hidden_session'
            rescue_by_root[root] = rescued
    if not rescue_by_root:
        return visible
    rescued_rows = sorted(
        rescue_by_root.values(),
        key=lambda session: (session.get('pinned', False), _session_sort_timestamp(session)),
        reverse=True,
    )
    return visible + rescued_rows


def _prefer_fuller_snapshots_for_sidebar(sessions: list[dict]) -> list[dict]:
    """Expose a hidden snapshot when it is the fuller transcript for a lineage.

    Pre-compression snapshots are normally hidden so archived compression
    segments do not duplicate the current continuation in the sidebar. If a
    snapshot row has more messages than the visible continuation for the same
    lineage, hiding it makes the conversation look truncated. In that case,
    show the fuller snapshot and suppress the shorter inactive continuation.
    """
    sessions_by_id = {
        str(session.get('session_id')): session
        for session in sessions
        if session.get('session_id')
    }
    groups: dict[str, list[dict]] = {}
    for session in sessions:
        sid = str(session.get('session_id') or '')
        source = session.get('source_tag') or session.get('source')
        if source == 'cron' or sid.startswith('cron_'):
            continue
        root = _sidebar_lineage_root_id(session, sessions_by_id)
        groups.setdefault(root, []).append(session)

    snapshot_ids_to_show: set[str] = set()
    continuation_ids_to_hide: set[str] = set()
    for group in groups.values():
        visible = [session for session in group if not session.get('pre_compression_snapshot')]
        snapshots = [session for session in group if session.get('pre_compression_snapshot')]
        if not visible or not snapshots:
            continue
        if any(_has_live_sidebar_state(session) for session in visible):
            continue

        best_visible_count = max(_sidebar_message_count(session) for session in visible)
        best_snapshot = max(
            snapshots,
            key=lambda session: (_sidebar_message_count(session), _session_sort_timestamp(session)),
        )
        if _sidebar_message_count(best_snapshot) <= best_visible_count:
            continue

        newest_visible_ts = max(_session_sort_timestamp(session) for session in visible)
        snapshot_ts = _session_sort_timestamp(best_snapshot)
        snapshot_id = str(best_snapshot.get('session_id') or '')
        if not snapshot_id:
            continue

        snapshot_ids_to_show.add(snapshot_id)
        # If the continuation is newer, keep it visible too. That means the
        # lineage is split-brain-ish: the snapshot has more transcript rows, but
        # the continuation may still contain the newest post-compression turn.
        # Showing both is less tidy than hiding one, but it preserves every
        # reachable message. Tidy and wrong is how users start doubting reality.
        if newest_visible_ts > snapshot_ts:
            continue

        messageful_visible = [
            session for session in visible
            if _sidebar_message_count(session) > 0
        ]
        if len(messageful_visible) > 1:
            continue

        continuation_ids_to_hide.update(
            str(session.get('session_id'))
            for session in visible
            if session.get('session_id')
        )

    if not snapshot_ids_to_show and not continuation_ids_to_hide:
        return sessions

    out = []
    for session in sessions:
        sid = str(session.get('session_id') or '')
        if sid in continuation_ids_to_hide:
            continue
        if sid in snapshot_ids_to_show:
            session = dict(session)
            session['_show_pre_compression_snapshot'] = True
        out.append(session)
    return out


def _strip_sidebar_internal_flags(sessions: list[dict]) -> None:
    for session in sessions:
        session.pop('_show_pre_compression_snapshot', None)


def _looks_like_stale_zero_message_row(session: dict) -> bool:
    """Return True for indexed rows that likely need sidecar metadata repair."""
    return bool(
        int(session.get('message_count') or 0) == 0
        and int(session.get('user_message_count') or 0) > 0
    )


def _row_may_need_sidecar_metadata_refresh(
    session: dict,
    *,
    stale_snapshot_ids: set[str] | None = None,
) -> bool:
    """Return True when a row needs canonical sidecar runtime/snapshot metadata.

    Compression lineage fields are enriched from state.db in one batched query
    later in all_sessions(). Loading hundreds of lineage sidecars on every
    /api/sessions poll turns the sidebar into molasses, so keep this refresh
    limited to rows with transient runtime state, missing snapshot sidebar
    metadata, or a stale snapshot candidate that can affect the visibility
    decision for its lineage.
    """
    is_runtime_row = bool(
        session.get('active_stream_id')
        or session.get('has_pending_user_message')
        or session.get('pending_user_message')
    )
    if is_runtime_row:
        return True
    sid = str(session.get('session_id') or '')
    if not session.get('pre_compression_snapshot'):
        # Refresh a stale-indexed COMPRESSION CONTINUATION row from its sidecar.
        # Gate tightly: a plain /branch fork also carries parent_session_id
        # (#1342) but has no compression sidecar drift to correct, and its file
        # mtime routinely exceeds the indexed logical last_message_at — so
        # including forks here would call load_metadata_only() on every fork row
        # on every /api/sessions poll (the molasses #3770 guards against, per the
        # #3789 release gate). Exclude session_source == 'fork'
        # (the marker /api/session/branch stamps; see _is_continuation_session)
        # so only true continuations are eligible.
        if str(session.get('session_source') or '').strip().lower() == 'fork':
            return False
        if session.get('message_count') is None or session.get('last_message_at') is None:
            return True
        # Lineage fields are enriched from state.db in a batched pass later in
        # all_sessions(). A complete indexed lineage row must not be reloaded
        # from its sidecar merely because the filesystem mtime is newer than the
        # logical message timestamp; that pattern is common after compression
        # and turns each /api/sessions poll into hundreds of JSON prefix scans.
        # Keep the mtime repair path only for rows whose counters are known bad
        # or incomplete enough that the index cannot be trusted.
        lineage_shaped = bool(
            session.get('parent_session_id')
            or session.get('_lineage_root_id')
            or session.get('_compression_segment_count')
        )
        needs_mtime_check = bool(
            sid
            and (
                _looks_like_stale_zero_message_row(session)
                or (lineage_shaped and session.get('user_message_count') is None)
            )
        )
        if needs_mtime_check and _sidecar_mtime_after_index_timestamp(session):
            return True
        return False
    if (
        sid
        and _looks_like_stale_zero_message_row(session)
        and str(session.get('session_source') or '').strip().lower() != 'fork'
        and _sidecar_mtime_after_index_timestamp(session)
    ):
        return True
    if session.get('message_count') is None or session.get('last_message_at') is None:
        return True
    return bool(sid and stale_snapshot_ids and sid in stale_snapshot_ids)


def _sidecar_mtime_after_index_timestamp(session: dict) -> bool:
    sid = str(session.get('session_id') or '')
    if not sid or not is_safe_session_id(sid):
        return False
    try:
        sidecar_mtime = (SESSION_DIR / f'{sid}.json').stat().st_mtime
    except OSError:
        return False
    indexed_ts = _session_sort_timestamp(session)
    return sidecar_mtime > indexed_ts + 0.001


def _stale_snapshot_metadata_refresh_ids(sessions: list[dict]) -> set[str]:
    """Return pre-compression snapshots worth a sidecar metadata refresh.

    Most snapshot rows can be decided from the index: either their indexed count
    already beats the visible continuation, or they are normal older snapshots
    that should remain hidden. Only stat candidate sidecars when a hidden
    snapshot has a visible continuation in the same lineage and its indexed
    metadata would otherwise fail to expose it.
    """
    sessions_by_id = {
        str(session.get('session_id')): session
        for session in sessions
        if session.get('session_id')
    }
    groups: dict[str, list[dict]] = {}
    for session in sessions:
        sid = str(session.get('session_id') or '')
        source = session.get('source_tag') or session.get('source')
        if source == 'cron' or sid.startswith('cron_'):
            continue
        root = _sidebar_lineage_root_id(session, sessions_by_id)
        groups.setdefault(root, []).append(session)

    refresh_ids: set[str] = set()
    for group in groups.values():
        visible = [session for session in group if not session.get('pre_compression_snapshot')]
        snapshots = [session for session in group if session.get('pre_compression_snapshot')]
        if not visible or not snapshots:
            continue
        if any(_has_live_sidebar_state(session) for session in visible):
            continue
        best_visible_count = max(_sidebar_message_count(session) for session in visible)
        for snapshot in snapshots:
            sid = str(snapshot.get('session_id') or '')
            if not sid:
                continue
            if _sidebar_message_count(snapshot) > best_visible_count:
                continue
            # Modern index rows already carry enough sidebar summary data to
            # decide snapshot visibility. Only legacy/incomplete rows need the
            # sidecar mtime rescue; otherwise every historical snapshot whose
            # file mtime is newer than its logical timestamp is re-read on every
            # sidebar poll. Treat stale-zero-message rows as incomplete even
            # when user_message_count/last_message_at are present; their sidecar
            # may hold the real count that makes the snapshot visible.
            if (
                snapshot.get('user_message_count') is not None
                and int(snapshot.get('message_count') or 0) > 0
                and snapshot.get('last_message_at') is not None
            ):
                continue
            if _sidecar_mtime_after_index_timestamp(snapshot):
                refresh_ids.add(sid)
    return refresh_ids


def _refresh_index_rows_from_sidecar_metadata(
    sessions: list[dict],
    *,
    index_message_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Overlay fuller sidecar metadata onto stale sidebar index rows.

    ``_index.json`` is a cache and can lag behind the canonical session sidecar
    during compression/continuation writes. Keep this read-only and limited to
    lineage/runtime-shaped rows so ordinary sidebar refreshes do not scan every
    historical transcript.
    """
    out: list[dict] = []
    stale_snapshot_ids = _stale_snapshot_metadata_refresh_ids(sessions)
    for session in sessions:
        if not _row_may_need_sidecar_metadata_refresh(
            session,
            stale_snapshot_ids=stale_snapshot_ids,
        ):
            out.append(session)
            continue
        sid = session.get('session_id')
        if not sid:
            out.append(session)
            continue
        sidecar = Session.load_metadata_only(
            sid,
            index_message_counts=index_message_counts,
        )
        if not sidecar:
            out.append(session)
            continue
        compact = sidecar.compact(include_runtime=True)
        refreshed = dict(session)
        for key in (
            'message_count', 'updated_at', 'last_message_at', 'title', 'workspace',
            'model', 'model_provider', 'created_at', 'pinned', 'archived', 'project_id',
            'profile', 'pre_compression_snapshot', 'parent_session_id', 'source_tag',
            'raw_source', 'session_source', 'source_label', 'active_stream_id',
            'has_pending_user_message', 'pending_user_message', 'pending_started_at',
        ):
            value = compact.get(key)
            if value is not None:
                refreshed[key] = value
        try:
            refreshed['message_count'] = max(
                int(session.get('message_count') or 0),
                int(compact.get('message_count') or 0),
            )
        except (TypeError, ValueError):
            pass
        if _session_sort_timestamp(compact) > _session_sort_timestamp(session):
            refreshed['updated_at'] = compact.get('updated_at', refreshed.get('updated_at'))
            refreshed['last_message_at'] = compact.get('last_message_at', refreshed.get('last_message_at'))
        out.append(refreshed)
    return out

__all__ = ['_hide_from_default_sidebar', '_sidebar_message_count', '_sidebar_lineage_root_id', '_has_live_sidebar_state', '_is_intentionally_background_sidebar_session', '_include_project_hidden_background_sidebar_sessions', '_preserve_messageful_sidebar_discoverability', '_prefer_fuller_snapshots_for_sidebar', '_strip_sidebar_internal_flags', '_looks_like_stale_zero_message_row', '_row_may_need_sidecar_metadata_refresh', '_sidecar_mtime_after_index_timestamp', '_stale_snapshot_metadata_refresh_ids', '_refresh_index_rows_from_sidecar_metadata']
