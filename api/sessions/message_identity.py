"""Pure message identity and duplicate-detection rules."""

from __future__ import annotations

import datetime
import copy
import json
import re


def _message_content_text(message) -> str:
    """Return the user-visible text represented by one message payload."""
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get('text'), str):
                parts.append(item['text'])
        return ''.join(parts)
    return ''

def _normalized_message_timestamp_for_key(value):
    if value is None or value == "":
        return ""
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Truncate to second-level granularity so that sub-second drift between
    # the sidecar JSON write and the state.db created_at write does not cause
    # the legacy dedup key to differ for the same logical message.
    return str(int(timestamp))


def _message_timestamp_as_float(msg):
    if not isinstance(msg, dict):
        return None
    value = msg.get("timestamp")
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _session_message_merge_key(msg: dict):
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    message_identity = msg.get("id") or msg.get("message_id")
    if message_identity:
        return ("message_id", str(message_identity))
    # Include tool_calls so assistant messages that invoke different tools
    # (but share identical empty content and same-second timestamp) are not
    # collapsed by the merge-key guard at line ~4216.  Without this,
    # all tool-calling messages map to the same legacy key and the
    # timestamp<=max_sidecar_timestamp blanket-skip at line ~4218 drops
    # every state.db tool-call after the first one registered by the sidecar.
    _tc = msg.get("tool_calls")
    _tc_key = json.dumps(_tc, sort_keys=True, default=str) if _tc else ""
    return (
        "legacy",
        str(msg.get("role") or ""),
        str(msg.get("content") or ""),
        _normalized_message_timestamp_for_key(msg.get("timestamp")),
        str(msg.get("tool_call_id") or ""),
        str(msg.get("tool_name") or msg.get("name") or ""),
        _tc_key,
    )


def _session_messages_have_prefix(messages, prefix) -> bool:
    messages = list(messages or [])
    prefix = list(prefix or [])
    if len(prefix) > len(messages):
        return False
    for idx, expected in enumerate(prefix):
        if _session_message_merge_key(messages[idx]) != _session_message_merge_key(expected):
            return False
    return True


_SESSION_MESSAGE_DISPLAY_METADATA_KEYS = (
    "_turnDuration",
    "_turnTps",
    "_turnUsage",
    "_firstTokenMs",
    "_gatewayRouting",
    "_statusCard",
    "_anchor_stream_id",
    "_anchor_activity_scene",
)


def _message_display_metadata_value_present(value) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (dict, list, tuple, set)) and not value:
        return False
    return True


def _merge_session_display_metadata(target: dict | None, source: dict | None) -> None:
    """Preserve display-only turn metadata when duplicate transcript rows merge."""
    if not isinstance(target, dict) or not isinstance(source, dict):
        return
    for key in _SESSION_MESSAGE_DISPLAY_METADATA_KEYS:
        if _message_display_metadata_value_present(target.get(key)):
            continue
        value = source.get(key)
        if _message_display_metadata_value_present(value):
            target[key] = copy.deepcopy(value)


def _session_message_dedup_key(msg: dict):
    """Like _session_message_merge_key but preserves full-precision timestamp.

    Two messages are true duplicates only if role, content, AND exact
    timestamp all match.  Sub-second timestamp differences indicate
    legitimately distinct messages (e.g. two assistant turns within the
    same wall-clock second).
    """
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    message_identity = msg.get("id") or msg.get("message_id")
    if message_identity:
        return ("message_id", str(message_identity))
    # Include tool_calls in the key so assistant messages that carry
    # different tool invocations (but identical empty content/timestamp)
    # are never collapsed into one.  (#3346 regression)
    _tc = msg.get("tool_calls")
    _tc_key = json.dumps(_tc, sort_keys=True, default=str) if _tc else ""
    return (
        "legacy",
        str(msg.get("role") or ""),
        str(msg.get("content") or ""),
        str(msg.get("timestamp") or ""),
        str(msg.get("tool_call_id") or ""),
        str(msg.get("tool_name") or msg.get("name") or ""),
        _tc_key,
    )


def _normalized_session_message_content(msg: dict) -> str:
    if not isinstance(msg, dict):
        return repr(msg)
    return " ".join(str(msg.get("content") or "").split())


def _loose_session_message_content(value: str) -> str:
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def _session_message_content_key(msg: dict):
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    role = str(msg.get("role") or "")
    content = _normalized_session_message_content(msg)
    if role == "user":
        # WebUI sends the model a workspace-prefixed user_message
        # ("[Workspace::v1: /path]\n<text>") while the visible/optimistic
        # bubble and the WebUI sidecar row carry only the bare "<text>". The
        # streaming dedup identity (_message_identity in api/streaming.py)
        # strips this prefix for user turns, so this reconciliation key must
        # do the same. Otherwise a state.db row (prefixed) and a sidecar row
        # (bare) key DIFFERENTLY, the alignment loop in
        # state_db_delta_after_context fails to match them, treats the
        # state.db copy as a NEW row, and appends a duplicate user turn. The
        # agent then merges the two adjacent user rows into a permanent
        # composite -- the post-restart stale-user-prepend bug (#5339). Reuse
        # the SAME helper as the streaming side (imported lazily to avoid a
        # circular import; api.streaming imports the session store at module load) so
        # the two dedup layers can't drift apart again.
        from api.streaming import _strip_workspace_prefix

        content = " ".join(
            _strip_workspace_prefix(content, include_legacy=True).split()
        )
    return (
        role,
        content,
        str(msg.get("tool_call_id") or ""),
        str(msg.get("tool_name") or msg.get("name") or ""),
    )


def _session_message_visible_key(msg: dict):
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    # Include tool_calls so assistant messages that invoke different tools
    # (but share identical empty content) are not collapsed by sidecar
    # prefix matching.  Without this, all tool-calling messages map to
    # ("assistant", "") and the merge treats state.db rows as replays.
    _tc = msg.get("tool_calls")
    _tc_key = json.dumps(_tc, sort_keys=True, default=str) if _tc else ""
    return (
        str(msg.get("role") or ""),
        _normalized_session_message_content(msg),
        _tc_key,
    )


def _build_visible_duplicate_lookup(visible_keys: set[tuple]) -> dict:
    by_role = {}
    for key in visible_keys:
        try:
            role = key[0]
            content = key[1]
        except (TypeError, IndexError):
            continue
        if not content:
            continue
        by_role.setdefault(role, []).append(key)
    # Keep loose_by_key lazy.  Some transcripts contain multi-megabyte tool
    # outputs; eagerly casefolding + regex-tokenizing every visible key on every
    # duplicate probe made /api/session take 10s+ and blocked /api/sessions.
    return {"keys": visible_keys, "by_role": by_role, "loose_by_key": {}}


def _matching_visible_duplicate(visible_key: tuple, visible_keys: set[tuple], lookup: dict | None = None):
    if visible_key in visible_keys:
        return visible_key
    role = visible_key[0]
    content = visible_key[1] if len(visible_key) > 1 else ""
    if not content:
        return None
    if lookup is None:
        lookup = _build_visible_duplicate_lookup(visible_keys)
    loose_content = None
    loose_by_key = lookup.setdefault("loose_by_key", {})
    for existing_key in lookup.get("by_role", {}).get(role, []):
        existing_role = existing_key[0]
        existing_content = existing_key[1] if len(existing_key) > 1 else ""
        if role != existing_role or not existing_content:
            continue
        # Exact visible-key equality was checked above. For very large payloads
        # (tool logs / request dumps), Python-in substring and fuzzy-token
        # comparisons are both expensive and low-value; doing them repeatedly
        # made session loading block the whole WebUI for many seconds. Keep
        # fuzzy matching for normal chat-sized text, but do exact-only matching
        # for giant payloads.
        if max(len(content), len(existing_content)) > 200_000:
            continue
        if content in existing_content or existing_content in content:
            return existing_key
        if loose_content is None:
            loose_content = _loose_session_message_content(content)
        loose_existing = loose_by_key.get(existing_key)
        if loose_existing is None:
            loose_existing = _loose_session_message_content(existing_content)
            loose_by_key[existing_key] = loose_existing
        if loose_content and loose_existing and (
            loose_content in loose_existing or loose_existing in loose_content
        ):
            return existing_key
    return None


def _has_visible_duplicate(visible_key: tuple, visible_keys: set[tuple]) -> bool:
    return _matching_visible_duplicate(visible_key, visible_keys) is not None
