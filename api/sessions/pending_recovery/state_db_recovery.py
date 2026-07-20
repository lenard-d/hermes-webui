"""Fail-closed sidecar self-heal from the authoritative Hermes state database."""

from __future__ import annotations

import logging
import time

from api.config import session_agent_lock as _get_session_agent_lock
from ..reconciliation_projection import reconciled_state_db_messages_for_session
from ..records import (
    Session,
    _active_stream_ids,
    _last_message_timestamp,
    is_safe_session_id,
)
from ..state_db_messages import get_state_db_session_messages, get_state_db_session_summary
from .sidecar_recovery import _REPAIR_STALE_PENDING_GRACE_SECONDS

logger = logging.getLogger(__name__)

def _sync_sidecar_from_state_db_if_newer(session) -> bool:
    """Read-side self-heal when WebUI sidecar lags Hermes state.db.

    A WebUI stream can lose its terminal ``done``/``stream_end`` path while the
    underlying agent continues writing messages to ``state.db``. In that shape
    the browser briefly shows live SSE output, but a refresh reloads the stale
    sidecar JSON and the already-produced text appears to vanish. Reconcile the
    sidecar from state.db whenever the state transcript is visibly newer than
    the sidecar, even if the sidecar still carries an ``active_stream_id``.

    This deliberately reuses the existing append-only reconciler so workspace
    prefixes, timestamp drift, compaction watermarks, and tool metadata keep the
    same semantics as normal WebUI/state.db display merging.
    """
    if session is None or getattr(session, '_loaded_metadata_only', False):
        return False
    sid = getattr(session, 'session_id', None)
    if not sid or not is_safe_session_id(sid):
        return False
    seen_stream_id = getattr(session, 'active_stream_id', None)
    has_unfinished_sidecar_turn = bool(
        seen_stream_id or getattr(session, 'pending_user_message', None)
    )
    if not has_unfinished_sidecar_turn:
        return False
    # Never reconcile while the sidecar's stream is still a LIVE in-process
    # worker. A running turn owns the final writeback (it merges the agent
    # result and clears pending state itself); racing it here would drop its
    # active_stream_id mid-run and make the normal terminal writeback skip as
    # "stale". Only self-heal once the worker is gone from both the SSE
    # (STREAMS) and worker-lifecycle (ACTIVE_RUNS) registries.
    if seen_stream_id and seen_stream_id in _active_stream_ids():
        return False
    # Registration-window grace guard (mirrors _repair_stale_pending). A turn is
    # registered in STREAMS/ACTIVE_RUNS by the worker thread a moment AFTER the
    # request handler persists active_stream_id + pending_started_at to the
    # sidecar. Within that window the stream is legitimately in flight yet not
    # yet visible in the registries, so the liveness check above would
    # mis-classify it as a dead stream. A recent pending_started_at means "still
    # starting up" — bail. This also covers cross-process / gateway turns the
    # local registries cannot see. Falsy pending_started_at (None/0/missing) is
    # treated as "old enough" so legacy/orphaned sidecars still self-heal.
    if seen_stream_id:
        _started = getattr(session, 'pending_started_at', None)
        if _started:
            try:
                _age = time.time() - float(_started)
            except (TypeError, ValueError):
                _age = float('inf')
            if _age < _REPAIR_STALE_PENDING_GRACE_SECONDS:
                return False

    try:
        state_summary = get_state_db_session_summary(
            sid,
            profile=getattr(session, 'profile', None),
        )
        state_count = int(state_summary.get('message_count') or 0)
        state_last = float(state_summary.get('last_message_at') or 0.0)
    except Exception:
        logger.debug("state.db summary check failed for session %s", sid, exc_info=True)
        return False
    if state_count <= 0:
        return False

    sidecar_messages = list(getattr(session, 'messages', None) or [])
    sidecar_count = len(sidecar_messages)
    sidecar_last = _last_message_timestamp(sidecar_messages) or 0.0

    # Fast negative (pre-lock): if state.db is not ahead by either count or
    # timestamp, do not pay for the lock. This keeps normal reads cheap.
    if state_count <= sidecar_count and state_last <= sidecar_last:
        return False

    # ── Under-lock critical section ──────────────────────────────────────────
    # The merge + sidecar write must hold the per-session lock so a concurrent
    # worker/checkpoint save can neither (a) be clobbered by a stale full-record
    # write here, nor (b) revive the stream between our liveness check and our
    # write. Non-blocking acquire: if a caller already holds the lock (retry_last,
    # undo_last, cancel_stream, the streaming worker's own finalize), bail rather
    # than deadlock — a later read will retry the self-heal.
    lock = _get_session_agent_lock(sid)
    if not lock.acquire(blocking=False):
        logger.debug(
            "state.db newer-sidecar sync: lock contended, skipping for session %s", sid,
        )
        return False
    try:
        # Re-load the authoritative on-disk session under the lock so we both
        # validate against (and write back) the very latest sidecar — never a
        # snapshot captured before the lock that could clobber a newer write.
        try:
            locked = Session.load(sid)
        except Exception:
            logger.debug(
                "state.db newer-sidecar sync: locked reload failed for session %s",
                sid, exc_info=True,
            )
            return False
        if locked is None:
            return False

        # Re-check liveness conditions against the freshly-loaded state: the
        # stream may have rotated (compression), come back alive, terminated and
        # cleared its own pending state, or had its turn finalized while we
        # waited. Any of these means there is nothing stale to repair.
        locked_stream_id = getattr(locked, 'active_stream_id', None)
        if locked_stream_id != seen_stream_id:
            return False
        if not (locked_stream_id or getattr(locked, 'pending_user_message', None)):
            return False
        if locked_stream_id and locked_stream_id in _active_stream_ids():
            return False
        if locked_stream_id:
            _lstarted = getattr(locked, 'pending_started_at', None)
            if _lstarted:
                try:
                    _lage = time.time() - float(_lstarted)
                except (TypeError, ValueError):
                    _lage = float('inf')
                if _lage < _REPAIR_STALE_PENDING_GRACE_SECONDS:
                    return False

        locked_messages = list(getattr(locked, 'messages', None) or [])
        locked_count = len(locked_messages)

        state_messages = get_state_db_session_messages(
            sid,
            profile=getattr(locked, 'profile', None),
        )
        if not state_messages:
            return False
        merged_messages = reconciled_state_db_messages_for_session(
            locked,
            state_messages=state_messages,
        )
        # The reconciler is append-only: a genuine state.db advance (output the
        # lost stream never wrote back) shows up as MORE rows than the sidecar.
        # A merged length not greater than the sidecar means nothing new to
        # recover — leave the sidecar untouched rather than rewriting in place.
        if len(merged_messages) <= locked_count:
            return False
        merged_context = reconciled_state_db_messages_for_session(
            locked,
            prefer_context=True,
            state_messages=state_messages,
        )

        # Mutate + persist the freshly-loaded, locked object. Because we hold the
        # lock and reloaded under it, this save cannot clobber a concurrent
        # writer's newer record.
        locked.messages = merged_messages
        locked.context_messages = merged_context
        locked.active_stream_id = None
        locked.pending_user_message = None
        locked.pending_attachments = []
        locked.pending_started_at = None
        locked.pending_user_source = None
        try:
            locked.save(touch_updated_at=True)
        except Exception:
            logger.debug(
                "state.db newer-sidecar sync save failed for session %s",
                sid, exc_info=True,
            )
            return False

        # Durable write succeeded — reflect the reconciled state on the caller's
        # shared/cached object so the in-flight read returns the recovered data.
        session.messages = merged_messages
        session.context_messages = merged_context
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        logger.info(
            "Session %s: synced sidecar from newer state.db transcript (%d -> %d messages)",
            sid,
            locked_count,
            len(merged_messages),
        )
        return True
    finally:
        lock.release()
