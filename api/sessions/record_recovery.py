"""Recovery-time message/context projections for durable session records."""

from __future__ import annotations

import time

import api.config as _cfg

def _content_has_reasoning_only_parts(content) -> bool:
    if not isinstance(content, list) or not content:
        return False
    saw_reasoning = False
    for part in content:
        if not isinstance(part, dict):
            if str(part or '').strip():
                return False
            continue
        part_type = str(part.get('type') or '').lower()
        if part_type in {'thinking', 'reasoning'}:
            text = part.get('thinking') or part.get('reasoning') or part.get('text') or ''
            if str(text).strip():
                saw_reasoning = True
            continue
        if part_type == 'text' and str(part.get('text') or part.get('content') or '').strip():
            return False
        if part_type not in {'text', 'thinking', 'reasoning'}:
            return False
    return saw_reasoning


def _active_stream_ids():
    # Runtime ownership combines live transports with detached workers so stale
    # repair cannot misclassify a provider-blocked or cancelling run as dead.
    return set(_cfg.runtime_active_run_ids())


def _recovered_model_context_projection(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return None
    projected = dict(message)
    projected.pop('reasoning', None)
    if projected.get('_error'):
        return None
    if _content_has_reasoning_only_parts(projected.get('content')):
        if projected.get('tool_calls'):
            projected['content'] = ''
        else:
            return None
    projected_text = _normalize_journal_recovery_text(projected.get('content'))
    if not projected_text and not projected.get('tool_call_id') and not projected.get('tool_calls'):
        return None
    return projected


def _append_recovered_context_projection(
    session,
    context_messages: list,
    recovered: dict,
) -> None:
    recovered_text = _normalize_journal_recovery_text(recovered.get('content'))
    if recovered_text:
        if recovered.get('role') == 'user':
            if _message_matches_pending_checkpoint(
                context_messages[-1] if context_messages else None,
                recovered.get('content'),
                recovered.get('timestamp'),
                recovered.get('_source'),
                recovered.get('attachments'),
            ):
                return
        else:
            for existing in reversed(context_messages[-8:]):
                if not isinstance(existing, dict) or existing.get('role') != recovered.get('role'):
                    continue
                if _normalize_journal_recovery_text(existing.get('content')) == recovered_text:
                    return
    context_messages.append(dict(recovered))


def _seed_recovered_context_from_messages(session, context_messages: list) -> None:
    for message in getattr(session, 'messages', None) or []:
        projected = _recovered_model_context_projection(message)
        if projected is None:
            continue
        context_messages.append(projected)


def _append_recovered_turn_to_context(session, recovered: dict) -> None:
    context_messages = getattr(session, 'context_messages', None)
    if not isinstance(context_messages, list):
        context_messages = []
        session.context_messages = context_messages
    if not context_messages:
        _seed_recovered_context_from_messages(session, context_messages)
    projected = _recovered_model_context_projection(recovered)
    if projected is None:
        return
    _append_recovered_context_projection(session, context_messages, projected)


def _append_recovered_pending_turn(session, *, timestamp: int | None = None) -> dict | None:
    pending_text = str(session.pending_user_message or '')
    if not pending_text:
        return None
    recovered_ts = int(time.time())
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        recovered_ts = int(timestamp)
    recovered: dict = {
        'role': 'user',
        'content': session.pending_user_message,
        'timestamp': recovered_ts,
        '_recovered': True,
    }
    pending_source = getattr(session, 'pending_user_source', None)
    if pending_source and pending_source != 'webui':
        recovered['_source'] = pending_source
    if session.pending_attachments:
        recovered['attachments'] = list(session.pending_attachments)
    session.messages.append(recovered)
    _append_recovered_turn_to_context(session, recovered)
    # The new user turn is now committed to messages (#3831): advance the
    # truncation watermark to the new message's timestamp so that
    # merge_session_messages_append_only() still filters out replaced
    # pre-edit rows from state.db whose timestamps fall below the boundary.
    # The merge's sidecar_advanced_past_watermark guard allows state.db rows
    # newer than the watermark, so post-edit turns are not dropped.
    # Never 0.0 (the truncate-to-empty sentinel, #2914).
    if getattr(session, 'truncation_watermark', None):
        session.truncation_watermark = recovered_ts
    return recovered


def _is_streaming_session(active_stream_id, active_stream_ids):
    return bool(active_stream_id and active_stream_id in active_stream_ids)

def _session_sort_timestamp(session):
    if isinstance(session, dict):
        return session.get('last_message_at') or session.get('updated_at') or 0
    return _last_message_timestamp(getattr(session, 'messages', None)) or getattr(session, 'updated_at', 0) or 0


def _message_timestamp(message):
    if not isinstance(message, dict):
        return None
    raw = message.get('_ts') or message.get('timestamp')
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_empty_partial_activity_message(message):
    """Return True for cancelled/recovered activity rows with no reply text."""
    if not isinstance(message, dict):
        return False
    if message.get('role') != 'assistant' or not message.get('_partial'):
        return False
    content = message.get('content', '')
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text' and str(part.get('text') or part.get('content') or '').strip():
                    return False
                continue
            if str(part or '').strip():
                return False
        return True
    return not str(content or '').strip()


def _last_message_timestamp(messages, *, tail_window: int = 8):
    """perf(session-load-latency) Priority 1: bounded tail-scan.

    Old behavior: reversed-iterate ALL messages until a non-tool, non-empty
    message's timestamp is found. For a 2,730-message session on eMMC, that's
    ~500ms of Python attribute lookups, repeated on every /api/session
    response.

    New behavior: the messages array is chronologically ordered, so the
    last non-tool message is at the very end. We scan only the last
    ``tail_window`` messages — covers the realistic case where 1-3 tool
    rows sit after the last assistant/user message. Falls back to a full
    scan only when no timestamp is found in the window, which preserves
    exact correctness for messages with very large trailing tool clusters
    (rare in practice; we'd need >8 consecutive tool rows to hit it).
    """
    if not isinstance(messages, list):
        return None
    n = len(messages)
    start = max(0, n - max(1, int(tail_window)))
    # Walk from the end backwards. reversed() over a slice still creates
    # a full reverse iterator, but only the slice's elements are touched.
    for message in reversed(messages[start:]):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    # Window miss — fall back to the original full-reversed scan. The
    # caller pays this cost only when the heuristic didn't find a hit,
    # which means the session is unusual (long tool tail or all-empty
    # messages).
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    return None


def _message_role(message):
    if not isinstance(message, dict):
        return ''
    return str(message.get('role', '')).strip().lower()


def _normalize_journal_recovery_text(value) -> str:
    return " ".join(str(value or "").split())


def _message_matches_pending_checkpoint(message, pending_text, timestamp, source, attachments):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    try:
        message_timestamp = int(message.get('timestamp'))
        expected_timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
        and message_timestamp == expected_timestamp
        and (message.get('_source') or 'webui') == (source or 'webui')
        and list(message.get('attachments') or []) == list(attachments or [])
    )


def _message_matches_pending_text(message, pending_text):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
    )


def _latest_user_matches_pending_text(messages, pending_text):
    if not isinstance(messages, list) or not pending_text:
        return False
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'user':
            return _message_matches_pending_text(message, pending_text)
    return False
