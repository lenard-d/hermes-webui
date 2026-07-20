"""State database metadata projection for the session sidebar."""

from __future__ import annotations

import logging
from contextlib import closing
from pathlib import Path

from api.agent_sessions import (
    normalize_agent_session_source,
    open_state_db_readonly,
    read_session_lineage_metadata,
)
from .projects import title_from
from .state_db_access import _active_state_db_path

logger = logging.getLogger(__name__)


def _sidebar_title_is_generic_webui(title: str | None) -> bool:
    text = ' '.join(str(title or '').split())
    if text == 'Hermes WebUI':
        return True
    prefix = 'Hermes WebUI #'
    return text.startswith(prefix) and text[len(prefix):].isdigit()


def _read_state_db_sidebar_overrides(
    db_path: Path,
    session_ids: set[str],
    count_session_ids: set[str] | None = None,
) -> dict[str, dict]:
    """Return cheap state.db source/title overrides for sidebar rows.

    This intentionally does not chase lineage parents/children. It is used on
    the /api/sessions hot path before CLI filtering so state.db can correct
    stale JSON source flags without paying the full lineage-enrichment cost.

    Two-tier cost split (#5132): the ``sessions``-table lookup (source/title/
    message_count) is an indexed primary-key fetch and is run for ALL
    ``session_ids`` — its result feeds the source classification that
    ``/api/sessions`` filters on BEFORE the lazy lineage correction, so capping
    it would silently drop rows (e.g. a stale ``cli`` JSON row whose state.db
    source is ``webui``) from the default sidebar. The expensive part is the
    ``messages`` aggregation (``COUNT(*)``/``MAX(timestamp)`` GROUP BY), which is
    what blocked /api/sessions for 5-18s on power users; that scan is restricted
    to ``count_session_ids`` (the top-N paint-priority rows). When
    ``count_session_ids`` is None, both tiers cover the full set (caller opted
    out of the cap).
    """
    wanted = {str(sid) for sid in (session_ids or set()) if sid}
    if count_session_ids is None:
        count_wanted = set(wanted)
    else:
        count_wanted = {str(sid) for sid in count_session_ids if sid} & wanted
    if not wanted or not db_path.exists():
        return {}
    try:
        import sqlite3
    except ImportError:
        return {}
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            if 'id' not in session_cols:
                return {}
            source_expr = 's.source' if 'source' in session_cols else 'NULL AS source'
            session_source_expr = 's.session_source' if 'session_source' in session_cols else 'NULL AS session_source'
            title_expr = 's.title' if 'title' in session_cols else 'NULL AS title'
            message_count_expr = 's.message_count' if 'message_count' in session_cols else 'NULL AS message_count'

            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages'")
            has_messages_table = cur.fetchone() is not None
            messages_has_session_id = False
            messages_has_timestamp = False
            messages_has_title_fields = False
            if has_messages_table:
                cur.execute("PRAGMA table_info(messages)")
                message_cols = {str(row[1]) for row in cur.fetchall()}
                messages_has_session_id = 'session_id' in message_cols
                messages_has_timestamp = 'timestamp' in message_cols
                messages_has_title_fields = {'session_id', 'role', 'content', 'timestamp'}.issubset(message_cols)

            overrides: dict[str, dict] = {}
            delegated_title_ids: set[str] = set()
            ids = list(wanted)
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join('?' * len(chunk))
                cur.execute(
                    f"""
                    SELECT s.id, {source_expr}, {session_source_expr}, {title_expr}, {message_count_expr}
                    FROM sessions s
                    WHERE s.id IN ({placeholders})
                    """,
                    chunk,
                )
                for row in cur.fetchall():
                    sid = str(row['id'])
                    entry: dict[str, object] = {}
                    state_title = str(row['title'] or '').strip()
                    if state_title:
                        entry['_state_db_title'] = state_title
                    state_source = str(row['source'] or '').strip().lower()
                    if (
                        state_source == 'subagent'
                        and sid in count_wanted
                        and ' '.join(state_title.split()) == 'Subagent Session'
                    ):
                        delegated_title_ids.add(sid)
                    if state_source:
                        entry['_state_db_source'] = state_source
                        source_meta = normalize_agent_session_source(state_source)
                        entry['_state_db_source_tag'] = state_source
                        entry['_state_db_raw_source'] = source_meta.get('raw_source')
                        entry['_state_db_session_source'] = source_meta.get('session_source')
                        entry['_state_db_source_label'] = source_meta.get('source_label')
                    if row['message_count'] is not None:
                        try:
                            entry['_state_db_message_count'] = max(0, int(row['message_count'] or 0))
                        except (TypeError, ValueError):
                            pass
                    if entry:
                        overrides[sid] = entry
                if has_messages_table and messages_has_session_id:
                    count_chunk = [sid for sid in chunk if sid in count_wanted]
                    if not count_chunk:
                        continue
                    count_placeholders = ','.join('?' * len(count_chunk))
                    last_at_expr = "MAX(timestamp) AS last_message_at" if messages_has_timestamp else "NULL AS last_message_at"
                    cur.execute(
                        f"""
                        SELECT session_id, COUNT(*) AS actual_message_count, {last_at_expr}
                        FROM messages
                        WHERE session_id IN ({count_placeholders})
                        GROUP BY session_id
                        """,
                        count_chunk,
                    )
                    for row in cur.fetchall():
                        sid = str(row['session_id'])
                        entry = overrides.setdefault(sid, {})
                        try:
                            entry['_state_db_message_count'] = max(
                                int(entry.get('_state_db_message_count') or 0),
                                int(row['actual_message_count'] or 0),
                            )
                        except (TypeError, ValueError):
                            pass
                        if row['last_message_at'] is not None:
                            try:
                                entry['_state_db_last_message_at'] = float(row['last_message_at'] or 0)
                            except (TypeError, ValueError):
                                pass
            if messages_has_title_fields and delegated_title_ids:
                seen_user_messages: set[str] = set()
                delegated_title_ids = list(delegated_title_ids)
                for i in range(0, len(delegated_title_ids), chunk_size):
                    chunk = delegated_title_ids[i:i + chunk_size]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT session_id, role, content, timestamp
                        FROM messages
                        WHERE session_id IN ({placeholders}) AND role = 'user'
                        ORDER BY session_id, timestamp ASC
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        sid = str(row['session_id'])
                        if sid in seen_user_messages:
                            continue
                        display_title = title_from([dict(row)], fallback='')
                        if display_title:
                            seen_user_messages.add(sid)
                            overrides.setdefault(sid, {})['_state_db_display_title'] = display_title
            return overrides
    except Exception:
        return {}


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
