"""Compression marker, summary-anchor, and checkpointed-turn helpers."""

from __future__ import annotations

import re

from api.compression_anchor import is_context_compression_marker

from .context_replay import _message_identity
from .thinking_content import _message_text
from api.workspace_context import _looks_like_current_user_turn, _strip_workspace_prefix


def _is_context_compression_marker(msg):
    return is_context_compression_marker(msg)


def _compact_summary_text(raw_text: str | None) -> str | None:
    """Normalize a text blob used in compression summary cards."""
    if not isinstance(raw_text, str):
        return None
    txt = raw_text.strip()
    if not txt:
        return None
    return re.sub(r"\s+", " ", txt).strip()


def _compression_anchor_message_key(message):
    if not isinstance(message, dict):
        return None
    role = str(message.get('role') or '')
    if not role or role == 'tool':
        return None
    content = message.get('content', '')
    text = _message_text(content)
    if len(text) > 160:
        text = text[:160]
    ts = message.get('_ts') or message.get('timestamp')
    attachments = message.get('attachments')
    attach_count = len(attachments) if isinstance(attachments, list) else 0
    if not text and not attach_count and not ts:
        return None
    return {'role': role, 'ts': ts, 'text': text, 'attachments': attach_count}


def _compression_summary_from_messages(messages):
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if not _is_context_compression_marker(m):
            continue
        text = _message_text(m.get('content'))
        if text:
            return text
    return None


def _find_current_user_turn(messages, msg_text):
    needle = " ".join(str(msg_text or '').split())
    last_strong_match = None  # _looks_like_current_user_turn (high confidence)
    last_weak_match = None    # needle substring match (lower confidence)
    fallback = None
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        fallback = idx
        if _looks_like_current_user_turn(msg, msg_text):
            last_strong_match = idx
            continue
        text = " ".join(
            _strip_workspace_prefix(
                _message_text(msg.get('content', '')),
                include_legacy=True,
            ).split()
        )
        if needle and (needle in text or text in needle):
            last_weak_match = idx
    # Return the LAST matching user turn. After context compression the agent's
    # result_messages contain the full conversation history; if the user asked a
    # similar question in an earlier turn, first-match would return that old
    # index, causing the merge to replay the entire history from that point.
    # Last-match anchors on the current turn instead.
    #
    # Prefer the last STRONG match (an exact `_looks_like_current_user_turn`
    # hit) over the last WEAK substring match. The agent loop appends synthetic
    # `role:"user"` continuation prompts (e.g. "Continue", empty-recovery nudges
    # — see conversation_loop.py) AFTER the real user turn; those can weak-match
    # `msg_text` and, if weak matches were allowed to win, would anchor the merge
    # PAST the real turn and drop the assistant/tool output in between. The real
    # current turn is the last strong match, so it must take priority.
    if last_strong_match is not None:
        return last_strong_match
    if last_weak_match is not None:
        return last_weak_match
    return fallback


def _drop_checkpointed_current_user_from_context(messages, msg_text):
    """Return model history without an eager-checkpointed current user turn."""
    history = list(messages or [])
    if not history:
        return history
    current_user_key = _message_identity({'role': 'user', 'content': msg_text})
    if current_user_key and _message_identity(history[-1]) == current_user_key:
        return history[:-1]
    return history
