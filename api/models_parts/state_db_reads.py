"""Read-only state-database message queries.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

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

__all__ = ['_json_loads_if_string', 'get_state_db_session_messages', 'get_state_db_session_message_prefix_summary', 'get_state_db_session_message_keys_before_timestamp', 'get_state_db_session_summary']
