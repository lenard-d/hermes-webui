"""Read-only Agent session lineage reports and sidebar metadata."""

import sqlite3
from contextlib import closing
from pathlib import Path

from .session_discovery import open_state_db_readonly
from .session_sources import (
    _as_score,
    _continuation_root_id,
    _is_continuation_session,
    _optional_col,
    normalize_agent_session_source,
)


def _lineage_report_row(row: dict, role: str) -> dict:
    updated_at = row.get('ended_at') if row.get('ended_at') is not None else row.get('started_at')
    return {
        'session_id': row.get('id'),
        'role': role,
        'title': row.get('title'),
        'source': row.get('source'),
        'started_at': row.get('started_at'),
        'updated_at': updated_at,
        'end_reason': row.get('end_reason'),
        'active': row.get('ended_at') is None,
        'archived': False,
    }


def _empty_lineage_report(session_id: str, *, found: bool = False) -> dict:
    return {
        'mutation': False,
        'found': found,
        'session_id': session_id,
        'lineage_key': session_id,
        'tip_session_id': session_id,
        'total_segments': 0,
        'materialized_segments': 0,
        'segments': [],
        'children': [],
        'manual_review': False,
    }


def read_session_lineage_report(db_path: Path, session_id: str | None, max_hops: int = 20) -> dict:
    """Return a bounded, read-only lifecycle report for a session lineage.

    This helper intentionally reports only facts that can be derived from
    ``state.db.sessions`` without mutating WebUI JSON, archiving rows, or
    deleting historical segments. It mirrors the sidebar continuation rules so
    a future UI/PR can explain which rows are hidden compression/cli-close
    segments and which child-session branches remain distinct.
    """
    sid = str(session_id or '').strip()
    if not sid:
        return _empty_lineage_report('')
    db_path = Path(db_path)
    if not db_path.exists():
        return _empty_lineage_report(sid)

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            required = {'id', 'parent_session_id', 'end_reason'}
            if not required.issubset(session_cols):
                return _empty_lineage_report(sid)

            source_expr = _optional_col('source', session_cols)
            session_source_expr = _optional_col('session_source', session_cols)
            title_expr = _optional_col('title', session_cols)
            started_expr = _optional_col('started_at', session_cols, '0')
            ended_expr = _optional_col('ended_at', session_cols)
            end_reason_expr = _optional_col('end_reason', session_cols)
            parent_expr = _optional_col('parent_session_id', session_cols)

            def fetch_one(row_id: str | None) -> dict | None:
                if not row_id:
                    return None
                cur.execute(
                    f"""
                    SELECT s.id,
                           {source_expr},
                           {session_source_expr},
                           {title_expr},
                           {started_expr},
                           {parent_expr},
                           {ended_expr},
                           {end_reason_expr}
                    FROM sessions s
                    WHERE s.id = ?
                    """,
                    (row_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

            target = fetch_one(sid)
            if not target:
                return _empty_lineage_report(sid)

            segments = [target]
            current = target
            seen = {sid}
            manual_review = False
            for _hop in range(max(0, int(max_hops))):
                parent_id = current.get('parent_session_id')
                parent = fetch_one(parent_id)
                if not parent or parent_id in seen:
                    manual_review = bool(parent_id and parent_id in seen)
                    break
                if not _is_continuation_session(parent, current):
                    break
                segments.append(parent)
                seen.add(parent_id)
                current = parent
            else:
                manual_review = True

            segment_ids = {row['id'] for row in segments}
            child_rows: list[dict] = []
            parent_ids = [row['id'] for row in segments]
            children_by_parent: dict[str, list[dict]] = {pid: [] for pid in parent_ids}
            if parent_ids:
                placeholders = ','.join('?' * len(parent_ids))
                cur.execute(
                    f"""
                    SELECT s.id,
                           {source_expr},
                           {session_source_expr},
                           {title_expr},
                           {started_expr},
                           {parent_expr},
                           {ended_expr},
                           {end_reason_expr}
                    FROM sessions s
                    WHERE s.parent_session_id IN ({placeholders})
                    """,
                    parent_ids,
                )
                for child_row in cur.fetchall():
                    child = dict(child_row)
                    parent_id = child.get('parent_session_id')
                    if parent_id in children_by_parent:
                        children_by_parent[parent_id].append(child)
            for parent in segments:
                parent_children = children_by_parent.get(parent['id'], [])
                parent_children.sort(key=lambda row: row.get('started_at') or 0, reverse=True)
                for child in parent_children:
                    if child['id'] in segment_ids:
                        continue
                    if _is_continuation_session(parent, child):
                        # A continuation outside the selected path means the
                        # lineage is branched or the caller selected an older
                        # segment. Report manual review rather than proposing
                        # destructive cleanup candidates.
                        manual_review = True
                        continue
                    child_rows.append(child)
    except Exception:
        return _empty_lineage_report(sid)

    root_id = segments[-1]['id'] if segments else sid
    tip_id = segments[0]['id'] if segments else sid
    return {
        'mutation': False,
        'found': True,
        'session_id': sid,
        'lineage_key': root_id,
        'tip_session_id': tip_id,
        'total_segments': len(segments),
        'materialized_segments': len(segments),
        'segments': [
            _lineage_report_row(row, 'tip' if idx == 0 else 'hidden_segment')
            for idx, row in enumerate(segments)
        ],
        'children': [_lineage_report_row(row, 'child_session') for row in child_rows],
        'manual_review': manual_review,
    }


def read_session_lineage_metadata(db_path: Path, session_ids: list[str] | set[str]) -> dict[str, dict]:
    """Return compression-lineage metadata for known WebUI sidebar sessions.

    WebUI sessions are persisted as JSON files, but Hermes Agent also mirrors
    them into ``state.db.sessions`` for insights/session history. Compression
    and cross-surface continuation create parent chains there. ``/api/sessions``
    needs to surface that lineage to the sidebar so client-side collapse can
    group logical continuations without mutating or deleting any session files.

    Missing DBs, old schemas, or incomplete rows degrade to an empty mapping.
    """
    wanted = {str(sid) for sid in (session_ids or []) if sid}
    db_path = Path(db_path)
    if not wanted or not db_path.exists():
        return {}

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            if 'parent_session_id' not in session_cols or 'end_reason' not in session_cols:
                return {}
            session_source_expr = _optional_col('session_source', session_cols)
            source_expr = _optional_col('source', session_cols)
            message_count_expr = _optional_col('message_count', session_cols, '0')
            # Scoped fetch via PRIMARY KEY + idx_sessions_parent rather than a
            # full table scan. The sessions table grows unbounded over time
            # (1000+ rows is normal, 10000+ for power users), and this function
            # runs on every sidebar refresh — a full SELECT was ~50x slower
            # than the indexed lookup at 1000 rows and scales linearly.
            #
            # Fetch the wanted ids first, then chase parent_session_id chains
            # in batches until no new ids appear. Each batch hits PRIMARY KEY
            # so it's effectively O(N) lookups. Then walk continuation children
            # from the materialized ancestors so branchy compression lineages can
            # mark the real freshest tip, not just the newest direct sibling.
            #
            # IN-clause is chunked to 500 to stay under SQLITE_MAX_VARIABLE_NUMBER
            # on older sqlite (Python 3.9 ships sqlite 3.31 which defaults to 999;
            # newer Python ships sqlite 3.32+ at 32766). On a power user with
            # 2000+ sessions in the sidebar, an unchunked first hop would raise
            # `OperationalError: too many SQL variables`, get swallowed by the
            # except below, and silently disable lineage collapse forever.
            # (Opus pre-release review of v0.50.251, SHOULD-FIX 2.)
            IN_CHUNK = 500
            rows: dict[str, dict] = {}
            to_fetch = set(wanted)
            # Cap walk depth to bound worst-case query count. Real lineage
            # chains seen in production are <10 segments; anything longer is
            # almost certainly pathological data and not worth chasing.
            for _hop in range(20):
                if not to_fetch:
                    break
                fetch_list = list(to_fetch)
                to_fetch = set()
                for i in range(0, len(fetch_list), IN_CHUNK):
                    chunk = fetch_list[i:i + IN_CHUNK]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT s.id, {source_expr}, {session_source_expr}, s.title, s.started_at, s.parent_session_id, s.ended_at, s.end_reason, {message_count_expr}
                        FROM sessions s
                        WHERE s.id IN ({placeholders})
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        rows[row['id']] = dict(row)
                # Queue up parents we haven't fetched yet.
                for sid in fetch_list:
                    parent_id = rows.get(sid, {}).get('parent_session_id')
                    if parent_id and parent_id not in rows and parent_id not in to_fetch:
                        to_fetch.add(parent_id)

            # Fetch descendants from the discovered ancestors using the parent
            # index. This keeps the sidebar read scoped while still giving the
            # collapse metadata enough information to choose the active branch.
            to_expand = set(rows)
            expanded: set[str] = set()
            for _hop in range(20):
                frontier = [sid for sid in to_expand if sid not in expanded]
                if not frontier:
                    break
                to_expand = set()
                for i in range(0, len(frontier), IN_CHUNK):
                    chunk = frontier[i:i + IN_CHUNK]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT s.id, {source_expr}, {session_source_expr}, s.title, s.started_at, s.parent_session_id, s.ended_at, s.end_reason, {message_count_expr}
                        FROM sessions s
                        WHERE s.parent_session_id IN ({placeholders})
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        child = dict(row)
                        rows[child['id']] = child
                        parent_id = child.get('parent_session_id')
                        parent = rows.get(str(parent_id)) if parent_id else None
                        if parent and child['id'] not in expanded and _is_continuation_session(parent, child):
                            to_expand.add(child['id'])
                expanded.update(frontier)

            message_stats: dict[str, dict] = {}
            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages'")
            has_messages_table = cur.fetchone() is not None
            # Older/minimal state.db schemas can have a `messages` table WITHOUT a
            # `timestamp` column (or with a non-numeric one). Detect the columns
            # rather than gating on table existence alone: require `session_id`,
            # and only select MAX(timestamp) when that column is actually present
            # so the query can't raise and collapse the whole lineage metadata.
            messages_has_session_id = False
            messages_has_timestamp = False
            if has_messages_table:
                cur.execute("PRAGMA table_info(messages)")
                _message_cols = {row[1] for row in cur.fetchall()}
                messages_has_session_id = 'session_id' in _message_cols
                messages_has_timestamp = 'timestamp' in _message_cols
            use_messages_query = has_messages_table and messages_has_session_id
            row_ids = list(rows)
            if use_messages_query:
                last_at_expr = "MAX(timestamp) AS last_message_at" if messages_has_timestamp else "NULL AS last_message_at"
                for i in range(0, len(row_ids), IN_CHUNK):
                    chunk = row_ids[i:i + IN_CHUNK]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT session_id, COUNT(*) AS actual_message_count, {last_at_expr}
                        FROM messages
                        WHERE session_id IN ({placeholders})
                        GROUP BY session_id
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        message_stats[row['session_id']] = dict(row)
            for sid, row in rows.items():
                stats = message_stats.get(sid) or {}
                if use_messages_query:
                    row['actual_message_count'] = int(stats.get('actual_message_count') or 0)
                else:
                    row['actual_message_count'] = int(row.get('message_count') or 0)
                row['last_message_at'] = stats.get('last_message_at')
    except Exception:
        return {}

    children_by_parent: dict[str, list[dict]] = {}
    for row in rows.values():
        parent_id = row.get('parent_session_id')
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(row)

    def continuation_root_and_depth(sid: str) -> tuple[str, int]:
        root_id = sid
        current_id = sid
        depth = 1
        seen = {sid}
        while True:
            current = rows.get(current_id)
            raw_parent_id = current.get('parent_session_id') if current else None
            parent_id = str(raw_parent_id) if raw_parent_id else ''
            if not parent_id:
                break
            parent = rows.get(parent_id)
            if not parent or parent_id in seen:
                break
            if not _is_continuation_session(parent, current):
                break
            root_id = parent_id
            current_id = parent_id
            seen.add(parent_id)
            depth += 1
        return root_id, depth

    def freshest_continuation_tip(root_id: str) -> tuple[str, int]:
        best_id = root_id
        best_depth = 1
        segment_count = 0
        best_score = _as_score(rows.get(root_id, {}).get('last_message_at'), rows.get(root_id, {}).get('started_at'))
        stack: list[tuple[str, int]] = [(root_id, 1)]
        seen: set[str] = set()
        while stack:
            current_id, depth = stack.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            current = rows.get(current_id)
            if not current:
                continue
            segment_count += 1
            actual_count = int(current.get('actual_message_count') or 0)
            score = _as_score(current.get('last_message_at'), current.get('started_at'))
            if actual_count > 0 and (score > best_score or (score == best_score and depth >= best_depth)):
                best_id = current_id
                best_depth = depth
                best_score = score
            for child in children_by_parent.get(current_id, []):
                if _is_continuation_session(current, child):
                    stack.append((child['id'], depth + 1))

        return best_id, max(segment_count, best_depth)

    lineage_tip_cache: dict[str, tuple[str, int]] = {}
    metadata: dict[str, dict] = {}
    for sid in wanted:
        row = rows.get(sid)
        if not row:
            continue

        state_title = str(row.get('title') or '').strip()
        if state_title:
            metadata.setdefault(sid, {})['_state_db_title'] = state_title
        state_source = str(row.get('source') or '').strip().lower()
        if state_source:
            entry = metadata.setdefault(sid, {})
            entry['_state_db_source'] = state_source
            source_meta = normalize_agent_session_source(state_source)
            entry['_state_db_source_tag'] = state_source
            entry['_state_db_raw_source'] = source_meta.get('raw_source')
            entry['_state_db_session_source'] = source_meta.get('session_source')
            entry['_state_db_source_label'] = source_meta.get('source_label')

        parent_id = row.get('parent_session_id')
        parent_row = rows.get(parent_id) if parent_id else None
        if parent_id and parent_row:
            entry = metadata.setdefault(sid, {})
            entry['parent_session_id'] = parent_id
            if not _is_continuation_session(parent_row, row):
                entry['relationship_type'] = 'child_session'
                entry['parent_title'] = parent_row.get('title')
                entry['parent_source'] = parent_row.get('source')
                parent_source = str(parent_row.get('source') or '').strip().lower()
                child_source = str(row.get('source') or '').strip().lower()
                if parent_source and child_source and parent_source != child_source:
                    entry['_cross_surface_child_session'] = True
                parent_root = _continuation_root_id(rows, parent_id)
                if parent_root:
                    entry['_parent_lineage_root_id'] = parent_root
                    if parent_root not in lineage_tip_cache:
                        lineage_tip_cache[parent_root] = freshest_continuation_tip(parent_root)
                    entry['_parent_lineage_tip_id'] = lineage_tip_cache[parent_root][0]
                continue

        root_id, segment_count = continuation_root_and_depth(sid)

        if root_id != sid:
            entry = metadata.setdefault(sid, {})
            entry['_lineage_root_id'] = root_id
            if root_id not in lineage_tip_cache:
                lineage_tip_cache[root_id] = freshest_continuation_tip(root_id)
            tip_id, tip_depth = lineage_tip_cache[root_id]
            entry['_lineage_tip_id'] = tip_id
            entry['_compression_segment_count'] = max(segment_count, tip_depth)

    return metadata
