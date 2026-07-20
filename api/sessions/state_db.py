"""State-database queries used by session projection and file operations."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from importlib.util import find_spec
from pathlib import Path

from api.agent_sessions import (
    _is_continuation_session,
    normalize_agent_session_source,
    open_state_db_readonly,
    read_session_lineage_metadata,
)
from api.config import HOME
from api.workspace import get_last_workspace
from .projects import title_from
from .process_wakeup import _get_profile_home
from .records import Session
from .message_identity import _session_message_visible_key

logger = logging.getLogger(__name__)

def state_db_has_session(sid: str) -> bool:
    """Return True when ``sid`` exists in the active state.db sessions table.

    Used by file-manager handlers to fall back to a state.db lookup when
    ``get_session`` raises ``KeyError`` because the session was created by
    Telegram/CLI (external) rather than the WebUI (issue #3280). The state.db
    schema stores only metadata (id/title/model/source/...), not a workspace
    path — the workspace is shared across session storage backends and is
    resolved separately via ``get_last_workspace()``.
    """
    if not sid:
        return False
    if find_spec("sqlite3") is None:
        return False
    db_path = _active_state_db_path()
    if not db_path.exists():
        return False
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (str(sid),))
            return cur.fetchone() is not None
    except Exception:
        return False


class _ExternalSessionView:
    """Minimal session-shaped view for external (Telegram/CLI) sessions.

    Only exposes the fields file-manager handlers need (``session_id`` and
    ``workspace``). The workspace falls back to the WebUI's last-used
    workspace because state.db does not persist a per-session workspace path
    and the file browser is intentionally workspace-scoped, not
    session-storage-scoped (issue #3280).
    """

    __slots__ = ("session_id", "workspace")

    def __init__(self, session_id: str, workspace: str):
        self.session_id = session_id
        self.workspace = workspace


def get_session_for_file_ops(sid: str):
    """Return a profile-authorized session-like object for file-manager handlers.

    Tries ``get_session`` first (preserves all existing behavior for WebUI
    sessions) and only returns that session when its stored profile belongs to
    the active request profile.  If that lookup fails, checks state.db; when the
    session exists there, returns an ``_ExternalSessionView`` whose ``workspace``
    is the active WebUI workspace. If neither has the session, re-raises
    ``KeyError`` so callers continue to return their existing 404.
    """
    from .cache import get_session

    try:
        session = get_session(sid, metadata_only=True)
    except KeyError:
        if state_db_has_session(sid):
            return _ExternalSessionView(str(sid), str(get_last_workspace()))
        raise

    from api.profiles import get_active_profile_name, profiles_match as _profiles_match

    session_profile = getattr(session, 'profile', None)
    active_profile = get_active_profile_name()
    if not _profiles_match(session_profile, active_profile):
        logger.debug(
            "Rejected file-manager session for foreign profile: "
            "session_id=%s session_profile=%r active_profile=%r",
            sid,
            session_profile,
            active_profile,
        )
        raise KeyError(sid)
    return session


def _active_state_db_path() -> Path:
    """Return state.db for the active Hermes profile, degrading to HERMES_HOME."""
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    return hermes_home / 'state.db'


def _agent_state_db_path(*, profile=None) -> Path | None:
    """Return agent ``state.db`` for *profile*, or ``None`` when unavailable."""
    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return None
    return db_path


def agent_session_rows_existing(
    session_ids: list[str] | set[str] | frozenset[str],
    *,
    profile=None,
) -> frozenset[str]:
    """Return session ids confirmed present in the agent ``sessions`` table.

    Used by the sidebar orphan-prune path (#3238) to batch existence probes
    instead of opening one SQLite connection per candidate row.

    Degrades safely to ``frozenset(wanted)`` (assume all present) on any error,
    when the DB is missing, or when the ``sessions`` table is absent — matching
    ``agent_session_row_exists()`` so a transient failure never causes pruning.
    """
    wanted = {str(sid).strip() for sid in (session_ids or []) if str(sid or "").strip()}
    if not wanted:
        return frozenset()
    if find_spec("sqlite3") is None:
        return frozenset(wanted)
    db_path = _agent_state_db_path(profile=profile)
    if db_path is None:
        return frozenset(wanted)
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            cols = {str(row[1]) for row in cur.fetchall()}
            if 'id' not in cols:
                return frozenset(wanted)
            existing: set[str] = set()
            ids = list(wanted)
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join('?' * len(chunk))
                cur.execute(
                    f"SELECT id FROM sessions WHERE id IN ({placeholders})",
                    chunk,
                )
                existing.update(str(row[0]).strip() for row in cur.fetchall())
            return frozenset(existing)
    except Exception:
        logger.debug(
            "agent_session_rows_existing probe failed for %d ids",
            len(wanted),
            exc_info=True,
        )
        return frozenset(wanted)


def agent_session_zero_message_sids(
    session_ids: list[str] | set[str] | frozenset[str],
    *,
    profile=None,
) -> frozenset[str]:
    """Return session ids confirmed to have zero rows in the agent ``messages`` table.

    Used by the sidebar orphan-prune path (#4985) to detect native-WebUI sessions
    whose backing agent row exists but was never written to (boot-time ``+`` click,
    profile switch that resets the active id, sidebar nav that opens a session then
    closes the tab before the first message commits). Such rows linger in the
    sidebar forever because the WebUI delete affordance is not exposed for them,
    and the existing #3238/#4591 orphan prune explicitly excludes webui sources.

    Mirrors ``agent_session_rows_existing``'s batched chunked probe, safe-degrade
    contract (returns ``frozenset()`` on any error so a transient failure NEVER
    causes a stale-prune data loss), and ``messages`` table absence handling.
    """
    wanted = {str(sid).strip() for sid in (session_ids or []) if str(sid or "").strip()}
    if not wanted:
        return frozenset()
    if find_spec("sqlite3") is None:
        return frozenset()
    db_path = _agent_state_db_path(profile=profile)
    if db_path is None:
        return frozenset()
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            sessions_cols = {str(row[1]) for row in cur.fetchall()}
            if 'id' not in sessions_cols:
                return frozenset()
            cur.execute("PRAGMA table_info(messages)")
            messages_cols = {str(row[1]) for row in cur.fetchall()}
            if 'session_id' not in messages_cols:
                return frozenset()
            zero_message: set[str] = set()
            ids = list(wanted)
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join('?' * len(chunk))
                cur.execute(
                    f"SELECT s.id FROM sessions s "
                    f"WHERE s.id IN ({placeholders}) "
                    f"AND NOT EXISTS ("
                    f"  SELECT 1 FROM messages m WHERE m.session_id = s.id"
                    f")",
                    chunk,
                )
                zero_message.update(str(row[0]).strip() for row in cur.fetchall())
            return frozenset(zero_message)
    except Exception:
        logger.debug(
            "agent_session_zero_message_sids probe failed for %d ids",
            len(wanted),
            exc_info=True,
        )
        return frozenset()


def agent_session_row_exists(session_id: str, *, profile=None) -> bool:
    """Return True if ``session_id`` still has a backing row in the agent state.db.

    Used to detect orphaned imported-CLI sidecars (#3238): the WebUI sidebar
    must NOT rely on the session's presence in ``get_cli_sessions()`` to decide
    whether its backing CLI row still exists, because that helper caps at
    ``CLI_VISIBLE_SESSION_LIMIT`` (20) rows — a still-existing session can fall
    out of the recent window and look "deleted." This is an exact, uncapped
    existence probe against the ``sessions`` table.

    Degrades safely to ``True`` (assume present) on any error or when the DB is
    unreadable, so a transient failure never causes a stale-pruning data loss.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return False
    return sid in agent_session_rows_existing([sid], profile=profile)


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


def _path_stat_cache_key(path):
    if path is None:
        return None
    try:
        stat = Path(path).stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def state_db_content_fingerprint(db_path: Path):
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


def state_db_cache_key(db_path: Path):
    """Return a commit-reliable invalidation key for a SQLite DB.

    Combines a content fingerprint (the authoritative signal — advances on every
    commit, immune to mtime-granularity collisions that flaked the gateway_sync
    test) with the cheap file stat stamps as a belt-and-suspenders fallback for
    the case where the fingerprint can't be read.
    """
    return (
        state_db_content_fingerprint(db_path),
        _path_stat_cache_key(db_path),
        _path_stat_cache_key(Path(f"{db_path}-wal")),
        _path_stat_cache_key(Path(f"{db_path}-shm")),
    )

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
