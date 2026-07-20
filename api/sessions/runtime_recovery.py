"""Persisted stream-state reconciliation for durable sessions.

The runtime registry owns liveness; the session repository owns the durable
mutation. This module keeps the generation recheck, pending-turn repair, and
idle projection together so reads cannot clear a newer run.
"""

from __future__ import annotations

import logging
import time

from api.config import runtime_stream_alive, runtime_worker_alive
from api.runs.transcript import _materialize_pending_user_turn_before_error
from api.sessions.repository import edit_session
from api.sessions.pending_recovery.sidecar_recovery import (
    _REPAIR_STALE_PENDING_GRACE_SECONDS,
)
from api.sessions.session_cache_repository import get_session

logger = logging.getLogger(__name__)

def _clear_session_stream_fields(session) -> None:
    """Project one session object into the durable idle-stream shape."""
    session.active_stream_id = None
    if hasattr(session, "pending_user_message"):
        session.pending_user_message = None
    if hasattr(session, "pending_attachments"):
        session.pending_attachments = []
    if hasattr(session, "pending_started_at"):
        session.pending_started_at = None
    if hasattr(session, "pending_user_source"):
        session.pending_user_source = None


def _clear_stale_stream_state(session) -> bool:
    """Clear persisted streaming flags when the in-memory stream no longer exists.

    A server restart or worker crash can leave active_stream_id/pending_* in the
    session JSON while STREAMS is empty. The frontend then keeps reconnecting to
    a dead stream and shows a permanent running/thinking state.

    The repository reloads the full current generation only after acquiring its
    owner lock. A metadata-only projection or detached stale object therefore
    cannot overwrite the durable transcript or a newer stream generation.
    """
    stream_id = getattr(session, "active_stream_id", None)
    if not stream_id:
        return False
    if runtime_stream_alive(stream_id):
        return False
    if runtime_worker_alive(stream_id):
        logger.debug(
            "_clear_stale_stream_state: stream %s for session %s missing SSE channel "
            "but worker bookkeeping is still active; deferring stale cleanup",
            stream_id,
            getattr(session, "session_id", "?"),
        )
        return False
    grace_seconds = 30.0
    try:
        grace_seconds = float(_REPAIR_STALE_PENDING_GRACE_SECONDS)
        pending_started_at = getattr(session, "pending_started_at", None)
        pending_age = time.time() - float(pending_started_at) if pending_started_at else None
    except Exception:
        pending_age = None
    if (
        getattr(session, "pending_user_message", None)
        and pending_age is not None
        and pending_age < grace_seconds
    ):
        logger.debug(
            "_clear_stale_stream_state: stream %s for session %s missing SSE channel "
            "but pending turn is %.1fs old; waiting for %.1fs stale-repair grace",
            stream_id,
            getattr(session, "session_id", "?"),
            pending_age,
            grace_seconds,
        )
        return False

    original_projection = session
    authoritative_session = None
    cleared = False
    repair_persisted = False

    try:
        # Runtime cleanup is not user activity; do not bubble old sessions to
        # the top of the sidebar. ``save_when`` also avoids a second save when
        # the recovery helper already persisted its richer repair atomically.
        with edit_session(
            session.session_id,
            session=session,
            touch_updated_at=False,
            save_when=lambda _current: cleared and not repair_persisted,
        ) as current:
            authoritative_session = current
            # A concurrent chat start may have replaced the old generation
            # while this cleanup waited for the same repository owner lock.
            if getattr(current, "active_stream_id", None) != stream_id:
                return False
            if runtime_stream_alive(stream_id) or runtime_worker_alive(stream_id):
                logger.debug(
                    "_clear_stale_stream_state: stream %s for session %s became "
                    "live while cleanup waited for its owner",
                    stream_id,
                    getattr(current, "session_id", "?"),
                )
                return False

            if getattr(current, "pending_user_message", None):
                try:
                    from api.sessions.pending_recovery.sidecar_recovery import (
                        _apply_core_sync_or_error_marker,
                    )
                    from api.sessions.process_wakeup import _get_profile_home

                    profile_home = _get_profile_home(getattr(current, "profile", None))
                    core_path = (
                        profile_home
                        / "sessions"
                        / f"session_{current.session_id}.json"
                    )
                    repair_persisted = _apply_core_sync_or_error_marker(
                        current,
                        core_path,
                        stream_id_for_recheck=stream_id,
                        touch_updated_at=False,
                    )
                except Exception:
                    logger.exception(
                        "_clear_stale_stream_state: failed to repair stale pending "
                        "stream %s for session %s",
                        stream_id,
                        getattr(current, "session_id", "?"),
                    )
                    repair_persisted = False
                if repair_persisted:
                    cleared = True
                elif getattr(current, "active_stream_id", None) != stream_id:
                    return False

            if not repair_persisted:
                _materialize_pending_user_turn_before_error(current)
                _clear_session_stream_fields(current)
                cleared = True
    except Exception:
        logger.exception(
            "_clear_stale_stream_state: repository mutation failed for session %s",
            getattr(session, "session_id", "?"),
        )
        return False

    # Patch the caller's read projection only after durable persistence was
    # confirmed, avoiding one ghost reconnect without publishing an uncommitted
    # idle state after a save failure.
    if cleared and original_projection is not authoritative_session:
        try:
            _clear_session_stream_fields(original_projection)
        except Exception:
            pass
    return cleared
def _reconcile_stale_stream_state_for_session_rows(session_rows) -> bool:
    """Clear stale persisted stream fields before /api/sessions serializes rows."""
    changed = False
    for row in session_rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("session_id")
        if not sid or not row.get("active_stream_id"):
            continue
        if row.get("is_streaming") is True:
            continue
        try:
            session = get_session(sid, metadata_only=True)
        except Exception:
            logger.debug(
                "Failed to load session %s while reconciling stale stream state",
                sid,
                exc_info=True,
            )
            continue
        if session is None:
            continue
        changed = _clear_stale_stream_state(session) or changed
    return changed


__routes_exports__ = (
    "_clear_session_stream_fields",
    "_clear_stale_stream_state",
    "_reconcile_stale_stream_state_for_session_rows",
)
