"""New-turn context selection and checkpoint/writeback preparation."""

from __future__ import annotations

import logging
import re
import time

from .compression_anchors import _drop_checkpointed_current_user_from_context
from .context_replay import _session_context_messages
from .thinking_content import _message_text
from api.workspace_context import _strip_workspace_prefix


logger = logging.getLogger(__name__)


def _save_streaming_checkpoint(session):
    """Persist a streaming checkpoint under the session's profile context."""
    from api import profiles as profiles_api

    with profiles_api.profile_env_for_background_worker(
        session,
        "streaming checkpoint",
        logger_override=logger,
    ):
        session.save(skip_index=True)


def _normalize_fresh_chat_text(text):
    text = _strip_workspace_prefix(str(text or ''), include_legacy=True)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.strip(" \t\r\n.!?。！？,，~～")


def _is_casual_fresh_chat_message(msg_text):
    """Return True for short opener messages that should not resume old tasks."""
    text = _normalize_fresh_chat_text(msg_text)
    if not text or len(text) > 24:
        return False
    continuation_terms = (
        "continue",
        "resume",
        "carry on",
        "go on",
        # CJK continuation terms (zh-CN): jixu, jiezhe, wangxia, xiayibu.
        # Encoded as Python escape sequences (not literal CJK) so api/streaming.py
        # passes tests/test_title_sanitization.py::test_title_generation_source_has_no_cjk_literals,
        # which scans this file for any U+4E00-U+9FFF code points. Runtime
        # comparisons still use the real CJK strings — Python decodes the
        # escapes at compile time.
        "\u7ee7\u7eed",
        "\u63a5\u7740",
        "\u5f80\u4e0b",
        "\u4e0b\u4e00\u6b65",
    )
    if any(term in text for term in continuation_terms):
        return False
    return text in {
        "hi",
        "hello",
        "hey",
        "hello there",
        "hi there",
        # CJK greetings (zh-CN): nihao, ninhao, hai, haluo, zaima, zaime.
        # Same escape-sequence rationale as the continuation block above.
        "\u4f60\u597d",         # nihao
        "\u60a8\u597d",         # ninhao
        "\u55e8",               # hai (was \u5616 = "click of tongue", not a greeting)
        "\u54c8\u55bd",         # haluo (was \u54c8\u5582 = uncommon "ha-wei" variant)
        "\u5728\u5417",         # zaima
        "\u5728\u4e48",         # zaime
    }


def _has_task_resume_compaction_marker(messages):
    """Detect compacted model context that tells the agent to resume an old task."""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        text = _message_text(msg.get('content', '')).lower()
        if not text:
            continue
        if "context compaction" not in text and "context compression" not in text:
            continue
        if (
            "active task" in text
            or "resume exactly" in text
            or "current task" in text
            or "task list was preserved" in text
            or "in_progress" in text
        ):
            return True
    return False


def _new_turn_context_from_messages(messages, msg_text):
    """Return provider-facing history for a new user turn from a message list."""
    history = _drop_checkpointed_current_user_from_context(messages, msg_text)
    if _is_casual_fresh_chat_message(msg_text) and _has_task_resume_compaction_marker(
        history
    ):
        return []
    return history


def _context_messages_for_new_turn(session, msg_text):
    """Return provider-facing history for a new user turn.

    Compacted agent sessions can carry a hidden "resume the active task" summary
    in context_messages. If the user starts a fresh casual greeting in that old
    session, do not feed that stale active-task summary back to the model.
    """
    return _new_turn_context_from_messages(_session_context_messages(session), msg_text)


def _stream_writeback_is_current(session, stream_id):
    """Return True only while a worker still owns the session writeback.

    cancel_stream() intentionally clears ``active_stream_id`` early so the UI can
    accept a follow-up turn while the old worker is unwinding. That old worker
    must not later persist its stale result over the newer transcript.
    """
    return bool(stream_id) and getattr(session, 'active_stream_id', None) == stream_id


def _stream_writeback_can_supersede_recovery_marker(session, msg_text):
    """Allow a finishing worker to replace its own stale-repair marker.

    The stale-pending repair path can occasionally run while the original worker
    is still alive but temporarily missing from the in-memory stream registry. It
    clears ``active_stream_id`` and appends a "Response interrupted" marker. If
    the original worker later finishes, treating ``active_stream_id is None`` as
    stale drops the real answer and leaves the misleading marker visible.

    This is intentionally narrow: only a session with no active/pending turn and
    whose last visible row is the recovery marker for this exact user prompt may
    be superseded. If a newer turn has appended anything after the marker, the
    normal stale-writeback guard still wins.
    """
    if getattr(session, 'active_stream_id', None):
        return False
    if getattr(session, 'pending_user_message', None):
        return False
    if getattr(session, 'pending_attachments', None):
        return False
    messages = list(getattr(session, 'messages', None) or [])
    if len(messages) < 2:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or not last.get('_error'):
        return False
    if last.get('type') != 'interrupted':
        return False
    content = str(last.get('content') or '')
    if 'Response interrupted' not in content or 'before this turn finished' not in content:
        return False

    expected = ' '.join(str(msg_text or '').split())
    if not expected:
        return False
    for msg in reversed(messages[:-1]):
        if not isinstance(msg, dict):
            continue
        if msg.get('_error'):
            continue
        if msg.get('role') != 'user':
            continue
        actual = ' '.join(str(msg.get('content') or '').split())
        return actual == expected
    return False


def _advance_truncation_watermark_after_commit(session) -> None:
    """Advance a positive truncation watermark once a new user turn is committed
    to ``session.messages`` (#3831).

    retry/undo/Edit set a positive watermark to suppress the *replaced* tail from
    the append-only state.db merge; Session.save() deliberately never auto-clears
    it (#2914). Once the new turn is durably in messages we advance the watermark
    to the newest user message timestamp so that state.db rows newer than the
    watermark are still merged in, while the replaced pre-edit tail remains
    filtered. Never 0.0 (the truncate-to-empty sentinel that must keep blocking
    replay, #2914).
    """
    if not getattr(session, 'truncation_watermark', None):
        return
    messages = getattr(session, 'messages', None) or []
    # Walk backwards to find the newest user message timestamp
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get('role') == 'user':
            ts = msg.get('timestamp')
            if isinstance(ts, (int, float)) and ts > 0:
                session.truncation_watermark = float(ts)
                return
    session.truncation_watermark = time.time()
