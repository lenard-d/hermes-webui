"""Interpret settled transcript content for Anchor scene reconstruction."""

from __future__ import annotations

import copy
import json
import re

def _anchor_scene_message_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or part.get("input_text") or ""))
            else:
                parts.append(str(part or ""))
        return "\n".join(parts)
    return str(content or "")


def _anchor_scene_content_text(part) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part or "")
    return str(
        part.get("text")
        or part.get("content")
        or part.get("input_text")
        or part.get("output_text")
        or part.get("thinking")
        or part.get("reasoning")
        or part.get("summary")
        or ""
    )


def _anchor_scene_content_visible_text(part) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part or "")
    part_type = str(part.get("type") or "")
    if part_type in ("thinking", "reasoning"):
        return ""
    content_text = part.get("content") if part_type in ("text", "input_text", "output_text") else ""
    return str(part.get("text") or part.get("input_text") or part.get("output_text") or content_text or "")


def _anchor_scene_message_has_content_tool_use(message) -> bool:
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "tool_use" for part in content
    )


def _anchor_scene_final_answer_text(message) -> str:
    if not _anchor_scene_message_has_content_tool_use(message):
        return _anchor_scene_message_text(message)
    content = message.get("content") if isinstance(message, dict) else []
    last_tool_index = -1
    for idx, part in enumerate(content):
        if isinstance(part, dict) and part.get("type") == "tool_use":
            last_tool_index = idx
    tail_text = "\n".join(
        text
        for text in (_anchor_scene_content_visible_text(part) for part in content[last_tool_index + 1 :])
        if _anchor_scene_clean_text(text)
    )
    return tail_text if _anchor_scene_clean_text(tail_text) else ""


def _anchor_scene_message_reasoning_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning", "_reasoning", "reasoning_content", "thinking"):
        value = message.get(key)
        if not value:
            continue
        if isinstance(value, list):
            parts = []
            for part in value:
                if isinstance(part, dict):
                    parts.append(
                        str(
                            part.get("text")
                            or part.get("content")
                            or part.get("reasoning")
                            or part.get("summary")
                            or ""
                        )
                    )
                else:
                    parts.append(str(part or ""))
            return "\n".join(parts)
        if isinstance(value, dict):
            return str(
                value.get("text")
                or value.get("content")
                or value.get("reasoning")
                or value.get("summary")
                or ""
            )
        return str(value or "")
    return ""


def _anchor_scene_clean_text(value) -> str:
    return " ".join(str(value or "").split()).strip()


def _anchor_scene_text_key(value) -> str:
    return _anchor_scene_clean_text(value).lower()


_ANCHOR_SCENE_SETTLED_SNIPPET_CAP = 4000


def _anchor_scene_string_payload(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except Exception:
        return str(value)


def _anchor_scene_is_bounded_tool_body_preview(settled, full) -> bool:
    settled_text = _anchor_scene_string_payload(settled)
    full_text = _anchor_scene_string_payload(full)
    return bool(
        settled_text
        and full_text
        and len(full_text) > len(settled_text)
        and len(settled_text) >= _ANCHOR_SCENE_SETTLED_SNIPPET_CAP
        and full_text.startswith(settled_text)
    )


def _anchor_scene_row_looks_like_final_answer(row_text_key: str, final_key: str) -> bool:
    if not row_text_key or not final_key:
        return False
    if row_text_key == final_key:
        return True
    # #4587: align with the renderer's _anchorSceneProseMatchesFinalAnswer — a
    # prefix-like overlap only counts as "the final answer" when it's a
    # NEAR-complete match (ratio >= 0.9). A shorter intermediate-prose row that
    # is merely a prefix of the final answer is legitimate progress narration and
    # must be preserved in the persisted scene, not filtered out.
    if not (final_key.startswith(row_text_key) or row_text_key.startswith(final_key)):
        return False
    shorter = min(len(row_text_key), len(final_key))
    longer = max(len(row_text_key), len(final_key))
    return shorter >= 80 and longer > 0 and (shorter / longer) >= 0.9


def _anchor_scene_text_has_long_overlap(text_key: str, final_key: str) -> bool:
    if len(text_key) < 80 or len(final_key) < 80:
        return False
    shorter, longer = (text_key, final_key) if len(text_key) <= len(final_key) else (final_key, text_key)
    window = 64
    scan_limit = min(len(shorter), 1400)
    if scan_limit < window:
        return False
    for start in range(0, scan_limit - window + 1, 24):
        chunk = shorter[start : start + window].strip()
        if len(chunk) >= 48 and chunk in longer:
            return True
    text_tokens = set(re.findall(r"[a-z0-9_./:-]{3,}", text_key))
    final_tokens = set(re.findall(r"[a-z0-9_./:-]{3,}", final_key))
    if text_tokens and final_tokens:
        common = text_tokens & final_tokens
        shorter_count = min(len(text_tokens), len(final_tokens))
        if shorter_count >= 3 and len(common) >= min(5, shorter_count) and (len(common) / shorter_count) >= 0.5:
            return True
    text_compact = re.sub(r"[\s`*_#|\[\](){}<>.,;:!?，。；：！？、/\\-]+", "", text_key)
    final_compact = re.sub(r"[\s`*_#|\[\](){}<>.,;:!?，。；：！？、/\\-]+", "", final_key)
    if len(text_compact) >= 40 and len(final_compact) >= 40:
        text_grams = {text_compact[idx : idx + 4] for idx in range(0, len(text_compact) - 3)}
        final_grams = {final_compact[idx : idx + 4] for idx in range(0, len(final_compact) - 3)}
        common_grams = text_grams & final_grams
        shorter_grams = min(len(text_grams), len(final_grams))
        if shorter_grams and len(common_grams) >= 12 and (len(common_grams) / shorter_grams) >= 0.35:
            return True
    return False


def _anchor_scene_row_is_stale_token_answer(row, row_text_key: str, final_key: str) -> bool:
    if not isinstance(row, dict) or row.get("role") not in ("prose", "thinking"):
        return False
    source_type = str(row.get("source_event_type") or row.get("source") or "")
    if source_type != "token":
        return False
    return _anchor_scene_text_has_long_overlap(row_text_key, final_key)


def _anchor_scene_message_turn_duration(message):
    if not isinstance(message, dict):
        return None
    for key in ("_turnDuration", "_turn_duration", "turn_duration"):
        value = message.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return value
    return None


def _anchor_scene_tool_id(tool) -> str:
    if not isinstance(tool, dict):
        return ""
    return str(
        tool.get("tid")
        or tool.get("id")
        or tool.get("tool_call_id")
        or tool.get("tool_use_id")
        or tool.get("call_id")
        or ""
    ).strip()


def _anchor_scene_tool_name(tool) -> str:
    if not isinstance(tool, dict):
        return "tool"
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(tool.get("name") or tool.get("tool_name") or fn.get("name") or "tool").strip() or "tool"


def _anchor_scene_tool_args(tool):
    if not isinstance(tool, dict):
        return {}
    for key in ("args", "input"):
        value = tool.get(key)
        if isinstance(value, dict):
            return copy.deepcopy(value)
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    raw = fn.get("arguments")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _anchor_scene_content_tool(part):
    if not isinstance(part, dict):
        return {}
    fn = part.get("function") if isinstance(part.get("function"), dict) else {}
    tool_id = (
        part.get("id")
        or part.get("tid")
        or part.get("tool_call_id")
        or part.get("tool_use_id")
        or part.get("call_id")
    )
    return {
        "id": tool_id,
        "tid": part.get("tid") or tool_id,
        "tool_call_id": part.get("tool_call_id"),
        "tool_use_id": part.get("tool_use_id"),
        "call_id": part.get("call_id"),
        "name": part.get("name") or part.get("tool_name") or fn.get("name") or "tool",
        "tool_name": part.get("tool_name"),
        "args": part.get("args"),
        "input": part.get("input"),
        "function": copy.deepcopy(part.get("function")) if isinstance(part.get("function"), dict) else None,
        "command": part.get("command") or part.get("raw_command") or part.get("original_command") or part.get("display_command"),
        "preview": part.get("preview") or part.get("summary"),
        "snippet": part.get("snippet") or part.get("result") or part.get("output"),
        "result": copy.deepcopy(part.get("result")),
        "output": copy.deepcopy(part.get("output")),
        "is_error": part.get("is_error"),
        "error": part.get("error"),
        "duration": part.get("duration"),
        "started_at": part.get("started_at"),
    }
