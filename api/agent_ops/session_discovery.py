"""Read-only discovery and sidebar projection for Agent-owned sessions."""

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from .session_sources import (
    _as_score,
    _continuation_root_id,
    _is_continuation_session,
    _optional_col,
    _with_normalized_source,
    is_cli_session_row_visible,
)

logger = logging.getLogger(__name__)


def open_state_db_readonly(db_path: Path, log: logging.Logger | None = None) -> sqlite3.Connection:
    """Open the live Agent state database without creating a missing database."""
    log = log or logger
    if not db_path.exists():
        raise FileNotFoundError(f"agent state.db not found: {db_path}")
    read_only_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        return sqlite3.connect(read_only_uri, uri=True)
    except sqlite3.Error as exc:
        log.warning(
            "agent state.db read-only open failed for %s; falling back to writable connection: %s",
            db_path,
            exc,
        )
        return sqlite3.connect(str(db_path))


def _project_agent_session_rows(rows: list[dict]) -> list[dict]:
    """Collapse compression chains into one logical sidebar row.

    The visible conversation should still look like the original chain head
    (title and timestamps), while importing should use the latest importable
    segment so the user continues from the current compressed state.
    """
    rows_by_id = {row['id']: row for row in rows}
    children_by_parent: dict[str, list[dict]] = {}
    continuation_child_ids = set()

    for row in rows:
        parent_id = row.get('parent_session_id')
        if not parent_id:
            continue
        children_by_parent.setdefault(parent_id, []).append(row)
        parent = rows_by_id.get(parent_id)
        if _is_continuation_session(parent, row):
            continuation_child_ids.add(row['id'])
        else:
            row['relationship_type'] = 'child_session'
            row['parent_title'] = parent.get('title') if parent else None
            row['parent_source'] = parent.get('source') if parent else None
            parent_root = _continuation_root_id(rows_by_id, parent_id)
            if parent_root:
                row['_parent_lineage_root_id'] = parent_root

    for children in children_by_parent.values():
        children.sort(key=lambda row: row.get('started_at') or 0, reverse=True)

    def compression_tip(row: dict) -> tuple[dict | None, int]:
        """Return the freshest importable continuation descendant for ``row``.

        Compression parents can have multiple continuation-looking children when
        a stale segment is resumed after a newer compressed branch already
        exists. Picking the newest *direct* child can hide the branch whose
        deeper descendant has the actual latest activity. Walk all reachable
        continuation descendants and select by real message activity instead.
        """
        latest_importable = row if (row.get('actual_message_count') or 0) > 0 else None
        segment_count = 0
        best_depth = 1
        best_score = (
            _as_score(latest_importable.get('last_activity'), latest_importable.get('started_at'))
            if latest_importable
            else 0
        )
        stack: list[tuple[dict, int]] = [(row, 1)]
        seen: set[str] = set()

        while stack:
            current, depth = stack.pop()
            current_id = current.get('id')
            if not current_id or current_id in seen:
                continue
            seen.add(current_id)
            segment_count += 1

            current_score = _as_score(current.get('last_activity'), current.get('started_at'))
            if (
                (current.get('actual_message_count') or 0) > 0
                and (current_score > best_score or (current_score == best_score and depth >= best_depth))
            ):
                latest_importable = current
                best_depth = depth
                best_score = current_score
            for child in children_by_parent.get(current_id, []):
                child_id = child.get('id')
                if not child_id or child_id in seen:
                    continue
                if not _is_continuation_session(current, child):
                    continue
                stack.append((child, depth + 1))

        return latest_importable, max(segment_count, 1)

    projected = []
    for row in rows:
        if row['id'] in continuation_child_ids:
            continue

        segment_count = 1
        tip = row
        if row.get('end_reason') in {'compression', 'cli_close'}:
            tip, segment_count = compression_tip(row)
        if not tip or (tip.get('actual_message_count') or 0) <= 0:
            continue

        if tip is row:
            projected.append(dict(row))
            continue

        merged = dict(row)
        # Keep the chain head's visible identity (title, started_at), but
        # point the row at the latest importable segment for navigation AND
        # surface the tip's recency so an actively-used chain bubbles to the
        # top of the sidebar by its true last activity. Without overriding
        # last_activity, a long-lived chain whose tip is being edited NOW
        # would sort by the root's old timestamp and fall below recently
        # touched standalone sessions — exactly the inverse of what a user
        # expects from "Show agent sessions" sorted by activity.
        for key in (
            'id', 'model', 'message_count', 'actual_message_count', 'actual_user_message_count',
            'ended_at', 'end_reason', 'last_activity',
        ):
            if key in tip:
                merged[key] = tip[key]
        if str(tip.get('source') or '').strip().lower() == 'tui':
            # TUI continuation rows are user-visible session segments (#6, #17,
            # ...), not opaque compression snapshots. Keep navigation pointed at
            # the latest tip and show that tip's title so the newest conversation
            # can be found by its visible TUI name.
            if tip.get('title'):
                merged['title'] = tip.get('title')
            if tip.get('source'):
                merged['source'] = tip.get('source')
        else:
            if not merged.get('title'):
                merged['title'] = tip.get('title')
            if not merged.get('source'):
                merged['source'] = tip.get('source')
        merged['_lineage_root_id'] = row['id']
        merged['_lineage_tip_id'] = tip['id']
        merged['_compression_segment_count'] = segment_count
        projected.append(merged)

    projected.sort(
        key=lambda row: _as_score(row.get('last_activity'), row.get('started_at')),
        reverse=True,
    )
    return projected


def read_importable_agent_session_rows(
    db_path: Path,
    limit: int | None = 200,
    log=None,
    exclude_sources: tuple[str, ...] | None = ("cron", "webui"),
    include_sources: tuple[str, ...] | None = None,
) -> list[dict]:
    """Return agent sessions projected as importable conversations.

    Hermes Agent can create rows in ``state.db.sessions`` before a session has
    any messages, and long conversations can be split into compression-linked
    rows. WebUI cannot import empty rows and should not show compression
    segments as separate conversations, so both the regular ``/api/sessions``
    path and the gateway SSE watcher use this shared projection.

    By default, omit background/internal sources such as ``cron`` from the WebUI
    sidebar. This mirrors Hermes Agent CLI's session-list behaviour: interactive
    views should stay focused on user-facing conversations, while callers that
    need a source-specific diagnostic view can opt out by passing
    ``exclude_sources=None``. ``include_sources`` is an additional narrowing
    filter; callers that want an include-only query should explicitly pass
    ``exclude_sources=None`` so the default exclusions do not also apply.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    log = log or logger
    # Open read-only for this projection/listing path: it is a pure read, and
    # holding a write-capable handle on the live (multi-GB, WAL) state.db while
    # the agent streams into it adds needless checkpoint/lock surface (#5455).
    # The defensive index self-heal below still runs, but through a separate
    # short-lived writable connection on the rare missing-index path only.
    read_only_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(read_only_uri, uri=True)
    except sqlite3.Error as exc:
        log.warning(
            "agent session listing read-only open failed for %s; falling back to writable connection: %s",
            db_path,
            exc,
        )
        conn = sqlite3.connect(str(db_path))
    with closing(conn):
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Older Hermes Agent versions may not have source tracking. Without a
        # source column we cannot safely distinguish WebUI rows from agent rows.
        cur.execute("PRAGMA table_info(sessions)")
        session_cols = {row[1] for row in cur.fetchall()}
        cur.execute("PRAGMA table_info(messages)")
        message_cols = {row[1] for row in cur.fetchall()}
        if 'source' not in session_cols:
            log.warning(
                "agent session listing skipped: state.db at %s has no 'source' column "
                "(older hermes-agent?). Agent sessions unavailable. "
                "Upgrade hermes-agent to fix this.",
                db_path,
            )
            return []

        parent_expr = _optional_col('parent_session_id', session_cols)
        session_source_expr = _optional_col('session_source', session_cols)
        ended_expr = _optional_col('ended_at', session_cols)
        end_reason_expr = _optional_col('end_reason', session_cols)
        user_id_expr = _optional_col('user_id', session_cols)
        chat_id_expr = _optional_col('chat_id', session_cols)
        chat_type_expr = _optional_col('chat_type', session_cols)
        thread_id_expr = _optional_col('thread_id', session_cols)
        session_key_expr = _optional_col('session_key', session_cols)
        origin_chat_id_expr = _optional_col('origin_chat_id', session_cols)
        origin_user_id_expr = _optional_col('origin_user_id', session_cols)
        platform_expr = _optional_col('platform', session_cols)
        # Older/minimal state.db schemas can have NO ``messages`` table at all,
        # or a ``messages`` table without a ``session_id`` / ``timestamp`` column.
        # The projection SQL below joins ``messages`` and aggregates
        # ``MAX(m.timestamp)`` unconditionally, so on those schemas the query
        # raised ``sqlite3.OperationalError`` — which the caller
        # (``get_cli_sessions``) swallows into an empty list, silently hiding
        # ALL imported/CLI/agent sessions from the sidebar. Detect the columns
        # and degrade gracefully (mirrors ``read_session_lineage_metadata``):
        # only join/aggregate ``messages`` when it's actually usable, otherwise
        # fall back to the per-session ``s.message_count`` / ``s.started_at``. (#3762)
        messages_has_session_id = 'session_id' in message_cols
        messages_has_timestamp = 'timestamp' in message_cols
        use_messages_join = messages_has_session_id
        count_col = 'id' if 'id' in message_cols else 'session_id'

        # Defensive index prime (#3887). The normal candidate-ordering shape uses
        # the agent's standard ``idx_messages_session ON messages(session_id,
        # timestamp)`` index; without it, large cron-only scans degrade badly.
        # Writable dbs self-heal by recreating the index. Read-only or locked dbs
        # fall back to the pre-aggregated cron-only path below instead of failing.
        messages_index_present = False
        if messages_has_session_id and messages_has_timestamp:
            try:
                cur.execute("PRAGMA index_list(messages)")
                messages_index_present = any(str(row[1]) == "idx_messages_session" for row in cur.fetchall())
            except sqlite3.Error:
                messages_index_present = False
            if not messages_index_present:
                # Self-heal via a separate writable connection so the common
                # (index-present) path keeps its read-only handle. On a truly
                # read-only/locked db this fails and we degrade to the
                # pre-aggregated cron-only path below, exactly as before.
                try:
                    with closing(sqlite3.connect(str(db_path))) as _heal:
                        _heal.execute(
                            "CREATE INDEX IF NOT EXISTS idx_messages_session "
                            "ON messages(session_id, timestamp)"
                        )
                        _heal.commit()
                    messages_index_present = True
                except sqlite3.Error:
                    pass  # read-only db / locked / older schema — degrade gracefully

        if use_messages_join:
            actual_count_expr = f"COUNT(m.{count_col})"
            if 'role' in message_cols:
                user_message_count_expr = "COUNT(CASE WHEN LOWER(m.role) = 'user' THEN 1 END)"
            else:
                user_message_count_expr = f"COUNT(m.{count_col})"
            last_activity_expr = "MAX(m.timestamp)" if messages_has_timestamp else "NULL"
            join_clause = "LEFT JOIN messages m ON m.session_id = s.id"
            group_by_clause = "GROUP BY s.id"
        else:
            # No usable messages table: use the denormalized per-session counts
            # and ``started_at`` so the rows still surface in the sidebar.
            actual_count_expr = "s.message_count"
            user_message_count_expr = "s.message_count"
            last_activity_expr = "NULL"
            join_clause = ""
            group_by_clause = ""

        order_by_clause = "ORDER BY s.started_at DESC"
        latest_messages_cte = None
        candidate_order_clause = "ORDER BY s.started_at DESC"

        where_clauses = ["s.source IS NOT NULL"]
        params: list[object] = []
        included = ()
        if include_sources:
            included = tuple(str(source) for source in include_sources if source)
            if included:
                placeholders = ", ".join("?" for _ in included)
                where_clauses.append(f"s.source IN ({placeholders})")
                params.extend(included)
        if exclude_sources:
            excluded = tuple(str(source) for source in exclude_sources if source)
            if excluded:
                placeholders = ", ".join("?" for _ in excluded)
                where_clauses.append(f"s.source NOT IN ({placeholders})")
                params.extend(excluded)

        use_preaggregated_candidate_order = (
            use_messages_join
            and messages_has_timestamp
            and included == ("cron",)
            and not messages_index_present
        )
        if use_preaggregated_candidate_order:
            order_by_clause = "ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC"
            latest_messages_cte = (
                "latest_messages AS (\n"
                "                    SELECT mx.session_id AS session_id, MAX(mx.timestamp) AS last_message_at\n"
                "                    FROM messages mx\n"
                "                    GROUP BY mx.session_id\n"
                "                )"
            )
            candidate_order_clause = "ORDER BY COALESCE(lm.last_message_at, s.started_at) DESC, s.started_at DESC"
        elif use_messages_join and messages_has_timestamp:
            order_by_clause = "ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC"
            candidate_order_clause = (
                "ORDER BY COALESCE(\n"
                "                        (SELECT MAX(mx.timestamp) FROM messages mx WHERE mx.session_id = s.id),\n"
                "                        s.started_at\n"
                "                    ) DESC,\n"
                "                    s.started_at DESC"
            )

        select_sql = f"""
            SELECT s.id, s.title, s.model, s.message_count,
                   s.started_at, s.source,
                   {session_source_expr},
                   {user_id_expr},
                   {chat_id_expr},
                   {chat_type_expr},
                   {thread_id_expr},
                   {session_key_expr},
                   {origin_chat_id_expr},
                   {origin_user_id_expr},
                   {platform_expr},
                   {parent_expr},
                   {ended_expr},
                   {end_reason_expr},
                   {actual_count_expr} AS actual_message_count,
                   {user_message_count_expr} AS actual_user_message_count,
                   {last_activity_expr} AS last_activity
        """
        if limit is not None:
            result_limit = max(0, int(limit))
            if result_limit == 0:
                return []
            # The sidebar only needs a small visible window. Bound the expensive
            # messages join to a recent-activity candidate set instead of
            # aggregating every historical Hermes state.db session before
            # slicing in Python. The candidate ordering must include the latest
            # message timestamp, not only ``started_at``: long-lived CLI sessions
            # can be resumed days later and should still surface at the top.
            # Oversampling preserves room for hidden compression segments or
            # other rows filtered after projection.
            candidate_limit = max(result_limit * 8, result_limit)
            if latest_messages_cte:
                candidate_cte = (
                    "WITH {latest_messages_cte}, candidates AS (\n"
                    "                    SELECT s.id\n"
                    "                    FROM sessions s\n"
                    "                    LEFT JOIN latest_messages lm ON lm.session_id = s.id\n"
                    "                    WHERE {where_clause}\n"
                    "                    {candidate_order_clause}\n"
                    "                    LIMIT ?\n"
                    "                )"
                ).format(
                    latest_messages_cte=latest_messages_cte,
                    where_clause=" AND ".join(where_clauses),
                    candidate_order_clause=candidate_order_clause,
                )
            else:
                candidate_cte = (
                    "WITH candidates AS (\n"
                    "                    SELECT s.id\n"
                    "                    FROM sessions s\n"
                    "                    WHERE {where_clause}\n"
                    "                    {candidate_order_clause}\n"
                    "                    LIMIT ?\n"
                    "                )"
                ).format(
                    where_clause=" AND ".join(where_clauses),
                    candidate_order_clause=candidate_order_clause,
                )

            cur.execute(
                f"""
                {candidate_cte}
                {select_sql}
                FROM sessions s
                JOIN candidates c ON c.id = s.id
                {join_clause}
                {group_by_clause}
                {order_by_clause}
                """,
                [*params, candidate_limit],
            )
        else:
            cur.execute(
                f"""
                {select_sql}
                FROM sessions s
                {join_clause}
                WHERE {' AND '.join(where_clauses)}
                {group_by_clause}
                {order_by_clause}
                """,
                params,
            )
        projected = _project_agent_session_rows([dict(row) for row in cur.fetchall()])
        projected = [_with_normalized_source(row) for row in projected]
        projected = [row for row in projected if is_cli_session_row_visible(row)]
        if limit is None:
            return projected
        return projected[:max(0, int(limit))]
