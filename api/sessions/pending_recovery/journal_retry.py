"""Bounded, idempotent retry for journals that arrive after sidecar repair."""

from __future__ import annotations

import logging
import threading
import time

from .interruption import (
    _INTERRUPTED_NEUTRAL_WORDING,
    _INTERRUPTED_RECOVERED_WORDING,
    _interrupted_recovery_marker,
)
from .journal_replay import (
    _append_journaled_partial_output,
    _journal_is_still_arriving,
)

logger = logging.getLogger(__name__)

# tail, a slow network FS, …), `_append_journaled_partial_output` returns
# False even though the journaled events will appear on disk shortly. Without
# the helpers below the repair path baked a permanent "no agent output was
# recovered" claim into the marker, and a later session read could never
# correct it.
#
# The contract is:
#
#   * Sidecar repair (`_apply_core_sync_or_error_marker`) writes a marker
#     with `_pending_journal_recovery=True` whenever it could not recover
#     visible output AND the stream id is known. Three retry-meta keys go
#     onto the marker: `_journal_retry_stream_id`, `_journal_retry_attempts`,
#     `_journal_retry_first_seen_ts`.
#   * Every `get_session()` call that returns the full session checks the
#     latest assistant marker; if the flag is set it re-runs
#     `_append_journaled_partial_output` with `dedupe_existing=True`. On
#     success the marker is promoted in place to the recovered-output
#     wording, the journaled rows are reordered to sit above the marker,
#     and all retry meta is stripped. If the journal is still missing or
#     zero-byte, the retry is a no-op and does not consume attempt budget.
#     Terminal/non-useful journals consume attempt budget and can demote
#     immediately at the max-attempt cap.
#   * After `_JOURNAL_RETRY_MAX_ATTEMPTS` failed retries or
#     `_JOURNAL_RETRY_GIVEUP_SECONDS` of wall-clock age, the marker is
#     demoted to the neutral wording ("Partial output may have been lost.")
#     so users do not see "reload to retry" prompts forever.


_JOURNAL_RETRY_MAX_ATTEMPTS = 12
_JOURNAL_RETRY_GIVEUP_SECONDS = 24 * 3600
_JOURNAL_RETRY_LOCKS: dict[str, threading.Lock] = {}
_JOURNAL_RETRY_LOCKS_GUARD = threading.Lock()


def _journal_retry_lock_for_sid(sid: str) -> threading.Lock:
    with _JOURNAL_RETRY_LOCKS_GUARD:
        return _JOURNAL_RETRY_LOCKS.setdefault(str(sid), threading.Lock())


def _build_recovery_marker_with_retry_hook(
    *, recovered_output: bool, stream_id: str | None, pending_started_at=None,
) -> dict:
    """Build an interrupted-turn marker, arming the lazy-retry hook when
    visible output was not recovered yet but a stream id is available."""
    if recovered_output:
        return _interrupted_recovery_marker(
            recovered_output=True,
            stream_id=stream_id,
            pending_started_at=pending_started_at,
        )
    if not stream_id:
        return _interrupted_recovery_marker(
            recovered_output=False,
            pending_started_at=pending_started_at,
        )
    marker = _interrupted_recovery_marker(
        pending_retry=True,
        stream_id=stream_id,
        pending_started_at=pending_started_at,
    )
    marker['_journal_retry_stream_id'] = str(stream_id)
    marker['_journal_retry_attempts'] = 0
    marker['_journal_retry_first_seen_ts'] = int(time.time())
    return marker


def _session_has_pending_journal_retry(session) -> bool:
    """Cheap short-circuit: scan from the tail until the most recent normal
    assistant turn. Any `_pending_journal_recovery` flag found before then
    means a retry is queued.
    """
    messages = getattr(session, 'messages', None) or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get('_pending_journal_recovery'):
            return True
        if msg.get('role') == 'assistant' and not msg.get('_error'):
            # A normal assistant turn after any pending marker — nothing to
            # retry above this point.
            return False
    return False


def _strip_journal_retry_meta(marker: dict) -> None:
    marker.pop('_pending_journal_recovery', None)
    marker.pop('_journal_retry_stream_id', None)
    marker.pop('_journal_retry_attempts', None)
    marker.pop('_journal_retry_first_seen_ts', None)


def _reorder_journal_tail_above_marker(session, marker_idx: int) -> None:
    """Move `_recovered_from_run_journal=True` rows appended *after*
    ``marker_idx`` to sit immediately above the marker so chronological
    order is preserved (journaled output happened during the turn, marker
    annotates its end).
    """
    messages = session.messages
    if marker_idx < 0 or marker_idx >= len(messages):
        return
    tail = messages[marker_idx + 1 :]
    if not tail:
        return
    journaled = [
        m for m in tail
        if isinstance(m, dict) and m.get('_recovered_from_run_journal')
    ]
    if not journaled:
        return
    rest = [
        m for m in tail
        if not (isinstance(m, dict) and m.get('_recovered_from_run_journal'))
    ]
    marker = messages[marker_idx]
    new_messages = (
        messages[:marker_idx]
        + journaled
        + [marker]
        + rest
    )
    # Rebase any tool_calls.assistant_msg_idx values that pointed into the
    # journaled rows when they were appended at the tail.
    old_journaled_idx_base = marker_idx + 1
    new_journaled_idx_base = marker_idx
    shift = new_journaled_idx_base - old_journaled_idx_base  # = -1
    for tool_call in session.tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        idx = tool_call.get('assistant_msg_idx')
        if isinstance(idx, int) and idx >= old_journaled_idx_base \
                and idx < old_journaled_idx_base + len(journaled):
            tool_call['assistant_msg_idx'] = idx + shift
    session.messages = new_messages


def _try_retry_journal_recovery_in_place(session) -> bool:
    sid = str(getattr(session, 'session_id', '') or '')
    lock = _journal_retry_lock_for_sid(sid)
    if not lock.acquire(blocking=False):
        logger.debug("lazy journal-retry already running for session %s", sid)
        return False
    try:
        return _retry_journal_recovery_in_place(
            session, preserve_arriving_budget=True,
        )
    finally:
        lock.release()
        with _JOURNAL_RETRY_LOCKS_GUARD:
            if _JOURNAL_RETRY_LOCKS.get(sid) is lock:
                _JOURNAL_RETRY_LOCKS.pop(sid, None)


def _retry_journal_recovery_in_place(
    session,
    *,
    preserve_arriving_budget: bool = False,
) -> bool:
    """Re-attempt run-journal recovery for the most recent pending marker.

    Returns True if the marker was promoted to the recovered-output wording.
    Never raises — caller is best-effort.
    """
    try:
        messages = session.messages or []
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if not isinstance(msg, dict):
                continue
            if msg.get('role') == 'assistant' and not msg.get('_error') \
                    and not msg.get('_pending_journal_recovery'):
                # Walked past the pending marker without finding it.
                return False
            if not (
                msg.get('type') == 'interrupted'
                and msg.get('_pending_journal_recovery')
            ):
                continue
            stream_id = msg.get('_journal_retry_stream_id')
            first_seen = msg.get('_journal_retry_first_seen_ts') or 0
            attempts = int(msg.get('_journal_retry_attempts') or 0)
            now = time.time()
            give_up = (
                attempts >= _JOURNAL_RETRY_MAX_ATTEMPTS
                or (
                    first_seen
                    and now - float(first_seen) > _JOURNAL_RETRY_GIVEUP_SECONDS
                )
            )
            if not stream_id:
                # No stream id to retry against; demote immediately.
                msg['content'] = _INTERRUPTED_NEUTRAL_WORDING
                _strip_journal_retry_meta(msg)
                try:
                    session.save(touch_updated_at=False)
                except Exception:
                    logger.debug(
                        "save() failed while demoting marker for session %s",
                        getattr(session, 'session_id', '?'),
                        exc_info=True,
                    )
                return False
            if give_up:
                msg['content'] = _INTERRUPTED_NEUTRAL_WORDING
                _strip_journal_retry_meta(msg)
                try:
                    session.save(touch_updated_at=False)
                except Exception:
                    logger.debug(
                        "save() failed while demoting marker for session %s",
                        getattr(session, 'session_id', '?'),
                        exc_info=True,
                    )
                return False
            tail_len_before = len(session.messages)
            ok = _append_journaled_partial_output(
                session, stream_id, dedupe_existing=True,
            )
            if ok:
                msg['content'] = _INTERRUPTED_RECOVERED_WORDING
                _strip_journal_retry_meta(msg)
                # The journaled rows were appended at the end of messages;
                # only the rows past the previous tail count as "newly
                # journaled" and need to move above the marker.
                _ = tail_len_before  # informational; helper below scans
                _reorder_journal_tail_above_marker(session, idx)
                try:
                    session.save(touch_updated_at=False)
                except Exception:
                    logger.debug(
                        "save() failed while promoting marker for session %s",
                        getattr(session, 'session_id', '?'),
                        exc_info=True,
                    )
                logger.info(
                    "Session %s: lazy journal-recovery promoted marker for "
                    "stream %s after %d attempts",
                    getattr(session, 'session_id', '?'),
                    stream_id,
                    attempts,
                )
                return True
            if (
                preserve_arriving_budget
                and _journal_is_still_arriving(session, stream_id)
            ):
                logger.debug(
                    "Session %s: journal for stream %s still arriving; "
                    "preserving retry budget",
                    getattr(session, 'session_id', '?'),
                    stream_id,
                )
                return False
            next_attempts = attempts + 1
            if next_attempts >= _JOURNAL_RETRY_MAX_ATTEMPTS:
                msg['content'] = _INTERRUPTED_NEUTRAL_WORDING
                _strip_journal_retry_meta(msg)
            else:
                msg['_journal_retry_attempts'] = next_attempts
            try:
                session.save(touch_updated_at=False)
            except Exception:
                logger.debug(
                    "save() failed while updating retry counter for session %s",
                    getattr(session, 'session_id', '?'),
                    exc_info=True,
                )
            return False
        return False
    except Exception:
        logger.exception(
            "_retry_journal_recovery_in_place failed for session %s",
            getattr(session, 'session_id', '?'),
        )
        return False
