"""Interrupted-turn recovery and stale pending-owner reconciliation."""

from __future__ import annotations

import json
import logging
import threading
import time

import api.config as _cfg
from api.config import LOCK, SESSIONS, SESSION_DIR, SESSION_INDEX_FILE, session_agent_lock as _get_session_agent_lock
from .state_db import get_state_db_session_messages, get_state_db_session_summary
from .process_wakeup import _get_profile_home
from .reconciliation import reconciled_state_db_messages_for_session
from .records import (
    Session,
    _active_stream_ids,
    _append_recovered_pending_turn,
    _append_recovered_turn_to_context,
    _last_message_timestamp,
    _read_file_head,
    is_safe_session_id,
    _latest_user_matches_pending_text,
    _message_matches_pending_checkpoint,
    _message_matches_pending_text,
    _normalize_journal_recovery_text,
)

logger = logging.getLogger(__name__)

_INTERRUPTED_RECOVERED_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'The partial output above was recovered from the run journal, '
    'but the interrupted agent process could not continue.'
)
_INTERRUPTED_NO_OUTPUT_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'The user message above was preserved, but no agent output was recovered.'
)
_INTERRUPTED_PENDING_RETRY_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'Recovering the partial output from the run journal — '
    'reload this session to retry.'
)
# Neutral wording used when the lazy retry path gives up (max attempts reached
# or the marker has been pending longer than _JOURNAL_RETRY_GIVEUP_SECONDS).
_INTERRUPTED_NEUTRAL_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'Partial output may have been lost.'
)

_INTERRUPTION_CAUSE_DETAILS = {
    'process_restart': (
        'Evidence: the WebUI process started after this turn began, so this '
        'looks like a real process crash or restart.'
    ),
    'stream_run_split_brain': (
        'Evidence: the browser response stream was gone but the worker registry '
        'still listed the run. This is a stream/run bookkeeping split-brain.'
    ),
    'lost_worker_bookkeeping': (
        'Evidence: the stream was gone and worker bookkeeping no longer had an '
        'active run for it. This usually means the worker state was lost or '
        'cleaned up without a terminal event.'
    ),
    'unknown': (
        'Evidence: the stream stopped, but the WebUI could not classify the '
        'interruption more precisely.'
    ),
}


def _classify_interruption_cause(
    *, stream_id: str | None = None, pending_started_at=None,
) -> str:
    """Classify the stale live-response state without overstating certainty."""
    try:
        started = float(pending_started_at) if pending_started_at else None
    except (TypeError, ValueError):
        started = None

    if started is not None:
        try:
            if float(getattr(_cfg, 'SERVER_START_TIME', 0.0) or 0.0) > started:
                return 'process_restart'
        except (TypeError, ValueError):
            pass

    if stream_id:
        try:
            if _cfg.runtime_worker_alive(str(stream_id)):
                return 'stream_run_split_brain'
        except Exception:
            pass
        return 'lost_worker_bookkeeping'

    return 'unknown'


def _interrupted_content_for(
    *, recovered_output: bool, pending_retry: bool, interruption_cause: str,
) -> str:
    if recovered_output:
        outcome = (
            'The partial output above was recovered from the run journal, '
            'but the interrupted agent process could not continue.'
        )
    elif pending_retry:
        outcome = (
            'Recovering the partial output from the run journal — '
            'reload this session to retry.'
        )
    else:
        outcome = 'The user message above was preserved, but no agent output was recovered.'
    cause_detail = _INTERRUPTION_CAUSE_DETAILS.get(
        interruption_cause,
        _INTERRUPTION_CAUSE_DETAILS['unknown'],
    )
    return (
        '**Response interrupted.**\n\n'
        'The live response stream stopped before this turn finished. '
        f'{cause_detail} {outcome}'
    )


def _interrupted_recovery_marker(
    *,
    recovered_output: bool = False,
    pending_retry: bool = False,
    stream_id: str | None = None,
    pending_started_at=None,
) -> dict:
    """Build the standard interrupted-turn marker.

    ``recovered_output=True`` means the run journal already yielded visible
    text on this repair pass — the marker advertises that the partial output
    has been recovered.

    ``pending_retry=True`` is the lazy-retry hook: the journal was unreadable
    on this pass (page-cache loss, un-fsynced writes on slow FS, etc.). The
    marker carries a ``_pending_journal_recovery`` flag so a later
    ``get_session()`` can re-attempt recovery without baking a permanent
    "no output" claim into the transcript.

    The two are mutually exclusive; ``recovered_output`` wins if both are
    set so the caller cannot accidentally re-arm retry on a successful
    repair.
    """
    interruption_cause = _classify_interruption_cause(
        stream_id=stream_id,
        pending_started_at=pending_started_at,
    )
    content = _interrupted_content_for(
        recovered_output=recovered_output,
        pending_retry=pending_retry,
        interruption_cause=interruption_cause,
    )
    marker = {
        'role': 'assistant',
        'content': content,
        'timestamp': int(time.time()),
        '_error': True,
        'type': 'interrupted',
        'interruption_cause': interruption_cause,
    }
    if pending_retry and not recovered_output:
        marker['_pending_journal_recovery'] = True
    return marker


def _truncate_journal_tool_args(args, limit: int = 4) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in list(args.items())[:limit]:
        text = str(value)
        out[str(key)] = text[:120] + ('...' if len(text) > 120 else '')
    return out


def _find_existing_assistant_for_journal_content(
    session,
    content: str,
    *,
    max_index: int | None = None,
    excluded_indexes: set[int] | None = None,
) -> int | None:
    candidate = _normalize_journal_recovery_text(content)
    if not candidate:
        return None
    messages = session.messages or []
    stop = len(messages) if max_index is None else min(len(messages), max_index)
    substring_match = None
    for idx in range(stop):
        if excluded_indexes and idx in excluded_indexes:
            continue
        message = messages[idx]
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        if message.get('_error'):
            continue
        existing = _normalize_journal_recovery_text(message.get('content'))
        if not existing:
            continue
        if existing == candidate:
            return idx
        if substring_match is None and len(candidate) >= 24 and candidate in existing:
            substring_match = idx
    return substring_match


def _journal_tool_already_present(
    session,
    name: str,
    preview: str,
    *,
    stream_id: str | None = None,
) -> bool:
    """Return True when an equivalent tool card already exists.

    Matching rule:

    * If the existing tool card carries ``_recovered_stream_id``, that means a
      previous journal-recovery run materialized it.  The retry can safely
      collapse against it only when both stream ids match — otherwise a
      legitimately-repeated tool (e.g. a second ``terminal: ls`` in a
      different turn) would be dropped.
    * If the existing tool card has no ``_recovered_stream_id`` (a live tool
      card, or a tool card carried over from a core transcript that pre-dates
      stream-id tagging), the legacy name+preview match still wins.  This
      preserves the "core transcript already has this tool, don't duplicate
      it" invariant the original repair path established.
    * When ``stream_id`` is omitted, the helper degrades cleanly to its
      pre-fix session-wide behaviour.
    """
    candidate_name = str(name or '')
    candidate_preview = _normalize_journal_recovery_text(preview)
    candidate_stream = str(stream_id) if stream_id else None
    for tool_call in session.tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        if str(tool_call.get('name') or '') != candidate_name:
            continue
        existing_preview = _normalize_journal_recovery_text(
            tool_call.get('preview') or tool_call.get('snippet') or ''
        )
        if existing_preview != candidate_preview:
            continue
        if candidate_stream is not None:
            existing_stream = tool_call.get('_recovered_stream_id')
            # A tool card explicitly tagged with a recovered_stream_id that
            # differs from ours belongs to another retry's turn — don't let
            # it pre-empt this retry.  Untagged tool cards (live or carried
            # over from the core transcript) still match.
            if existing_stream and str(existing_stream) != candidate_stream:
                continue
        return True
    return False


def _run_journal_has_visible_output(session, stream_id: str | None) -> bool:
    if not stream_id:
        return False
    try:
        from api.run_journal import read_run_events
        journal = read_run_events(session.session_id, stream_id)
    except Exception:
        return False
    for event in journal.get('events') or []:
        if not isinstance(event, dict):
            continue
        event_name = str(event.get('event') or event.get('type') or '')
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        if event_name == 'token' and str(payload.get('text') or ''):
            return True
        if event_name == 'interim_assistant':
            if payload.get('already_streamed'):
                continue
            if str(payload.get('text') or '').strip():
                return True
        if event_name == 'reasoning':
            reasoning_text = str(
                payload.get('text') or payload.get('reasoning') or payload.get('thinking') or ''
            )
            if reasoning_text.strip():
                return True
        if event_name == 'tool':
            return True
    return False


def _run_journal_terminal_state(session, stream_id: str | None) -> str | None:
    if not stream_id:
        return None
    try:
        from api.run_journal import latest_run_summary
        summary = latest_run_summary(session.session_id, stream_id)
    except Exception:
        return None
    if not summary.get('terminal'):
        return None
    return str(summary.get('terminal_state') or '') or None


def _journal_is_still_arriving(session, stream_id: str | None) -> bool:
    """Return True for journals that may become visible on a later read.

    `read_run_events()` deliberately collapses missing files and empty files
    into an empty event list, so the lazy retry path needs a small filesystem
    visibility check to avoid burning all retry attempts while WSL2 / network
    filesystems are still surfacing the journal.  Non-empty journals are treated
    as sealed enough for retry-budget accounting; if they contain no visible
    output, the normal capped give-up path handles them.
    """
    if not stream_id:
        return False
    try:
        from api.run_journal import _run_path, latest_run_summary

        path = _run_path(session.session_id, stream_id)
        summary = latest_run_summary(session.session_id, stream_id)
        if summary.get('terminal'):
            return False
        try:
            return (not path.exists()) or path.stat().st_size == 0
        except OSError:
            return True
    except Exception:
        logger.debug(
            "Session %s: failed to classify journal visibility for stream %s",
            getattr(session, 'session_id', '?'),
            stream_id,
            exc_info=True,
        )
        return False


def _append_journaled_partial_output(
    session,
    stream_id: str | None,
    *,
    dedupe_existing: bool = False,
) -> bool:
    """Recover already-emitted visible output from a dead stream journal.

    This repair path is intentionally conservative: it restores user-visible
    assistant text, display-only reasoning, and tool-card metadata that had
    already been emitted over SSE before the WebUI process died. Restored
    reasoning stays out of ``context_messages`` so it cannot become provider-
    facing history. The repair does not try to continue execution.
    """
    if not stream_id:
        return False

    try:
        from api.run_journal import read_run_events
        journal = read_run_events(session.session_id, stream_id)
    except Exception:
        logger.debug(
            "Session %s: failed to read run journal for stream %s",
            getattr(session, 'session_id', '?'),
            stream_id,
            exc_info=True,
        )
        return False

    events = [event for event in journal.get('events') or [] if isinstance(event, dict)]
    if not events:
        return False

    appended_any = False
    assistant_parts: list[str] = []
    reasoning_parts: list[str] = []
    assistant_started_at: float | None = None
    current_assistant_idx: int | None = None
    recovered_tool_calls: list[dict] = []
    initial_message_count = len(session.messages or [])
    claimed_existing_assistant_indexes: set[int] = set()

    def content_match_can_receive_reasoning(existing_idx: int) -> bool:
        messages = session.messages or []
        owner_idx = None
        for candidate_idx in range(existing_idx - 1, -1, -1):
            candidate = messages[candidate_idx]
            if isinstance(candidate, dict) and candidate.get('role') == 'user':
                owner_idx = candidate_idx
                break
        if owner_idx is None:
            return False

        pending_text = _normalize_journal_recovery_text(session.pending_user_message)
        if pending_text and not _message_matches_pending_checkpoint(
            messages[owner_idx],
            session.pending_user_message,
            session.pending_started_at,
            session.pending_user_source,
            session.pending_attachments,
        ):
            return False

        for candidate_idx in range(existing_idx + 1, initial_message_count):
            candidate = messages[candidate_idx]
            if not isinstance(candidate, dict) or candidate.get('role') != 'user':
                continue
            candidate_text = _normalize_journal_recovery_text(candidate.get('content'))
            candidate_matches_checkpoint = pending_text and _message_matches_pending_checkpoint(
                candidate,
                session.pending_user_message,
                session.pending_started_at,
                session.pending_user_source,
                session.pending_attachments,
            )
            if candidate_matches_checkpoint and candidate.get('_recovered'):
                continue
            if pending_text and candidate_text == pending_text:
                return False
            return False
        return True

    def append_context_projection(message: dict) -> None:
        context_projection = dict(message)
        context_projection.pop('reasoning', None)
        _append_recovered_turn_to_context(session, context_projection)

    def attach_display_reasoning(message: dict, reasoning: str) -> bool:
        if not reasoning:
            return False
        existing = str(message.get('reasoning') or '').strip()
        if existing:
            return False
        message['reasoning'] = reasoning
        return True

    def flush_assistant() -> int | None:
        nonlocal appended_any, assistant_parts, reasoning_parts
        nonlocal assistant_started_at, current_assistant_idx
        content = ''.join(assistant_parts).strip()
        reasoning = ''.join(reasoning_parts).strip()
        assistant_parts = []
        reasoning_parts = []
        if not content and not reasoning:
            return current_assistant_idx
        if dedupe_existing and content:
            search_excluded = set(claimed_existing_assistant_indexes)
            existing_idx = None
            while True:
                candidate_idx = _find_existing_assistant_for_journal_content(
                    session,
                    content,
                    max_index=initial_message_count,
                    excluded_indexes=search_excluded,
                )
                if candidate_idx is None:
                    break
                if not reasoning or content_match_can_receive_reasoning(candidate_idx):
                    existing_idx = candidate_idx
                    break
                search_excluded.add(candidate_idx)
            if existing_idx is not None:
                claimed_existing_assistant_indexes.add(existing_idx)
                current_assistant_idx = existing_idx
                assistant_started_at = None
                if 0 <= existing_idx < len(session.messages):
                    existing_message = session.messages[existing_idx]
                    append_context_projection(existing_message)
                    if attach_display_reasoning(existing_message, reasoning):
                        appended_any = True
                return existing_idx
        if dedupe_existing and reasoning and not content:
            for existing_idx in range(initial_message_count):
                if existing_idx in claimed_existing_assistant_indexes:
                    continue
                existing_message = session.messages[existing_idx]
                if not isinstance(existing_message, dict):
                    continue
                if (
                    existing_message.get('_recovered_from_run_journal')
                    and existing_message.get('_recovered_stream_id') == stream_id
                    and existing_message.get('role') == 'assistant'
                    and not str(existing_message.get('content') or '').strip()
                    and str(existing_message.get('reasoning') or '').strip() == reasoning
                ):
                    claimed_existing_assistant_indexes.add(existing_idx)
                    current_assistant_idx = existing_idx
                    assistant_started_at = None
                    return existing_idx
        timestamp = int(assistant_started_at or time.time())
        recovered_assistant = {
            'role': 'assistant',
            'content': content,
            'timestamp': timestamp,
            '_recovered_from_run_journal': True,
            '_recovered_stream_id': stream_id,
        }
        attach_display_reasoning(recovered_assistant, reasoning)
        session.messages.append(recovered_assistant)
        append_context_projection(recovered_assistant)
        current_assistant_idx = len(session.messages) - 1
        assistant_started_at = None
        appended_any = True
        return current_assistant_idx

    def ensure_assistant_anchor(created_at: float | None = None) -> int:
        nonlocal appended_any, current_assistant_idx
        idx = flush_assistant()
        if idx is not None:
            return idx
        # A stream can start with tools before any text. Keep those tools
        # visible after restart with an empty recovered assistant anchor instead
        # of inventing synthetic progress prose.
        #
        # Dedup guard (#3875): reuse an existing empty recovered anchor for THIS
        # stream instead of appending a fresh one. The lazy read-side retry path
        # (_retry_journal_recovery_in_place) re-runs this recovery on repeated
        # get_session() calls, and a tool-first stream that never emitted text
        # has no content to dedup on (flush_assistant() returns early on empty),
        # so without this guard each retry — and each distinct interrupted stream
        # over the session's life — appends another empty anchor. A session that
        # was interrupted-and-recovered many times then accumulates thousands of
        # empty content-less assistant rows, bloating the file and (combined with
        # the render path) painting the transcript blank. One anchor per stream
        # is all that's needed to host its recovered tool cards.
        for _existing_idx in range(len(session.messages) - 1, -1, -1):
            _m = session.messages[_existing_idx]
            if not isinstance(_m, dict):
                continue
            if (
                _m.get('_recovered_from_run_journal')
                and _m.get('_recovered_stream_id') == stream_id
                and _m.get('role') == 'assistant'
                and not str(_m.get('content') or '').strip()
                and not str(_m.get('reasoning') or '').strip()
            ):
                current_assistant_idx = _existing_idx
                return _existing_idx
        session.messages.append({
            'role': 'assistant',
            'content': '',
            'timestamp': int(created_at or time.time()),
            '_recovered_from_run_journal': True,
            '_recovered_stream_id': stream_id,
        })
        current_assistant_idx = len(session.messages) - 1
        appended_any = True
        return current_assistant_idx

    for event in events:
        event_name = str(event.get('event') or event.get('type') or '')
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        created_at = event.get('created_at') if isinstance(event.get('created_at'), (int, float)) else None
        if event_name == 'reasoning':
            text = str(
                payload.get('text') or payload.get('reasoning') or payload.get('thinking') or ''
            )
            if not text:
                continue
            if not assistant_parts and not reasoning_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            reasoning_parts.append(text)
            continue
        if event_name == 'token':
            text = str(payload.get('text') or '')
            if not text:
                continue
            if not assistant_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            assistant_parts.append(text)
            continue
        if event_name == 'interim_assistant':
            if payload.get('already_streamed'):
                flush_assistant()
                continue
            text = str(payload.get('text') or '').strip()
            if not text:
                continue
            if not assistant_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            if assistant_parts and not ''.join(assistant_parts).endswith(('\n', ' ')):
                assistant_parts.append('\n\n')
            assistant_parts.append(text)
            flush_assistant()
            continue
        if event_name == 'tool':
            anchor_idx = flush_assistant()
            if anchor_idx is None:
                anchor_idx = ensure_assistant_anchor(created_at)
            name = str(payload.get('name') or 'tool')
            preview = str(payload.get('preview') or '')
            if dedupe_existing and _journal_tool_already_present(
                session, name, preview, stream_id=stream_id,
            ):
                current_assistant_idx = anchor_idx
                continue
            recovered_tool_calls.append({
                'name': name,
                'preview': preview,
                'snippet': preview,
                'tid': f"journal-{event.get('seq') or len(recovered_tool_calls) + 1}",
                'assistant_msg_idx': anchor_idx,
                'args': _truncate_journal_tool_args(payload.get('args') or {}),
                'done': False,
                '_recovered_from_run_journal': True,
                '_recovered_stream_id': stream_id,
            })
            appended_any = True
            current_assistant_idx = anchor_idx
            continue
        if event_name == 'tool_complete':
            name = str(payload.get('name') or '')
            for tool_call in reversed(recovered_tool_calls):
                if tool_call.get('done'):
                    continue
                if not name or tool_call.get('name') == name:
                    tool_call['done'] = True
                    if payload.get('preview'):
                        tool_call['preview'] = str(payload.get('preview') or '')
                        tool_call['snippet'] = str(payload.get('preview') or '')
                    if payload.get('duration') is not None:
                        tool_call['duration'] = payload.get('duration')
                    tool_call['is_error'] = bool(payload.get('is_error', False))
                    break
            continue
        if event_name in {'done', 'stream_end', 'cancel', 'apperror', 'error'}:
            flush_assistant()

    flush_assistant()
    if recovered_tool_calls:
        session.tool_calls = list(session.tool_calls or []) + recovered_tool_calls
        appended_any = True
    return appended_any


# ── Lazy run-journal recovery (read-side self-heal) ─────────────────────────
#
# When sidecar repair runs before the run-journal for the dead stream is
# visible on disk (page-cache loss on WSL2 9p / DrvFs, an un-fsynced journal
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
