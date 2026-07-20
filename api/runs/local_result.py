"""Transcript merge for provider results produced by a local run."""

from __future__ import annotations

from dataclasses import dataclass

from .context_replay import _dedupe_replayed_context_messages
from .message_sanitization import (
    _assign_stable_message_ids,
    _deduplicate_context_messages,
)
from .post_compression_context import (
    _restore_display_reasoning_metadata,
    _restore_reasoning_metadata,
)
from .terminal_outcomes import (
    _agent_result_tool_limit_reached,
    _drop_synthetic_max_iteration_summary_requests,
    _maybe_inject_max_iteration_summary_fallback,
)
from .thinking_content import _strip_xml_tool_calls
from .transcript import _merge_display_messages_after_agent_result
from .turn_context import _advance_truncation_watermark_after_commit


@dataclass(frozen=True)
class MergedLocalResult:
    result: dict
    messages: list
    tool_limit_reached: bool


def merge_local_result(
    session,
    result: dict,
    *,
    previous_messages: list,
    previous_context_messages: list,
    message_text: str,
) -> MergedLocalResult:
    """Project one provider result into display and model-context transcripts."""

    tool_limit_reached = _agent_result_tool_limit_reached(result)
    result_messages = result.get("messages") or previous_context_messages
    result_messages = _drop_synthetic_max_iteration_summary_requests(
        result_messages,
        enabled=tool_limit_reached,
    )
    if tool_limit_reached:
        result_messages = _maybe_inject_max_iteration_summary_fallback(
            result_messages,
            result,
        )
        result = {**result, "messages": result_messages}

    next_context = _restore_reasoning_metadata(
        previous_context_messages,
        result_messages,
    )
    _assign_stable_message_ids(
        result_messages,
        previous_messages,
        previous_context_messages,
    )
    next_context = _dedupe_replayed_context_messages(
        previous_context_messages,
        next_context,
        message_text,
    )
    session.context_messages = _deduplicate_context_messages(next_context)
    session.messages = _merge_display_messages_after_agent_result(
        previous_messages,
        previous_context_messages,
        _restore_display_reasoning_metadata(previous_messages, result_messages),
        message_text,
        source=getattr(session, "pending_user_source", None) or "webui",
    )
    _advance_truncation_watermark_after_commit(session)
    _strip_xml_blocks(session.messages)
    return MergedLocalResult(
        result=result,
        messages=result_messages,
        tool_limit_reached=tool_limit_reached,
    )


def _strip_xml_blocks(messages: list) -> None:
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = _strip_xml_tool_calls(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = _strip_xml_tool_calls(part["text"])
