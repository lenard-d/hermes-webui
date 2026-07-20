"""Sidebar override application and aggregate listing.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _apply_sidebar_state_db_overrides(sessions: list[dict]) -> None:
    """Apply state.db source/title overrides without full lineage enrichment.

    Source classification (source/title) is corrected for ALL rows because it
    feeds the CLI/WebUI sidebar filter that runs BEFORE the lazy lineage
    correction — capping it would silently drop rows whose stale JSON source
    disagrees with state.db (#5132 regression guard). Only the expensive
    ``messages`` count/last-message aggregation is capped to the top-N most
    recent (paint-priority) rows, which is what actually blocked /api/sessions
    for 5-18s on power users reading state.db for 2400+ rows on every
    concurrent poll (#5132). The cap is env-configurable and fails open; rows
    beyond it keep their JSON message-count/last-message until the history panel
    opens (lazily corrected, exactly as with the lineage cap #4638).
    """
    import os as _os
    try:
        _cap = int(_os.environ.get("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "300"))
    except (TypeError, ValueError):
        _cap = 300
    all_ids = {str(s.get('session_id')) for s in sessions if s.get('session_id')}
    if _cap > 0 and len(sessions) > _cap:
        count_ids = {str(s.get('session_id')) for s in sessions[:_cap] if s.get('session_id')}
    else:
        count_ids = None  # cap disabled / under cap -> count every row too
    try:
        metadata = _read_state_db_sidebar_overrides(
            _active_state_db_path(),
            all_ids,
            count_session_ids=count_ids,
        )
    except Exception:
        return
    _apply_sidebar_state_db_override_metadata(sessions, metadata)


def _apply_sidebar_state_db_override_metadata(sessions: list[dict], metadata: dict[str, dict]) -> None:
    for session in sessions:
        sid = session.get('session_id')
        if sid not in metadata:
            continue
        entry = dict(metadata[sid])
        state_db_title = entry.pop('_state_db_title', None)
        state_db_source = entry.pop('_state_db_source', None)
        state_db_source_tag = entry.pop('_state_db_source_tag', None)
        state_db_raw_source = entry.pop('_state_db_raw_source', None)
        state_db_session_source = entry.pop('_state_db_session_source', None)
        state_db_source_label = entry.pop('_state_db_source_label', None)
        state_db_message_count = entry.pop('_state_db_message_count', None)
        state_db_last_message_at = entry.pop('_state_db_last_message_at', None)
        state_db_display_title = entry.pop('_state_db_display_title', None)
        if state_db_source == 'webui':
            session['source_tag'] = state_db_source_tag
            session['raw_source'] = state_db_raw_source
            session['session_source'] = state_db_session_source
            session['source_label'] = state_db_source_label
            session['is_cli_session'] = False
        # Overlay the real state.db message count for WebUI-owned rows AND for
        # delegated subagent children (#5308). A subagent child
        # (state_db_source == 'subagent') is backed by the delegate runner's
        # state.db session, but its sidebar row is built from a stale sidecar
        # that often reports message_count == 0. Without overlaying the true
        # count, the front-end visibility predicate
        # (_sidebarRowHasVisibleMessages) drops the row and the subagent
        # session vanishes from the sidebar entirely (regression seam behind
        # #5308, same state.db-blind-metadata root as the #5307 transcript
        # recovery). The count overlay keeps the same conservative
        # anti-resurrection guard used for WebUI rows. The source-tag / title
        # reassignment above stays WebUI-only — a subagent child keeps its
        # subagent classification.
        if state_db_source in ('webui', 'subagent'):
            try:
                current_count = max(0, int(session.get('message_count') or 0))
                state_count = max(0, int(state_db_message_count or 0))
            except (TypeError, ValueError):
                current_count = 0
                state_count = 0
            try:
                current_last = max(
                    float(session.get('last_message_at') or 0),
                    float(session.get('updated_at') or 0),
                )
            except (TypeError, ValueError):
                current_last = 0.0
            try:
                state_last = float(state_db_last_message_at or 0)
            except (TypeError, ValueError):
                state_last = 0.0
            # ``current_last`` intentionally includes ``updated_at``: if a
            # sidecar metadata-only write happened after the state.db append,
            # keep the conservative anti-resurrection guard and wait for a
            # newer settled state.db message before overlaying counts again.
            if state_count > current_count and (state_last <= 0 or state_last > current_last):
                try:
                    existing_actual = max(0, int(session.get('actual_message_count') or 0))
                except (TypeError, ValueError):
                    existing_actual = 0
                session['message_count'] = state_count
                session['actual_message_count'] = max(state_count, existing_actual)
                if state_last > 0:
                    session['last_message_at'] = max(float(session.get('last_message_at') or 0), state_last)
                    session['updated_at'] = max(float(session.get('updated_at') or 0), state_last)
        title = session.get('title')
        if (
            state_db_title
            and state_db_title != title
            and _sidebar_title_is_generic_webui(title)
        ):
            session['_state_db_title'] = state_db_title
            session['display_title'] = state_db_title
        if (
            state_db_display_title
            and state_db_source == 'subagent'
            and ' '.join(str(title or '').split()) == 'Subagent Session'
        ):
            session['display_title'] = state_db_display_title


def _enrich_sidebar_lineage_metadata(sessions: list[dict]) -> None:
    """Attach state.db compression lineage metadata used by sidebar collapse.

    Cap the DB lookup to the top-N most recent sessions to bound wall-clock
    on power users with thousands of sessions. The sidebar paints chronologically
    newest first; older sessions almost never have visible lineage to collapse
    (parents are themselves stale and rarely surface in the same render).
    Lineage enrichment for those is loaded lazily when the user opens the
    history panel. Issue #38914 / 2026-06-21 triage: /api/sessions was spending
    4.9s on lineage_metadata across 2400+ rows.
    """
    # 2026-06-21: configurable via env to ease A/B and rollback without a redeploy.
    import os as _os
    try:
        _cap = int(_os.environ.get("HERMES_WEBUI_LINEAGE_TOP_N", "300"))
    except (TypeError, ValueError):
        _cap = 300
    if _cap > 0 and len(sessions) > _cap:
        candidates = sessions[:_cap]
    else:
        candidates = sessions
    try:
        metadata = read_session_lineage_metadata(
            _active_state_db_path(),
            {str(s.get('session_id')) for s in candidates if s.get('session_id')},
        )
    except Exception:
        return
    _apply_sidebar_state_db_override_metadata(sessions, metadata)
    for session in sessions:
        sid = session.get('session_id')
        if sid in metadata:
            entry = dict(metadata[sid])
            for key in (
                '_state_db_title',
                '_state_db_source',
                '_state_db_source_tag',
                '_state_db_raw_source',
                '_state_db_session_source',
                '_state_db_source_label',
            ):
                entry.pop(key, None)
            session.update(entry)


def _diag_stage(diag, name: str) -> None:
    if diag is not None:
        try:
            diag.stage(name)
        except Exception:
            pass


def all_sessions(diag=None, *, include_lineage_metadata: bool = True):
    _diag_stage(diag, "all_sessions.active_streams")
    active_stream_ids = _active_stream_ids()
    # Phase C: try index first for O(1) read; fall back to full scan
    _diag_stage(diag, "all_sessions.index_exists")
    if not SESSION_INDEX_FILE.exists():
        _diag_stage(diag, "all_sessions.start_index_rebuild")
        _start_session_index_rebuild_thread()
    if SESSION_INDEX_FILE.exists():
        try:
            _diag_stage(diag, "all_sessions.read_index")
            index = json.loads(SESSION_INDEX_FILE.read_bytes())
            _diag_stage(diag, "all_sessions.prune_index")
            with LOCK:
                in_memory_ids = set(SESSIONS.keys())
            persisted_ids = _persisted_session_ids_snapshot()
            if not index and _session_dir_has_persisted_session_files():
                raise ValueError("empty session index while session files exist")
            index = [
                s for s in index
                if (
                    str(s.get('session_id') or '') in in_memory_ids
                    or (
                        persisted_ids is not None
                        and str(s.get('session_id') or '') in persisted_ids
                    )
                    or (
                        persisted_ids is None
                        and _index_entry_exists(s.get('session_id'), in_memory_ids=in_memory_ids)
                    )
                )
            ]
            if not index and _session_dir_has_persisted_session_files():
                raise ValueError("session index has no live rows while session files exist")
            backfilled = []
            for i, s in enumerate(index):
                if 'last_message_at' not in s:
                    _diag_stage(diag, "all_sessions.backfill_load")
                    full = Session.load(s.get('session_id'))
                    if full:
                        index[i] = full.compact()
                        backfilled.append(full)
            if backfilled:
                try:
                    _diag_stage(diag, "all_sessions.backfill_write")
                    _write_session_index(updates=backfilled)
                except Exception:
                    logger.debug("Failed to persist last_message_at backfill")
            _diag_stage(diag, "all_sessions.mark_streaming")
            for s in index:
                s['is_streaming'] = _is_streaming_session(
                    s.get('active_stream_id'),
                    active_stream_ids,
                )
            # Overlay any in-memory sessions that may be newer than the index
            _diag_stage(diag, "all_sessions.overlay_lock")
            index_map = {s['session_id']: s for s in index}
            with LOCK:
                for s in SESSIONS.values():
                    index_map[s.session_id] = s.compact(
                        include_runtime=True,
                        active_stream_ids=active_stream_ids,
                    )
            missing_persisted_ids = []
            if persisted_ids is not None:
                indexed_ids = {str(sid) for sid in index_map.keys() if sid}
                missing_persisted_ids = sorted(
                    str(sid) for sid in persisted_ids
                    if sid and str(sid) not in indexed_ids
                )
            # #4985: the tombstone is intentionally NOT a blind-drop filter
            # on missing_persisted_ids. A tombstoned sid whose sidecar is
            # still on disk is recovered into the index here so the
            # post-recovery prune helper (``_prune_orphaned_webui_zero_message_sessions``
            # below) gets a chance to self-heal: if the row's state.db.messages
            # is still empty the helper leaves the tombstone in place (no
            # redundant re-prune); if state.db.messages now has rows the
            # helper clears the tombstone and the row stays visible. A
            # blind-drop here would be strictly worse than the orphan it
            # suppresses — it would silently swallow a legitimately-resurfaced
            # row forever, even after the user actually sent messages.
            recovered_sidecars = []
            if missing_persisted_ids:
                _diag_stage(diag, "all_sessions.recover_missing_index_sidecars")
                for sid in missing_persisted_ids:
                    try:
                        sidecar = Session.load_metadata_only(sid)
                    except Exception:
                        sidecar = None
                    if not sidecar:
                        continue
                    index_map[sidecar.session_id] = sidecar.compact(
                        include_runtime=True,
                        active_stream_ids=active_stream_ids,
                    )
                    recovered_sidecars.append(sidecar)
                if recovered_sidecars:
                    try:
                        _diag_stage(diag, "all_sessions.recover_missing_index_write")
                        _write_session_index(updates=recovered_sidecars)
                    except Exception:
                        logger.debug("Failed to persist recovered sidebar index rows")
            _diag_stage(diag, "all_sessions.refresh_sidecar_metadata")
            index_message_counts = _index_message_count_map(index)
            refreshed_index_rows = _refresh_index_rows_from_sidecar_metadata(
                list(index_map.values()),
                index_message_counts=index_message_counts,
            )
            index_map = {
                row['session_id']: row
                for row in refreshed_index_rows
                if row.get('session_id')
            }
            _diag_stage(diag, "all_sessions.sort_filter")
            result = sorted(index_map.values(), key=lambda s: (s.get('pinned', False), _session_sort_timestamp(s)), reverse=True)
            # Hide empty Untitled sessions from the UI entirely — they are ephemeral
            # scratch pads that only become real once the first message is sent (#1171).
            # No grace window: a 0-message Untitled session is never shown in the list
            # regardless of age. This means page refreshes and accidental New Conversation
            # clicks never leave orphan entries in the sidebar.
            #
            # Exception: sessions with active_stream_id set are actively streaming (#1327).
            # #1184 deferred the first save() until the first message, so during the
            # initial streaming turn the session still looks like Untitled+0-messages.
            # Without this exemption, navigating away during a long first turn causes
            # the session to vanish from the sidebar.
            result = [s for s in result if not (
                s.get('title', 'Untitled') == 'Untitled'
                and s.get('message_count', 0) == 0
                and not s.get('active_stream_id')
                and not s.get('has_pending_user_message')
                and not s.get('worktree_path')
            )]
            if include_lineage_metadata:
                _diag_stage(diag, "all_sessions.lineage_metadata")
                _enrich_sidebar_lineage_metadata(result)
            else:
                _diag_stage(diag, "all_sessions.state_db_overrides")
                _apply_sidebar_state_db_overrides(result)
                _diag_stage(diag, "all_sessions.lineage_metadata_skipped")
            result = _prefer_fuller_snapshots_for_sidebar(result)
            sidebar_candidates = result
            visible_result = [s for s in sidebar_candidates if not _hide_from_default_sidebar(s)]
            result = _preserve_messageful_sidebar_discoverability(sidebar_candidates, visible_result)
            result = _include_project_hidden_background_sidebar_sessions(sidebar_candidates, result)
            _strip_sidebar_internal_flags(result)
            # Backfill: sessions created before Sprint 22 have no profile tag.
            # Attribute them to 'default' so the client profile filter works correctly.
            for s in result:
                if not s.get('profile'):
                    s['profile'] = 'default'
            return result
        except Exception:
            logger.debug("Failed to load session index, falling back to full scan")
    # Full scan fallback
    _diag_stage(diag, "all_sessions.full_scan")
    out = []
    # #4985: the tombstone is intentionally NOT a blind-drop filter on the
    # full-scan fallback either. A tombstoned sid whose sidecar is still
    # on disk must be loaded here so the post-recovery prune helper
    # (``_prune_orphaned_webui_zero_message_sessions`` in api/routes) gets a
    # chance to self-heal: if state.db.messages is still empty the helper
    # leaves the tombstone in place; if state.db.messages now has rows the
    # helper clears the tombstone and the row stays visible. A blind-drop
    # here would be strictly worse than the orphan it suppresses — silently
    # swallowing a legitimately-resurfaced row forever.
    for p in SESSION_DIR.glob('*.json'):
        if p.name.startswith('_'): continue
        try:
            s = Session.load(p.stem)
            if s: out.append(s)
        except Exception:
            logger.debug("Failed to load session from %s", p)
    _diag_stage(diag, "all_sessions.full_scan_overlay")
    for s in SESSIONS.values():
        if all(s.session_id != x.session_id for x in out): out.append(s)
    _diag_stage(diag, "all_sessions.full_scan_sort_filter")
    out.sort(key=lambda s: (getattr(s, 'pinned', False), _session_sort_timestamp(s)), reverse=True)
    # Hide empty Untitled sessions from the UI entirely — kept consistent with the
    # index-path filter above. No grace window: a 0-message Untitled session is
    # never shown regardless of age (#1171).  Same streaming exemption as above (#1327).
    result = [s.compact(include_runtime=True, active_stream_ids=active_stream_ids) for s in out if not (
        s.title == 'Untitled'
        and len(s.messages) == 0
        and not s.active_stream_id
        and not s.pending_user_message
        and not getattr(s, 'worktree_path', None)
    )]  # fmt: skip
    if include_lineage_metadata:
        _diag_stage(diag, "all_sessions.lineage_metadata")
        _enrich_sidebar_lineage_metadata(result)
    else:
        _diag_stage(diag, "all_sessions.state_db_overrides")
        _apply_sidebar_state_db_overrides(result)
        _diag_stage(diag, "all_sessions.lineage_metadata_skipped")
    result = _prefer_fuller_snapshots_for_sidebar(result)
    sidebar_candidates = result
    visible_result = [s for s in sidebar_candidates if not _hide_from_default_sidebar(s)]
    result = _preserve_messageful_sidebar_discoverability(sidebar_candidates, visible_result)
    result = _include_project_hidden_background_sidebar_sessions(sidebar_candidates, result)
    _strip_sidebar_internal_flags(result)
    for s in result:
        if not s.get('profile'):
            s['profile'] = 'default'
    return result


def _strip_attached_files_marker(text: str) -> str:
    return re.sub(r"\n\n\[Attached files: [^\]]+\]$", "", str(text or "")).strip()


def title_from(messages, fallback: str='Untitled'):
    """Derive a session title from the first user message."""
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content', '')
            if c is None:
                continue
            if isinstance(c, list):
                c = ' '.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') == 'text')
            text = _strip_attached_files_marker(str(c))
            if text:
                return text[:64]
    return fallback


# ── Project helpers ──────────────────────────────────────────────────────────

_PROJECTS_MIGRATION_LOCK = threading.Lock()

__all__ = ['_apply_sidebar_state_db_overrides', '_apply_sidebar_state_db_override_metadata', '_enrich_sidebar_lineage_metadata', '_diag_stage', 'all_sessions', '_strip_attached_files_marker', 'title_from', '_PROJECTS_MIGRATION_LOCK']
