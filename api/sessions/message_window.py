"""Bounded, renderable transcript windows for session detail responses."""

from __future__ import annotations

import json

from .records import _is_empty_partial_activity_message


max_message_limit = 500
_LIMITED_TOOL_CONTENT_MAX_CHARS = 4096
_LIMITED_TOOL_CONTENT_NOTICE = (
    "\n\n[Tool output truncated in paginated session response; "
    "load the full transcript to inspect the complete result.]"
)


def includes_tool_metadata(messages) -> bool:
    """Return whether messages can reconstruct their own tool cards."""
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if isinstance(message.get("tool_calls"), list) and message.get("tool_calls"):
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "tool_use"
            for part in content
        ):
            return True
    return False


def window_tool_calls(tool_calls, start_idx: int, message_count: int) -> list:
    """Keep and rebase tool calls that anchor inside a returned window."""
    if not isinstance(tool_calls, list) or message_count <= 0:
        return []
    end_idx = start_idx + message_count
    filtered = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        assistant_idx = tool_call.get("assistant_msg_idx")
        if isinstance(assistant_idx, bool) or not isinstance(assistant_idx, int):
            continue
        if start_idx <= assistant_idx < end_idx:
            rebased = dict(tool_call)
            rebased["assistant_msg_idx"] = assistant_idx - start_idx
            filtered.append(rebased)
    return filtered


def counts_as_renderable(message) -> bool:
    """Return whether a row consumes the visible transcript-window budget."""
    if not isinstance(message, dict):
        return False
    if _is_empty_partial_activity_message(message):
        return False
    role = str(message.get("role") or "").strip().lower()
    return bool(role and role != "tool")


def _tool_call_ids(messages) -> set[str]:
    ids = set()
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        for key in ("tool_calls", "_partial_tool_calls"):
            for call in message.get(key) or []:
                if isinstance(call, dict):
                    call_id = call.get("id") or call.get("tool_call_id")
                    if call_id:
                        ids.add(str(call_id))
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    call_id = part.get("id")
                    if call_id:
                        ids.add(str(call_id))
    return ids


def _tool_result_matches(message, call_ids: set[str]) -> bool:
    if not call_ids or not isinstance(message, dict):
        return False
    if str(message.get("role") or "").lower() != "tool":
        return False
    call_id = message.get("tool_call_id") or message.get("tool_use_id") or ""
    return bool(call_id) and str(call_id) in call_ids


def message_window(
    messages,
    msg_limit=None,
    msg_before=None,
    expand_renderable=False,
) -> tuple[list, int]:
    """Return a visible-row-bounded transcript window and its source offset."""
    _ = expand_renderable
    messages = list(messages or [])
    if msg_before is not None:
        before_idx = max(0, min(int(msg_before), len(messages)))
    else:
        before_idx = len(messages)
    source = messages[:before_idx]
    if not source:
        return [], 0
    if not msg_limit:
        return source, 0
    limit = max(1, int(msg_limit))
    end_idx = len(source)
    last_renderable_idx = None
    for idx in range(end_idx - 1, -1, -1):
        if counts_as_renderable(source[idx]):
            last_renderable_idx = idx
            break
    if last_renderable_idx is None:
        start_idx = max(0, end_idx - limit)
        return source[start_idx:end_idx], start_idx

    end_idx = last_renderable_idx + 1
    visible_call_ids = _tool_call_ids(source[: last_renderable_idx + 1])
    while end_idx < len(source) and not counts_as_renderable(source[end_idx]):
        if _tool_result_matches(source[end_idx], visible_call_ids):
            end_idx += 1
        else:
            break
    start_idx = 0
    renderable_count = 0
    for idx in range(last_renderable_idx, -1, -1):
        if not counts_as_renderable(source[idx]):
            continue
        renderable_count += 1
        if renderable_count >= limit:
            start_idx = idx
            break
    return source[start_idx:end_idx], start_idx


def parse_message_limit(raw):
    """Parse a request limit and clamp it to the supported response range."""
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(value, max_message_limit))


def _bounded_tool_message(message):
    if not isinstance(message, dict) or str(message.get("role") or "").lower() != "tool":
        return message
    content = message.get("content")
    if content in (None, ""):
        return message
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            text = str(content)
    if len(text) <= _LIMITED_TOOL_CONTENT_MAX_CHARS:
        return message
    clipped = dict(message)
    preview = text[:_LIMITED_TOOL_CONTENT_MAX_CHARS] + _LIMITED_TOOL_CONTENT_NOTICE
    if isinstance(content, str):
        clipped["content"] = preview
    elif isinstance(content, list):
        clipped["content"] = [{"type": "text", "text": preview}]
    elif isinstance(content, dict):
        clipped["content"] = {"_truncated": True, "preview": preview}
    else:
        clipped["content"] = preview
    clipped["_content_truncated"] = True
    clipped["_content_original_chars"] = len(text)
    return clipped


def bounded_messages(messages) -> list:
    """Bound hidden tool-result payloads before sending a limited response."""
    return [_bounded_tool_message(message) for message in list(messages or [])]
