"""Pending-owner reconciliation against core transcripts and compression lineage."""

from __future__ import annotations

import json
import logging
import time

from api.config import (
    LOCK,
    SESSIONS,
    SESSION_DIR,
    SESSION_INDEX_FILE,
    session_agent_lock as _get_session_agent_lock,
)
from ..process_wakeup import _get_profile_home
from ..records import (
    _active_stream_ids,
    _append_recovered_pending_turn,
    _append_recovered_turn_to_context,
    _latest_user_matches_pending_text,
    _message_matches_pending_checkpoint,
    _message_matches_pending_text,
    _normalize_journal_recovery_text,
    _read_file_head,
    is_safe_session_id,
)
from .interruption import _interrupted_recovery_marker
from .journal_replay import (
    _append_journaled_partial_output,
    _run_journal_has_visible_output,
    _run_journal_terminal_state,
)
from .journal_retry import _build_recovery_marker_with_retry_hook

logger = logging.getLogger(__name__)

def _apply_core_sync_or_error_marker(
    session,
    core_path,
    stream_id_for_recheck=None,
    *,
    require_stream_dead=True,
    touch_updated_at=True,
) -> bool:
    """Inner repair logic. Must be called with the per-session lock already held.

    Re-checks session state under the lock, then either syncs messages from the
    core transcript (if present and non-empty) or restores the pending user
    message as a recovered user turn and appends an error marker.

    stream_id_for_recheck: when provided, repair bails if session.active_stream_id
    changed (e.g. context compression rotated it).  The cache-miss repair path
    also requires the stream to be absent from active streams; the streaming
    thread's final fallback passes require_stream_dead=False because it runs
    before its own stream is removed from STREAMS.

    Returns True if repair was applied, False if the re-check bailed out.
    Must never raise — caller is responsible for exception handling.
    """
    sid = session.session_id
    # Bail if pending is unset — nothing to repair.
    if not session.pending_user_message:
        return False
    if stream_id_for_recheck is not None:
        # Bail if active_stream_id rotated between the pre-lock check and now.
        # Cache-miss repair must also skip if the stream is alive again, but the
        # streaming thread's final fallback runs before removing its own stream
        # from STREAMS and must be allowed to repair that same active stream.
        if session.active_stream_id != stream_id_for_recheck:
            return False
        if require_stream_dead and session.active_stream_id in _active_stream_ids():
            return False

    # When messages is already non-empty, do not overwrite history from any core
    # transcript. The pending user turn may still be the only durable copy of a
    # prompt submitted just before a server restart, so materialize it before
    # clearing runtime stream state.
    if len(session.messages) != 0:
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        _already_checkpointed = _message_matches_pending_checkpoint(
            session.messages[-1],
            session.pending_user_message,
            _recovered_ts,
            session.pending_user_source,
            session.pending_attachments,
        )
        _tail_user_already_checkpointed = _already_checkpointed or _message_matches_pending_text(
            session.messages[-1],
            session.pending_user_message,
        )
        _stream_id = stream_id_for_recheck or session.active_stream_id
        _pending_started_at = session.pending_started_at
        if _run_journal_terminal_state(session, _stream_id) == 'completed':
            if not (_already_checkpointed or _latest_user_matches_pending_text(session.messages, session.pending_user_message)):
                _append_recovered_pending_turn(session, timestamp=_recovered_ts)
            _append_journaled_partial_output(
                session,
                _stream_id,
                dedupe_existing=True,
            )
            session.active_stream_id = None
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.pending_user_source = None
            session.save(touch_updated_at=touch_updated_at)
            logger.info(
                "Session %s: cleared stale pending state for completed stream %s without error marker",
                sid,
                _stream_id,
            )
            return True
        if not _tail_user_already_checkpointed:
            _append_recovered_pending_turn(session, timestamp=_recovered_ts)
        else:
            recovered = {
                'role': 'user',
                'content': session.pending_user_message,
                'timestamp': _recovered_ts,
                '_recovered': True,
            }
            pending_source = getattr(session, 'pending_user_source', None)
            if pending_source and pending_source != 'webui':
                recovered['_source'] = pending_source
            if session.pending_attachments:
                recovered['attachments'] = list(session.pending_attachments)
            _append_recovered_turn_to_context(session, recovered)
        recovered_output = _append_journaled_partial_output(
            session,
            _stream_id,
        )
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        session.messages.append(
            _build_recovery_marker_with_retry_hook(
                recovered_output=recovered_output,
                stream_id=_stream_id,
                pending_started_at=_pending_started_at,
            )
        )
        session.save(touch_updated_at=touch_updated_at)
        logger.info(
            "Session %s: recovered pending user turn (messages non-empty), added error marker",
            sid,
        )
        return True

    # ── messages *is* empty ─ full repair ─────────────────────────────────

    if core_path.exists():
        with open(core_path, encoding='utf-8') as f:
            core = json.load(f)
        core_messages = core.get('messages', [])
        if core_messages:
            _stream_id = stream_id_for_recheck or session.active_stream_id
            session.messages = core_messages
            session.tool_calls = core.get('tool_calls', [])
            for field in ('input_tokens', 'output_tokens', 'estimated_cost'):
                if core.get(field) is not None:
                    setattr(session, field, core[field])
            _pending_text = _normalize_journal_recovery_text(session.pending_user_message)
            _recovered_ts = int(time.time())
            if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
                _recovered_ts = int(session.pending_started_at)
            _already_checkpointed = _message_matches_pending_checkpoint(
                session.messages[-1] if session.messages else None,
                session.pending_user_message,
                _recovered_ts,
                session.pending_user_source,
                session.pending_attachments,
            )
            _tail_user_already_checkpointed = _already_checkpointed or _message_matches_pending_text(
                session.messages[-1] if session.messages else None,
                session.pending_user_message,
            )
            if (
                _pending_text
                and not _tail_user_already_checkpointed
                and _run_journal_has_visible_output(session, _stream_id)
            ):
                _append_recovered_pending_turn(session, timestamp=_recovered_ts)
            recovered_output = _append_journaled_partial_output(
                session,
                _stream_id,
                dedupe_existing=True,
            )
            _pending_started_at = session.pending_started_at
            session.active_stream_id = None
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.pending_user_source = None
            if recovered_output:
                session.messages.append(
                    _interrupted_recovery_marker(
                        recovered_output=True,
                        stream_id=_stream_id,
                        pending_started_at=_pending_started_at,
                    )
                )
            # NOTE: when the core transcript was synced in but the run journal
            # is not yet visible, intentionally do NOT append a lazy-retry
            # marker here. In this branch the canonical history is the core
            # transcript itself (which has already been written to s.messages
            # above) and the marker is purely advisory — the existing contract
            # is "marker only when there is a recovered partial turn to
            # annotate". Adding a pending-retry marker on every empty-journal
            # core-sync would surface a spurious "reload to retry" banner on
            # sessions whose journal is legitimately absent (e.g. archived
            # streams). The first and third branches handle the lost-response
            # case where the marker is the only signal the user gets.
            session.save(touch_updated_at=touch_updated_at)
            logger.info(
                "Session %s: synced %d messages from core transcript%s",
                sid,
                len(core_messages),
                " and recovered journaled output" if recovered_output else "",
            )
            return True

    # Core missing or empty — restore the pending user message as a recovered
    # user turn (preserving the draft), then append an error marker.
    if session.pending_user_message:
        # Use the original send time if available so the recovered turn
        # appears in the correct chronological position.
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        _append_recovered_pending_turn(session, timestamp=_recovered_ts)
    recovered_output = _append_journaled_partial_output(
        session,
        stream_id_for_recheck or session.active_stream_id,
    )
    _stream_id = stream_id_for_recheck or session.active_stream_id
    _pending_started_at = session.pending_started_at
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.messages.append(
        _build_recovery_marker_with_retry_hook(
            recovered_output=recovered_output,
            stream_id=_stream_id,
            pending_started_at=_pending_started_at,
        )
    )
    session.save(touch_updated_at=touch_updated_at)
    logger.info("Session %s: no core transcript found, added error marker", sid)
    return True


# ── _repair_stale_pending grace period (#1624) ─────────────────────────────
#
# Defense-in-depth against a narrow race between the streaming thread clearing
# pending_user_message and STREAMS.pop(stream_id). Without this guard, any
# fast turn (e.g. command approval) that exits the thread before the on-disk
# pending clear has flushed gets misdiagnosed as a crashed turn, producing a
# spurious "Response interrupted." marker.
#
# 30s covers the worst-case post-loop persistence window: LLM finishing a tool
# batch + lock contention with the checkpoint thread + a multi-MB session.save.
# A legitimately crashed turn whose pending_started_at is < 30s old will not
# repair on the first get_session() call, but WILL repair on the next call
# after the grace period elapses (typically the user's next interaction).
#
# Missing/falsy pending_started_at (legacy sidecars from before that field
# existed, or any path that forgot to set it) is treated as "old enough" so
# repair still recovers them — preserves current behavior for legacy data.
_REPAIR_STALE_PENDING_GRACE_SECONDS = 30


def _has_compression_continuation(session) -> bool:
    """Return True when ``session`` is an archived compression parent.

    Context compression rotates the live WebUI session id: the old sidecar is
    preserved for lineage while the new child owns the running/completed turn.
    Stale-pending repair must not append an interruption marker to that old
    parent just because its stream bookkeeping disappeared after the rotation.
    """
    sid = getattr(session, 'session_id', None)
    if not sid:
        return False

    def _row_is_continuation(row) -> bool:
        if not isinstance(row, dict):
            return False
        child_sid = row.get('session_id')
        if not child_sid or child_sid == sid:
            return False
        if row.get('parent_session_id') != sid:
            return False
        # Any child row is enough evidence that this pending state belongs to a
        # compression lineage, not a dead standalone turn. The child may itself
        # temporarily carry a bad pre_compression_snapshot flag from older code;
        # do not filter it out here or the guard misses the exact regression.
        return True

    try:
        with LOCK:
            for child in SESSIONS.values():
                if getattr(child, 'session_id', None) == sid:
                    continue
                if getattr(child, 'parent_session_id', None) == sid:
                    return True
    except Exception:
        pass

    try:
        if SESSION_INDEX_FILE.exists():
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
            if isinstance(entries, list) and any(_row_is_continuation(e) for e in entries):
                return True
    except Exception:
        logger.debug("Failed to inspect session index for compression continuation", exc_info=True)

    # Index rows can lag behind rapid compression/save races. Fall back to a
    # shallow JSON metadata scan; session files write parent_session_id before
    # the messages array, so this avoids loading multi-MB transcripts.
    try:
        needle = f'"parent_session_id": "{sid}"'
        for path in SESSION_DIR.glob('*.json'):
            if path.name.startswith('_') or path.stem == sid:
                continue
            try:
                # Preserve the old read_text()[:4096] CHARACTER-prefix semantics
                # with bounded I/O: a UTF-8 char is at most 4 bytes, so 4096 chars
                # fit in <=16384 bytes. Reading bytes then slicing to 4096 chars
                # avoids a regression where a multi-byte (e.g. emoji) compression
                # summary written before parent_session_id pushes the needle past a
                # 4096-BYTE cutoff even though it was within the old 4096-CHAR one.
                head = _read_file_head(path, max_prefix_bytes=16384)[:4096]
            except OSError:
                continue
            if needle in head:
                return True
    except Exception:
        logger.debug("Failed to scan session files for compression continuation", exc_info=True)

    return False


def _repair_stale_pending(session) -> bool:
    """Recover a sidecar stuck with messages=[] and stale pending state.

    Fires only when messages is empty, pending_user_message is set,
    active_stream_id is set, the stream is no longer alive, AND the turn is
    older than _REPAIR_STALE_PENDING_GRACE_SECONDS (#1624).

    Uses a non-blocking lock acquire so a caller that already holds the
    per-session lock (e.g. retry_last, undo_last, cancel_stream) cannot
    deadlock when get_session() triggers this on a cache miss.

    Returns True if repair was applied, False otherwise.
    Must never raise — all errors are caught and logged.
    """
    # Capture the stream id seen at pre-check time; the under-lock re-check in
    # _apply_core_sync_or_error_marker uses this to detect a rotated active_stream_id
    # (e.g. context compression) or a stream that came back alive.
    _seen_stream_id = session.active_stream_id
    if (not session.pending_user_message
            or not _seen_stream_id
            or _seen_stream_id in _active_stream_ids()):
        return False
    if getattr(session, 'pre_compression_snapshot', False):
        logger.debug(
            "_repair_stale_pending: skipping pre-compression snapshot %s",
            getattr(session, 'session_id', '?'),
        )
        return False
    if _has_compression_continuation(session):
        logger.debug(
            "_repair_stale_pending: skipping compression parent %s with continuation",
            getattr(session, 'session_id', '?'),
        )
        return False

    # Grace-period guard: bail if the turn is too fresh to be a real crash.
    # Falsy pending_started_at (None, 0, missing) means "old enough" — preserve
    # legacy-data recovery semantics for sessions that pre-date the field.
    _started = getattr(session, 'pending_started_at', None)
    if _started:
        try:
            _age = time.time() - float(_started)
        except (TypeError, ValueError):
            _age = float('inf')
        if _age < _REPAIR_STALE_PENDING_GRACE_SECONDS:
            logger.debug(
                "_repair_stale_pending: skipping repair for session %s — "
                "pending_started_at age=%.1fs < %ds grace window",
                session.session_id, _age, _REPAIR_STALE_PENDING_GRACE_SECONDS,
            )
            return False
    else:
        # Treat missing/falsy pending_started_at as "old enough" (legacy data).
        _age = float('inf')

    sid = session.session_id
    if not is_safe_session_id(sid):
        return False

    try:
        profile_home = _get_profile_home(session.profile)
        core_path = profile_home / 'sessions' / f'session_{sid}.json'

        lock = _get_session_agent_lock(sid)
        # Non-blocking acquire: bail immediately if the caller already holds this
        # lock (e.g. retry_last, undo_last, cancel_stream). Blocking would deadlock
        # because _get_session_agent_lock returns a non-reentrant threading.Lock.
        if not lock.acquire(blocking=False):
            logger.debug(
                "_repair_stale_pending: lock contended, skipping repair for session %s", sid,
            )
            return False
        try:
            # Telemetry (#1624): log legitimate repair firings so the next batch
            # of user reports tells us whether the underlying race still fires
            # post-fix. Rate-limit by age (Opus pre-release SHOULD-FIX): WARNING
            # for the diagnostically valuable race window (< 5 min — actual
            # leak-path candidates that slipped past the grace guard) and DEBUG
            # for the long-tail (orphaned sidecars from prior process lifetimes)
            # so reconnect loops on stuck sessions don't flood the log.
            _DIAG_WARN_WINDOW_SECONDS = 300  # 5 min
            _age_str = ('inf' if _age == float('inf') else f'{_age:.1f}s')
            _log = logger.warning if _age < _DIAG_WARN_WINDOW_SECONDS else logger.debug
            _log(
                "_repair_stale_pending firing: session=%s stream_id=%s pending_age=%s",
                sid, _seen_stream_id, _age_str,
            )
            return _apply_core_sync_or_error_marker(
                session, core_path, stream_id_for_recheck=_seen_stream_id,
            )
        finally:
            lock.release()
    except Exception:
        logger.exception("_repair_stale_pending failed for session %s", sid)
        return False
