"""State-database delta and chronological insertion.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def state_db_delta_after_context(sidecar_context: list, state_messages: list) -> list:
    """Return only state.db rows that are newer than model-facing context.

    `context_messages` is the authoritative model-facing prefix. state.db may
    contain a mirrored copy of that prefix with fresh timestamps, especially for
    LCM/continuation sessions. Appending the whole state transcript to a clean
    sidecar context replays old context into the next runtime prompt.
    """
    sidecar_context = list(sidecar_context or [])
    state_messages = list(state_messages or [])
    if not sidecar_context or not state_messages:
        return state_messages

    # Recovered interrupted turns are special: the visible interruption marker
    # is synthetic, so the recovered user turn should still count as a mirrored
    # prefix when it is the actual aligned prefix row.
    allow_single_row_prefix = bool(
        isinstance(sidecar_context[0], dict)
        and sidecar_context[0].get('_recovered')
        and str(sidecar_context[0].get('role') or '') == 'user'
    )

    sidecar_keys = [_session_message_content_key(m) for m in sidecar_context]
    state_keys = [_session_message_content_key(m) for m in state_messages]
    max_offset = min(len(sidecar_keys), len(state_keys))
    best_len = 0
    best_offset = 0
    for offset in range(max_offset):
        length = 0
        while (
            offset + length < len(sidecar_keys)
            and length < len(state_keys)
            and sidecar_keys[offset + length] == state_keys[length]
        ):
            length += 1
        if length > best_len:
            best_len = length
            best_offset = offset

    # Require at least two mirrored rows. A single repeated short user message
    # is not enough evidence that state.db starts with a mirrored context
    # segment, but small recovered contexts often contain only a compact summary
    # and one follow-up row; those should still use the delta path.
    if best_len < (1 if allow_single_row_prefix and best_offset == 0 else 2):
        return state_messages

    # Drop only rows that can be aligned with the remaining sidecar context in
    # order. This still tolerates stale state-only rows between mirrored context
    # rows, but once the sidecar context is exhausted every later state row is a
    # real delta, even if it repeats a short earlier message.
    sidecar_index = best_len
    state_index = best_len
    while sidecar_index < len(sidecar_keys) and state_index < len(state_keys):
        if state_keys[state_index] == sidecar_keys[sidecar_index]:
            sidecar_index += 1
        state_index += 1
    if sidecar_index == len(sidecar_keys):
        return state_messages[state_index:]
    return state_messages[best_len:]


def _normalized_compression_anchor_text(value) -> str:
    return " ".join(str(value or "").split()).strip()[:160]


def _compression_anchor_timestamp_as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _context_messages_include_compression_marker(messages: list) -> bool:
    for message in messages or []:
        if not is_context_compression_marker(message):
            continue
        text = _message_content_text(message).lower().lstrip()
        # Only prompt compaction summaries require fail-closed state.db replay.
        # Other compression-adjacent summaries, such as Session Arc Summary,
        # keep the existing prefix-delta behavior so fresh follow-ups survive.
        if text.startswith("[context compaction") or text.startswith("context compaction"):
            return True
    return False


def _state_db_anchor_index(state_messages: list, anchor_key) -> int | None:
    if not isinstance(anchor_key, dict):
        return None

    anchor_role = str(anchor_key.get("role") or "").strip().lower()
    anchor_text = _normalized_compression_anchor_text(anchor_key.get("text"))
    anchor_attachments = anchor_key.get("attachments")
    anchor_ts = _compression_anchor_timestamp_as_float(anchor_key.get("ts"))

    if not anchor_role:
        return None

    # Do not attempt text-only fallback when timestamp is unavailable. Text-based
    # fallback can match stale legacy rows if the anchor timestamp was lost,
    # which can re-introduce old state.db rows after compaction.
    if anchor_ts is None:
        return None

    if anchor_attachments in (None, ""):
        expected_attachments = 0
    else:
        try:
            expected_attachments = int(anchor_attachments)
        except (TypeError, ValueError):
            expected_attachments = 0

    exact_timestamp_matches = []
    for idx, message in enumerate(state_messages or []):
        if _message_role(message) != anchor_role:
            continue

        attachments = message.get("attachments") if isinstance(message, dict) else None
        attach_count = len(attachments) if isinstance(attachments, list) else 0
        if attach_count != expected_attachments:
            continue

        # Attachment-only or timestamp-only anchors have no stable text payload.
        # In that shape the timestamp + role + attachment count is the boundary;
        # apply text comparison only when the anchor actually captured text.
        message_text = _normalized_compression_anchor_text(_message_content_text(message))
        if anchor_text and message_text != anchor_text:
            continue

        message_ts = _compression_anchor_timestamp_as_float(
            message.get("timestamp") if isinstance(message, dict) else None
        )
        if message_ts is None:
            continue
        if abs(message_ts - anchor_ts) <= 1e-6:
            exact_timestamp_matches.append(idx)
            continue

    if exact_timestamp_matches:
        return exact_timestamp_matches[-1]
    return None


def _tool_call_assistant_should_precede_content_assistant(existing: dict, msg: dict) -> bool:
    return (
        isinstance(existing, dict)
        and isinstance(msg, dict)
        and str(msg.get("role") or "").lower() == "assistant"
        and bool(msg.get("tool_calls"))
        and not _message_content_text(msg).strip()
        and str(existing.get("role") or "").lower() == "assistant"
        and not existing.get("tool_calls")
        and bool(_message_content_text(existing).strip())
    )


def _insert_state_message_chronologically(messages: list, msg: dict) -> bool:
    """Insert a state.db-only row before newer sidecar rows when safe.

    Returns False when the only chronological slot would resurrect an old state
    row before the sidecar/context begins. This keeps no-watermark compression
    display paths from reintroducing rows that were already compacted out.
    """
    timestamp = _message_timestamp_as_float(msg)
    if timestamp is None:
        messages.append(msg)
        return True
    idx = 0
    while idx < len(messages):
        existing = messages[idx]
        existing_timestamp = _message_timestamp_as_float(existing)
        should_insert = existing_timestamp is not None and (
            existing_timestamp > timestamp
            or (
                existing_timestamp == timestamp
                and (
                    (
                        msg.get("role") == "user"
                        and existing.get("role") == "assistant"
                    )
                    or _tool_call_assistant_should_precede_content_assistant(existing, msg)
                )
            )
        )
        if not should_insert:
            idx += 1
            continue
        if idx == 0 and existing_timestamp is not None and existing_timestamp > timestamp:
            # With no surviving sidecar/context row before this slot, a real
            # interruption rescue is indistinguishable from a compacted-out old
            # prompt; prefer avoiding no-watermark resurrection in that shape.
            return False
        # Advance the insertion point past two kinds of slots that must not be
        # split, applied to a fixpoint so they compose in any order at an
        # equal-timestamp collision:
        #   (a) an assistant(tool_calls) -> tool result block — inserting inside
        #       it would split the tool call from its result (provider 400 /
        #       corrupt tool context);
        #   (b) a slot whose left neighbour shares this message's role at the
        #       same timestamp — inserting there would re-order an already-matched
        #       same-role turn (e.g. user, <inserted user>, assistant). The agent
        #       core merges adjacent users before send, so (b) is benign in
        #       practice, but advancing keeps the merged transcript correctly
        #       ordered and alternation-clean regardless.
        # Looping to a fixpoint guarantees the same-role guard can't strand the
        # insert back inside a tool-pair (and vice versa).
        while True:
            advanced = False
            # (a) Skip past a complete assistant(tool_calls) -> tool result
            # block. Advance over ALL contiguous tool rows that belong to the
            # preceding assistant's tool_calls, not just the first — a multi-tool
            # turn has several adjacent tool results, and inserting between any of
            # them splits the block (assistant, tool, <insert>, tool).
            if (
                idx < len(messages)
                and messages[idx].get("role") == "tool"
                and idx > 0
                and messages[idx - 1].get("role") == "assistant"
                and messages[idx - 1].get("tool_calls")
            ):
                while idx < len(messages) and messages[idx].get("role") == "tool":
                    idx += 1
                    advanced = True
            # (b) Skip past an equal-timestamp run whose left neighbour shares
            # this message's role — inserting there would re-order an
            # already-matched same-role turn (user, <inserted user>, assistant).
            # The agent core merges adjacent users before send, so this is benign
            # in practice, but advancing keeps the merged transcript ordered.
            while (
                idx < len(messages)
                and idx > 0
                and messages[idx - 1].get("role") == msg.get("role")
                and _message_timestamp_as_float(messages[idx]) == timestamp
                and not _tool_call_assistant_should_precede_content_assistant(messages[idx], msg)
            ):
                idx += 1
                advanced = True
            if not advanced:
                break
        messages.insert(idx, msg)
        return True
    messages.append(msg)
    return True

__all__ = ['state_db_delta_after_context', '_normalized_compression_anchor_text', '_compression_anchor_timestamp_as_float', '_context_messages_include_compression_marker', '_state_db_anchor_index', '_tool_call_assistant_should_precede_content_assistant', '_insert_state_message_chronologically']
