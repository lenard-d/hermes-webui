"""Model-context selection, message identity, and replay de-duplication."""

from __future__ import annotations

import copy
import json
from types import ModuleType


def session_context_messages(api: ModuleType, session):
    """Return model-facing history without assuming it matches the UI transcript."""
    context_messages = getattr(session, 'context_messages', None)
    if isinstance(context_messages, list) and context_messages:
        return context_messages
    return session.messages or []


def message_identity(api: ModuleType, msg):
    if not isinstance(msg, dict):
        return None
    role = str(msg.get('role') or '')
    content = msg.get('content', '')
    text = api._message_text(content)
    if role == 'user':
        # WebUI sends the model a workspace-prefixed user_message while the
        # visible optimistic bubble contains only the human text. Treat them as
        # the same turn for merge/dedup purposes; otherwise compaction results
        # render two adjacent user bubbles ("Ok" and "[Workspace...]\nOk").
        text = api._strip_workspace_prefix(text, include_legacy=True)
    if not text and not msg.get('tool_call_id') and not msg.get('tool_calls'):
        # Empty assistant messages (e.g. _partial markers with no visible
        # content) previously returned None, making them invisible to the
        # merge dedup in _merge_display_messages_after_agent_result. This
        # caused exponential accumulation: each turn's merge copied ALL
        # prior _partial messages because they had no identity to track.
        # Now, _partial messages with empty text get a stable identity
        # keyed on their role + _partial flag + reasoning/tool metadata,
        # so the merge can dedup identical empty partials.
        if msg.get('_partial'):
            reasoning_key = " ".join(str(msg.get('reasoning') or '').split())[:200]
            return (
                role,
                '',  # empty text
                '',  # no tool_call_id
                '__partial__' + reasoning_key,
            )
        return None
    return (
        role,
        " ".join(str(text or '').split())[:500],
        str(msg.get('tool_call_id') or ''),
        json.dumps(msg.get('tool_calls') or [], sort_keys=True, ensure_ascii=False),
    )


def messages_have_prefix(api: ModuleType, messages, prefix):
    if len(messages or []) < len(prefix or []):
        return False
    for idx, expected in enumerate(prefix or []):
        if api._message_identity((messages or [])[idx]) != api._message_identity(expected):
            return False
    return True


def message_replay_key(api: ModuleType, msg):
    """Return a stable comparison key for replay/overlap de-duplication."""
    identity = api._message_identity(msg)
    if identity is not None:
        return identity
    if not isinstance(msg, dict):
        return None
    return (
        str(msg.get('role') or ''),
        api._message_text(msg.get('content', '')),
        str(msg.get('tool_call_id') or ''),
        json.dumps(msg.get('tool_calls') or [], sort_keys=True, ensure_ascii=False),
    )


def strip_replayed_prefix(api: ModuleType, existing_messages, candidates):
    """Drop a candidate prefix that is already the suffix of existing_messages.

    Compression/continuation can replay the active tail from state.db after the
    previous WebUI context/display already contains it. Prefix-only merge logic
    then treats that replayed tail as a fresh delta and duplicates a whole turn.
    Strip the largest exact suffix/prefix overlap before appending.
    """
    existing_messages = list(existing_messages or [])
    candidates = list(candidates or [])
    max_overlap = min(len(existing_messages), len(candidates))
    for overlap in range(max_overlap, 0, -1):
        left = [api._message_replay_key(m) for m in existing_messages[-overlap:]]
        right = [api._message_replay_key(m) for m in candidates[:overlap]]
        if left == right:
            return candidates[overlap:]
    return candidates


def looks_like_replayed_session_arc_summary(api: ModuleType, previous_msg, candidate_msg):
    """Return True for repeated LCM/session summaries with refreshed hints.

    LCM summary cards can be re-injected with the same long recovered context
    and a different tail such as an expand hint. Exact identity misses those,
    but appending both copies bloats every later model prompt.
    """
    if not isinstance(previous_msg, dict) or not isinstance(candidate_msg, dict):
        return False
    if previous_msg.get('role') != candidate_msg.get('role'):
        return False
    previous_text = " ".join(api._message_text(previous_msg.get('content', '')).split())
    candidate_text = " ".join(api._message_text(candidate_msg.get('content', '')).split())
    if len(previous_text) < 2000 or len(candidate_text) < 2000:
        return False
    marker = '[Session Arc Summary'
    if not previous_text.startswith(marker) or not candidate_text.startswith(marker):
        return False
    return previous_text[:1500] == candidate_text[:1500]


def strip_replayed_context_items(api: ModuleType, existing_messages, candidates):
    """Drop replayed non-adjacent context blocks before persisting context."""
    existing_messages = list(existing_messages or [])
    candidates = list(candidates or [])
    if not existing_messages or not candidates:
        return candidates

    existing_keys = [api._message_replay_key(m) for m in existing_messages]
    candidate_keys = [api._message_replay_key(m) for m in candidates]
    existing_large = [m for m in existing_messages if isinstance(m, dict)]
    cleaned = []
    idx = 0
    min_block = 3
    while idx < len(candidates):
        msg = candidates[idx]
        if any(
            api._looks_like_replayed_session_arc_summary(prev, msg)
            for prev in existing_large
        ):
            idx += 1
            continue

        best = 0
        for start in range(len(existing_keys)):
            length = 0
            while (
                idx + length < len(candidate_keys)
                and start + length < len(existing_keys)
                and candidate_keys[idx + length] == existing_keys[start + length]
            ):
                length += 1
            if length > best:
                best = length
        if best >= min_block:
            idx += best
            continue

        cleaned.append(msg)
        idx += 1
    return cleaned


def dedupe_replayed_context_messages(
    api: ModuleType,
    previous_context,
    result_messages,
    msg_text=None,
):
    """Keep model context append-only without replayed blocks/summaries."""
    previous_context = list(previous_context or [])
    result_messages = list(result_messages or [])
    if not previous_context or not result_messages:
        return result_messages
    previous_user_tail = api._stale_user_tail_candidate(api._last_user_row(previous_context))
    if not api._messages_have_prefix(result_messages, previous_context):
        # Agent-side role-sequence repair can replace the last prior user row
        # with a repaired current-user row. In that shape the result no longer
        # has `previous_context` as an exact prefix, but it should still be
        # merged as: previous context + clean current turn + assistant/tool delta.
        if (
            msg_text
            and len(previous_context) >= 1
            and len(result_messages) >= len(previous_context)
            and api._messages_have_prefix(result_messages, previous_context[:-1])
        ):
            boundary_idx = len(previous_context) - 1
            boundary_row = result_messages[boundary_idx]
            is_stale_merge = bool(
                previous_user_tail
                and api._detect_stale_user_merge(
                    boundary_row,
                    msg_text,
                    previous_user_tail,
                    previous_context=previous_context,
                )
            )
            if is_stale_merge or api._looks_like_current_user_turn(boundary_row, msg_text):
                if is_stale_merge:
                    # Clean only the stale-merged boundary row; leave all prior
                    # history in previous_context untouched.
                    cleaned_boundary = copy.deepcopy(boundary_row)
                    cleaned_boundary['content'] = msg_text
                    candidates = [cleaned_boundary] + result_messages[boundary_idx + 1:]
                else:
                    candidates = result_messages[boundary_idx:]
                candidates = api._strip_replayed_prefix(previous_context, candidates)
                if candidates:
                    candidates = api._strip_replayed_context_items(previous_context, candidates)
                return previous_context + candidates
        return result_messages
    candidates = result_messages[len(previous_context):]
    # Strip stale merges only from the new-turn candidate slice so that
    # legitimate historical user rows in the already-committed previous_context
    # prefix are never rewritten.
    if msg_text and previous_user_tail:
        candidates = api._strip_stale_user_merge_from_messages(
            candidates,
            msg_text,
            previous_user_tail,
            previous_context=previous_context,
        )
    candidates = api._strip_replayed_prefix(previous_context, candidates)
    if candidates:
        candidates = api._strip_replayed_context_items(previous_context, candidates)
    return previous_context + candidates


def dedupe_replayed_active_context(
    api: ModuleType,
    previous_context,
    result_messages,
    msg_text=None,
):
    """Keep model context append-only without re-appending a replayed tail."""
    return api._dedupe_replayed_context_messages(previous_context, result_messages, msg_text)
