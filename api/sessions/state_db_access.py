"""Profile-aware state database access and exact session existence probes."""

from __future__ import annotations

import logging
import os
from contextlib import closing
from importlib.util import find_spec
from pathlib import Path

from api.agent_sessions import (
    open_state_db_readonly,
)
from api.config import HOME
from api.workspace import get_last_workspace
from .process_wakeup import _get_profile_home

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
    from .session_cache_repository import get_session

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
