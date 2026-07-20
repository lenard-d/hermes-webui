"""Append-only reconciliation between sidecars and the Agent state database."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import re
from contextlib import closing
from pathlib import Path

from api.compression_anchor import is_context_compression_marker
from api.config import HOME
from api.agent_sessions import open_state_db_readonly
from .claude_code import (  # noqa: F401 - compatibility re-exports
    CLAUDE_CODE_SOURCE,
    get_claude_code_session_messages,
)
from .state_db import get_state_db_session_messages  # noqa: F401 - compatibility re-export
from .message_identity import (
    _build_visible_duplicate_lookup,
    _has_visible_duplicate,
    _matching_visible_duplicate,
    _merge_session_display_metadata,
    _message_timestamp_as_float,
    _session_message_content_key,
    _session_message_dedup_key,
    _session_message_merge_key,
    _session_message_visible_key,
    _session_messages_have_prefix,
)
from .message_identity import _message_content_text
from .records import _message_role

logger = logging.getLogger(__name__)

def _sidecar_has_terminal_partial_error(sidecar_messages: list) -> bool:
    """Return True when WebUI already owns an interrupted live partial turn.

    After a cancelled/error terminal event, the WebUI sidecar contains the
    user prompt, the streamed partial assistant prose/tool snapshot, and the
    explicit terminal carrier. state.db may still contain the same run's raw
    assistant/tool replay rows; appending those rows makes Compact Worklog show
    duplicated process prose after cancel. In that shape, the sidecar is the
    display owner.
    """
    messages = [msg for msg in (sidecar_messages or []) if isinstance(msg, dict)]
    latest_error_idx = None
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "assistant":
            continue
        if msg.get("_error"):
            latest_error_idx = idx
    if latest_error_idx is None:
        return False
    for msg in messages[latest_error_idx + 1 :]:
        if str(msg.get("role") or "").lower() in ("user", "assistant"):
            return False
    segment_start = 0
    for idx in range(latest_error_idx - 1, -1, -1):
        if str(messages[idx].get("role") or "").lower() == "user":
            segment_start = idx + 1
            break
    for msg in messages[segment_start:latest_error_idx]:
        if str(msg.get("role") or "").lower() == "assistant" and msg.get("_partial"):
            return True
    return False


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


def merge_session_messages_append_only(
    sidecar_messages: list,
    state_messages: list,
    *,
    truncation_watermark=None,
    truncation_boundary=None,
) -> list:
    """Merge sidecar/context and state.db messages without deleting local rows.

    ``truncation_boundary``: the original truncate cutoff — the
    timestamp of the last message kept by the truncate operation.  When the
    watermark is later advanced (new turn committed), this boundary is preserved
    so the empty-sidecar recovery can distinguish a legitimate prefix from a
    deleted suffix instead of guessing by dropping one turn pair.
    """
    sidecar_messages = list(sidecar_messages or [])
    state_messages = list(state_messages or [])
    # Per-invocation cache keyed by message identity. Sidecar/state message objects
    # are retained for this call, and this function does not mutate key-defining
    # fields before each helper call.
    _MESSAGE_CACHE_MISSING = object()
    _cached_msg_prepared: dict[int, dict[str, object]] = {}
    _cached_msg_keys: dict[tuple[int, str], object] = {}

    _message_key_helpers = {
        "merge": _session_message_merge_key,
        "dedup": _session_message_dedup_key,
        "content": _session_message_content_key,
        "visible": _session_message_visible_key,
    }

    def _cached_message_key(msg, kind):
        if not isinstance(msg, dict):
            return _message_key_helpers[kind](msg)

        cache_key = (id(msg), kind)
        value = _cached_msg_keys.get(cache_key, _MESSAGE_CACHE_MISSING)
        if value is not _MESSAGE_CACHE_MISSING:
            return value

        helper = _message_key_helpers[kind]
        msg_cache_key = id(msg)
        prepared_msg = _cached_msg_prepared.get(msg_cache_key)

        if kind in {"merge", "dedup"}:
            if prepared_msg is None:
                value = helper(msg)
                # If this is a legacy message key, keep the already-stringified
                # content payload for downstream helper calls.
                if isinstance(value, tuple) and value and value[0] == "legacy":
                    prepared_msg = dict(msg)
                    prepared_msg["content"] = value[2]
                    _cached_msg_prepared[msg_cache_key] = prepared_msg
            else:
                value = helper(prepared_msg)
            _cached_msg_keys[cache_key] = value
            return value

        if prepared_msg is None:
            # For non-ID messages this is the canonical merge path.
            merge_key = _cached_message_key(msg, "merge")
            prepared_msg = _cached_msg_prepared.get(msg_cache_key)
            if prepared_msg is None:
                prepared_msg = dict(msg)
                prepared_msg["content"] = (
                    merge_key[2]
                    if isinstance(merge_key, tuple)
                    and len(merge_key) > 2
                    and merge_key[0] == "legacy"
                    else str(msg.get("content") or "")
                )
                _cached_msg_prepared[msg_cache_key] = prepared_msg

        value = helper(prepared_msg)
        _cached_msg_keys[cache_key] = value
        return value

    watermark_timestamp = _message_timestamp_as_float({"timestamp": truncation_watermark})
    if not state_messages:
        return sidecar_messages
    if not sidecar_messages:
        if watermark_timestamp is None:
            # No watermark — keep everything, just dedup.
            filtered = state_messages
        elif watermark_timestamp == 0:
            # Truncate-to-empty sentinel (#2914) — block all replay.
            return []
        else:
            # Positive watermark after edit/retry/undo (#4767).  Without a
            # sidecar there's no seen_content_keys to check against, so we
            # reconstruct the correct transcript from state.db alone.
            #
            # `at_or_after` (ts >= watermark) is legitimate POST-EDIT content
            # ONLY when the watermark was ADVANCED strictly past the original
            # truncate cutoff — i.e. a new turn was committed after the edit, so
            # truncation_boundary (the original cutoff) is strictly below the
            # advanced watermark.  In that state we keep the legitimate prefix
            # (ts <= boundary) plus the post-edit tail (ts >= watermark) and drop
            # the deleted (boundary, watermark) suffix.
            #
            # In every OTHER state the content above the watermark is the deleted
            # suffix, NOT post-edit content, so keeping it would resurrect deleted
            # turns (the exact data-loss this fix exists to kill):
            #   * boundary == watermark — just truncated, no new turn committed
            #     yet (e.g. crash/cold-load with metadata-vs-sidecar divergence);
            #   * boundary is None — legacy session saved before this field
            #     existed.  In the pre-#4767 model committing a turn CLEARED the
            #     watermark to None, so a persisted positive watermark always
            #     meant "frozen at cutoff, not advanced".
            # For all of those, fall back to the conservative pre-#4767 filter
            # `ts <= watermark`, which never resurrects a deleted suffix.
            boundary_ts = _message_timestamp_as_float({"timestamp": truncation_boundary})
            if boundary_ts is not None and boundary_ts < watermark_timestamp:
                pre_legitimate = [
                    m for m in state_messages
                    if (ts := _message_timestamp_as_float(m)) is not None
                    and ts <= boundary_ts
                ]
                at_or_after = [
                    m for m in state_messages
                    if (ts := _message_timestamp_as_float(m)) is not None
                    and ts >= watermark_timestamp
                ]
                filtered = pre_legitimate + at_or_after
            else:
                filtered = [
                    m for m in state_messages
                    if (ts := _message_timestamp_as_float(m)) is not None
                    and ts <= watermark_timestamp
                ]

        # Deduplicate true duplicates (same role, content, exact timestamp)
        # without collapsing legitimately-repeated identical turns (#3346).
        seen = set()
        seen_messages = {}
        deduped = []
        for msg in filtered:
            key = _cached_message_key(msg, "dedup")
            if key not in seen:
                seen.add(key)
                seen_messages[key] = msg
                deduped.append(msg)
            else:
                _merge_session_display_metadata(seen_messages.get(key), msg)
        return deduped

    merged_messages = []
    seen_message_keys = set()
    seen_dedup_keys = set()
    seen_content_keys = set()
    seen_visible_keys = set()
    sidecar_visible_sequence = []
    sidecar_visible_messages = []
    sidecar_visible_keys = set()
    sidecar_visible_counts = {}
    merged_by_message_key = {}
    merged_by_dedup_key = {}
    merged_by_visible_key = {}
    max_sidecar_timestamp = None

    def _remember_merged_message(message):
        if not isinstance(message, dict):
            return
        merged_by_message_key.setdefault(_cached_message_key(message, "merge"), message)
        merged_by_dedup_key.setdefault(_cached_message_key(message, "dedup"), message)
        merged_by_visible_key.setdefault(_cached_message_key(message, "visible"), message)

    for msg in sidecar_messages:
        timestamp = _message_timestamp_as_float(msg)
        if timestamp is not None:
            max_sidecar_timestamp = timestamp if max_sidecar_timestamp is None else max(max_sidecar_timestamp, timestamp)
        key = _cached_message_key(msg, "merge")
        seen_message_keys.add(key)
        seen_dedup_keys.add(_cached_message_key(msg, "dedup"))
        content_key = _cached_message_key(msg, "content")
        seen_content_keys.add(content_key)
        visible_key = _cached_message_key(msg, "visible")
        seen_visible_keys.add(visible_key)
        sidecar_visible_keys.add(visible_key)
        sidecar_visible_counts[visible_key] = sidecar_visible_counts.get(visible_key, 0) + 1
        sidecar_visible_sequence.append(visible_key)
        sidecar_visible_messages.append(msg)
        merged_messages.append(msg)
        _remember_merged_message(msg)
    if _sidecar_has_terminal_partial_error(sidecar_messages):
        return merged_messages
    sidecar_visible_lookup = _build_visible_duplicate_lookup(sidecar_visible_keys)
    state_replay_idx = 0
    skipped_state_visible_counts = {}
    # Loop-invariant: a session whose original truncate cutoff (truncation_boundary)
    # is strictly below the watermark is genuinely ADVANCED (a new turn was
    # committed after the edit). In that state post-watermark state.db rows are
    # legitimate post-edit content, even when the sidecar's newest row only
    # EQUALS the watermark (the post-edit user is checkpointed but its assistant
    # reply exists only in state.db). Conservative for boundary None / == watermark.
    boundary_ts = _message_timestamp_as_float({"timestamp": truncation_boundary})
    watermark_advanced_by_boundary = (
        watermark_timestamp is not None
        and boundary_ts is not None
        and boundary_ts < watermark_timestamp
    )
    for msg in state_messages:
        timestamp = _message_timestamp_as_float(msg)
        key = _cached_message_key(msg, "merge")
        dedup_key = _cached_message_key(msg, "dedup")
        visible_key = _cached_message_key(msg, "visible")
        content_key = _cached_message_key(msg, "content")
        replays_sidecar_prefix = False
        replay_target = None
        if state_replay_idx < len(sidecar_visible_sequence):
            expected_visible_key = sidecar_visible_sequence[state_replay_idx]
            if visible_key == expected_visible_key or _has_visible_duplicate(
                visible_key, {expected_visible_key}
            ):
                replays_sidecar_prefix = True
                replay_target = sidecar_visible_messages[state_replay_idx]
                state_replay_idx += 1
        if replays_sidecar_prefix:
            _merge_session_display_metadata(replay_target, msg)
            matched_visible_key = _matching_visible_duplicate(
                visible_key,
                sidecar_visible_keys,
                sidecar_visible_lookup,
            )
            if matched_visible_key is not None:
                skipped_state_visible_counts[matched_visible_key] = (
                    skipped_state_visible_counts.get(matched_visible_key, 0) + 1
                )
            # Record dedup key so later duplicates of this replayed message
            # are caught by the dedup guard (#3346).
            seen_dedup_keys.add(dedup_key)
            continue
        # Skip rows ABOVE the watermark only while the sidecar has NOT advanced
        # past the watermark. Because Session.save() no longer auto-clears the
        # watermark, an unconditional `timestamp > watermark` skip would become
        # permanent and silently drop legitimate future state.db-only recovery
        # rows once the session moves forward past the edit boundary. Once the
        # sidecar's own max timestamp is beyond the watermark (the session has
        # advanced), allow state rows newer than the sidecar tail to merge.
        #
        # The sidecar's max timestamp can also EQUAL the watermark when the new
        # post-edit USER turn has been checkpointed into the sidecar (its
        # timestamp == the advanced watermark) but its ASSISTANT reply exists
        # only in state.db (recovery before the sidecar tail advances). In that
        # state truncation_boundary < watermark proves the session is genuinely
        # advanced, so the post-watermark state-only reply is legitimate
        # post-edit content and must merge through (not be dropped as a replaced
        # tail). The conservative skip still applies for boundary is None and
        # boundary == watermark (not-advanced / legacy).
        #
        # CRITICAL: the boundary-advanced signal may only bypass the skip AFTER
        # state replay has consumed the sidecar's visible checkpoint
        # (state_replay_idx >= len(sidecar_visible_sequence)). A deleted suffix
        # row with ts > watermark that appears in state.db BEFORE the edited
        # checkpoint must still be skipped — otherwise the advanced signal would
        # resurrect it. The sidecar-max-timestamp signal needs no such gate (a
        # sidecar tail beyond the watermark is itself proof the checkpoint has
        # advanced).
        checkpoint_consumed = state_replay_idx >= len(sidecar_visible_sequence)
        sidecar_advanced_past_watermark = (
            watermark_timestamp is not None
            and (
                (max_sidecar_timestamp is not None
                 and max_sidecar_timestamp > watermark_timestamp)
                or (watermark_advanced_by_boundary and checkpoint_consumed)
            )
        )
        if (
            watermark_timestamp is not None
            and timestamp is not None
            and timestamp > watermark_timestamp
            and key not in seen_message_keys
            and (
                not sidecar_advanced_past_watermark
                or (max_sidecar_timestamp is not None and timestamp <= max_sidecar_timestamp)
            )
        ):
            continue
        # When a truncation watermark is active, state.db may contain original
        # messages that were replaced by Edit (old content with old timestamp).
        # The timestamp-based filter above catches messages AFTER the watermark,
        # but messages BEFORE it (like the original pre-edit content) slip through.
        # If a state.db message's content is not present in the sidecar and its
        # timestamp is before the watermark, it's a replaced/stale row — skip it.
        if (
            watermark_timestamp is not None
            and timestamp is not None
            and timestamp < watermark_timestamp
            and key not in seen_message_keys
            and content_key not in seen_content_keys
        ):
            continue
        # Same-second edit: if timestamp equals the watermark and the message
        # content is not in the sidecar, it's a replaced message edited at the
        # same second — skip it.  The edited version (same timestamp, different
        # content) is in the sidecar and survives this check.
        #
        # Only apply the same-second guard to user messages.  An assistant reply
        # (or tool message) at the same second as the watermark is a legitimate
        # post-edit recovery row — the sidecar holds only the edited user
        # checkpoint, so the assistant reply's content won't be in it and would
        # be silently dropped without this role guard.
        if (
            watermark_timestamp is not None
            and timestamp is not None
            and timestamp == watermark_timestamp
            and key not in seen_message_keys
            and content_key not in seen_content_keys
            and str(msg.get("role", "")).lower() == "user"
        ):
            continue
        # Check for true duplicates using full-precision timestamp (#3346).
        # Must run before the merge-key guards so that legitimately distinct
        # sub-second messages with the same second-level merge key are not
        # collapsed.  The merge key truncates to seconds; the dedup key does
        # not.
        if dedup_key in seen_dedup_keys:
            _merge_session_display_metadata(merged_by_dedup_key.get(dedup_key), msg)
            continue
        if max_sidecar_timestamp is not None and timestamp is not None and timestamp <= max_sidecar_timestamp:
            # For message_id keys the merge key is authoritative — skip if
            # already seen.  For legacy keys the dedup check above already
            # handled true duplicates; same-second distinct messages must
            # fall through.
            if key in seen_message_keys and key[0] == "message_id":
                _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                continue
            if not (isinstance(key, tuple) and key[:1] == ("message_id",)):
                # Legacy key within sidecar timestamp range — only skip if
                # this exact merge_key was already registered by the sidecar.
                # Different tool_calls produce different merge_keys even with
                # identical content/timestamp, so an unchecked continue here
                # would drop legitimately distinct turns.  (#3346 / PR #3665)
                if key in seen_message_keys:
                    _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                    continue
        if key in seen_message_keys and key[0] == "message_id":
            _merge_session_display_metadata(merged_by_message_key.get(key), msg)
            continue
        matched_visible_key = _matching_visible_duplicate(
            visible_key,
            sidecar_visible_keys,
            sidecar_visible_lookup,
        )
        if matched_visible_key is not None:
            skipped_count = skipped_state_visible_counts.get(matched_visible_key, 0)
            sidecar_count = sidecar_visible_counts.get(matched_visible_key, 0)
            if skipped_count < sidecar_count:
                skipped_state_visible_counts[matched_visible_key] = skipped_count + 1
                _merge_session_display_metadata(merged_by_visible_key.get(matched_visible_key), msg)
                continue
        # State rows at or before the newest sidecar timestamp are normally
        # assumed to have already been observed by the sidecar. The <= gate
        # preserves sidecar-only ordering/metadata for equal timestamps and
        # prevents duplicate legacy rows when timestamp precision differs
        # between stores. State rows whose visible content already exists in
        # the sidecar are also skipped even if state.db restamped them later
        # during compaction/recovery; otherwise old prompts can be appended
        # after the assistant tail and make /api/session look like the answer
        # vanished. Explicit message ids are authoritative for distinct rows
        # only when their visible content is not already present.
        if (
            key[0] != "message_id"
            and max_sidecar_timestamp is not None
            and timestamp is not None
            and timestamp <= max_sidecar_timestamp
        ):
            # When a truncation watermark is active and the sidecar holds only
            # the edited user checkpoint, state.db may contain an assistant/tool
            # reply at the same timestamp that is NOT in the sidecar.  This
            # block would normally skip it ("sidecar already has this message"),
            # but the sidecar doesn't — it's a genuine state-only recovery row.
            # Let it through (CORE-B, #4767).
            #
            # Only AFTER the sidecar's visible checkpoint has been consumed
            # (checkpoint_consumed) — a same-second row appearing in state.db
            # BEFORE the edited user replay is a deleted/replaced row, not the
            # post-edit reply, and must stay skipped.
            if (
                watermark_timestamp is not None
                and timestamp == watermark_timestamp
                and checkpoint_consumed
                and str(msg.get("role", "")).lower() != "user"
                and content_key not in seen_content_keys
            ):
                pass  # fall through to append below
            else:
                # Legacy key within sidecar timestamp range.  Normally skip — the
                # sidecar already has this message.  Exception: if the state.db
                # message has tool_calls that DIFFER from the sidecar version
                # (same content_key but different dedup_key because tool_calls
                # differ), preserve it — distinct tool_calls must not be collapsed.
                _tc = msg.get("tool_calls")
                if _tc:
                    _ck = content_key
                    if _ck in seen_content_keys and dedup_key not in seen_dedup_keys:
                        # Different tool_calls from sidecar — preserve, but keep
                        # the row in timestamp order. Falling through to the
                        # generic append path would move older tool-call-only
                        # assistant rows after the settled final answer.
                        if _insert_state_message_chronologically(merged_messages, msg):
                            seen_message_keys.add(key)
                            seen_dedup_keys.add(dedup_key)
                            seen_content_keys.add(content_key)
                            seen_visible_keys.add(visible_key)
                            _remember_merged_message(msg)
                        continue
                    else:
                        _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                        continue
                else:
                    if msg.get("role") == "user" and content_key not in seen_content_keys:
                        if _insert_state_message_chronologically(merged_messages, msg):
                            seen_message_keys.add(key)
                            seen_dedup_keys.add(dedup_key)
                            seen_content_keys.add(content_key)
                            seen_visible_keys.add(visible_key)
                            _remember_merged_message(msg)
                        continue
                    _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                    continue
        seen_message_keys.add(key)
        seen_dedup_keys.add(dedup_key)
        seen_content_keys.add(content_key)
        seen_visible_keys.add(visible_key)
        merged_messages.append(msg)
        _remember_merged_message(msg)
    return merged_messages


def reconciled_state_db_messages_for_session(
    session, *, prefer_context: bool = False, state_messages: list | None = None
) -> list:
    """Return append-only messages reconciled with state.db for a WebUI session."""
    if session is None:
        return []
    local_messages = []
    using_context_messages = False
    if prefer_context:
        context_messages = getattr(session, 'context_messages', None)
        if isinstance(context_messages, list) and context_messages:
            local_messages = context_messages
            using_context_messages = True
    if not local_messages:
        local_messages = getattr(session, 'messages', None) or []
    if state_messages is None:
        state_messages = get_state_db_session_messages(getattr(session, 'session_id', None))
    if prefer_context and local_messages:
        if using_context_messages:
            sidecar_messages = getattr(session, 'messages', None) or []
            if (
                getattr(session, 'is_cli_session', False)
                and not getattr(session, 'read_only', False)
                and sidecar_messages
                and len(sidecar_messages) > len(local_messages)
                and _session_messages_have_prefix(sidecar_messages, local_messages)
            ):
                # A claimed CLI sidecar can carry a stale context prefix while the
                # stitched CLI transcript already landed in session.messages. On the
                # first WebUI follow-up, prefer that longer authoritative transcript
                # unless context_messages intentionally diverged via compaction or
                # another non-prefix transform.
                local_messages = sidecar_messages
                using_context_messages = False
            if using_context_messages:
                compressed_context = _context_messages_include_compression_marker(local_messages)
                anchor_key = getattr(session, "compression_anchor_message_key", None)
                if compressed_context:
                    if not anchor_key:
                        logger.debug(
                            "Compressed context for session %s has no compression anchor; using context_messages only",
                            getattr(session, "session_id", None),
                        )
                        return list(local_messages)
                    anchor_index = _state_db_anchor_index(state_messages, anchor_key)
                    if anchor_index is None:
                        logger.debug(
                            "Compressed context for session %s has an unverifiable compression anchor; using context_messages only",
                            getattr(session, "session_id", None),
                        )
                        return list(local_messages)
                    state_messages = list(state_messages or [])[anchor_index + 1 :]
        state_messages = state_db_delta_after_context(local_messages, state_messages)
    return merge_session_messages_append_only(
        local_messages,
        state_messages,
        truncation_watermark=getattr(session, "truncation_watermark", None),
        truncation_boundary=getattr(session, "truncation_boundary", None),
    )


def get_cli_session_messages(sid, *, profile=None) -> list:
    """Read messages for a single CLI/external-agent session.

    Preserve tool-call/result and reasoning metadata from the agent state.db so
    CLI-origin transcripts render with the same tool cards as WebUI-native
    sessions. When the requested session is the tip of a compression/CLI-close
    continuation chain, return the stitched full transcript across all segments
    in chronological order. Returns empty list on any error.
    """
    if str(sid or '').startswith(f'{CLAUDE_CODE_SOURCE}_'):
        return get_claude_code_session_messages(sid)
    return get_state_db_session_messages(sid, stitch_continuations=True, profile=profile)


def count_conversation_rounds(sid: str, since: float | None = None) -> int:
    """Count conversation rounds for a session from state.db.

    A "round" = one user message + one agent reply.  Consecutive user
    messages are merged into a single round so that multi-part questions
    don't inflate the count.

    Parameters
    ----------
    sid : str
        Gateway session ID (e.g. ``20260430_151231_7209a0``).
    since : float | None
        Unix timestamp.  If provided, only messages **after** this
        timestamp are counted.

    Returns
    -------
    int
        Number of complete conversation rounds.
    """
    import os, sqlite3, datetime

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return 0

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT role, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
                (sid,),
            )
            rows = cur.fetchall()
    except Exception:
        return 0

    rounds = 0
    seen_user = False          # have we seen a user msg in the current round?
    seen_agent_after_user = False  # have we seen an agent reply after that user msg?

    for row in rows:
        role = (row['role'] or '').strip().lower()
        ts_raw = row['timestamp']

        # Parse timestamp and apply the ``since`` filter.
        if since is not None and ts_raw is not None:
            try:
                if isinstance(ts_raw, (int, float)):
                    ts_val = float(ts_raw)
                else:
                    # ISO-8601 string
                    ts_val = datetime.datetime.fromisoformat(
                        str(ts_raw).replace('Z', '+00:00')
                    ).timestamp()
                if ts_val <= since:
                    continue
            except Exception:
                pass

        if role == 'user':
            if seen_user and not seen_agent_after_user:
                # Consecutive user message — merge into current round.
                pass
            elif seen_user and seen_agent_after_user:
                # Previous round completed, starting a new one.
                rounds += 1
                seen_agent_after_user = False
            seen_user = True
        elif role == 'assistant':
            if seen_user:
                seen_agent_after_user = True

    # Close the last round if it was completed.
    if seen_user and seen_agent_after_user:
        rounds += 1

    return rounds
