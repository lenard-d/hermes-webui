"""Compression-aware context anchors for append-only transcript merging."""

from __future__ import annotations

import datetime
import logging

from api.compression_anchor import is_context_compression_marker
from .message_identity import (
    _session_message_content_key,
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
