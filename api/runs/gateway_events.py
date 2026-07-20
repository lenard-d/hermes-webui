"""Gateway protocol decoding and WebUI runtime-event publication."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .journal import bound_run_journal_snapshot_args
from .runtime_state import (
    append_runtime_partial_text,
    append_runtime_reasoning_text,
    finish_runtime_tool_call,
    replace_runtime_partial_text,
    start_runtime_tool_call,
    update_active_run,
)


def gateway_sse_delta(payload: dict) -> str:
    """Extract assistant text from an OpenAI-compatible streaming chunk."""
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            return content
        message = choice.get("message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else ""
    except Exception:
        return ""


def gateway_sse_reasoning_delta(payload: dict) -> str:
    """Extract reasoning text from OpenAI-compatible streaming chunks."""
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content")
        return (
            reasoning
            if isinstance(reasoning, str) and reasoning.strip()
            else ""
        )
    except Exception:
        return ""


def gateway_stream_usage(payload: dict) -> dict:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        ),
        "output_tokens": int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        ),
        "estimated_cost": (
            usage.get("estimated_cost")
            or usage.get("estimated_cost_usd")
            or 0
        ),
    }


def gateway_reasoning_delta(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "preview", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def gateway_tool_progress_event(payload: dict) -> tuple[str, dict] | None:
    """Translate Hermes Gateway tool-progress payloads to WebUI events."""
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("event") or "").strip().lower()
    if event_type == "reasoning.available":
        reason_delta = gateway_reasoning_delta(payload)
        return ("reasoning", {"text": reason_delta}) if reason_delta else None
    name = str(
        payload.get("tool")
        or payload.get("name")
        or payload.get("function_name")
        or ""
    ).strip()
    if not name:
        return None
    if name == "_thinking":
        reason_delta = gateway_reasoning_delta(payload)
        return ("reasoning", {"text": reason_delta}) if reason_delta else None
    if name.startswith("_"):
        return None
    status = str(payload.get("status") or "running").strip().lower()
    tool_call_id = (
        payload.get("toolCallId")
        or payload.get("tool_call_id")
        or payload.get("id")
    )
    is_complete = event_type == "tool.completed" or status in {
        "completed",
        "complete",
        "success",
        "error",
        "failed",
    }
    event_payload = {
        "event_type": "tool.completed" if is_complete else "tool.started",
        "name": name,
        "preview": payload.get("label") or payload.get("preview"),
        "args": (
            bound_run_journal_snapshot_args(payload.get("args"))
            if isinstance(payload.get("args"), dict)
            else {}
        ),
        "is_error": bool(payload.get("error"))
        or status in {"error", "failed"},
    }
    if tool_call_id:
        event_payload["tid"] = str(tool_call_id)
    return ("tool_complete" if is_complete else "tool", event_payload)


def gateway_runs_approval_event(payload: dict) -> dict | None:
    """Map a Gateway approval request to the WebUI approval contract."""
    if not isinstance(payload, dict):
        return None
    tool = str(
        payload.get("tool")
        or payload.get("function_name")
        or payload.get("pattern_key")
        or ""
    ).strip()
    command = str(payload.get("command") or "").strip()
    description = str(payload.get("description") or "").strip()
    pattern_keys = (
        payload.get("pattern_keys")
        if isinstance(payload.get("pattern_keys"), list)
        else []
    )
    pattern_key = str(payload.get("pattern_key") or "").strip()
    args: Any = (
        payload.get("args")
        if isinstance(payload.get("args"), (list, dict))
        else []
    )
    run_id = str(payload.get("run_id") or "").strip()
    approval_id = str(
        payload.get("approval_id") or payload.get("id") or ""
    ).strip()
    risk = str(payload.get("risk_level") or "high").strip()
    choices = (
        payload.get("choices")
        if isinstance(payload.get("choices"), list)
        else []
    )
    allow_permanent = payload.get("allow_permanent")
    if allow_permanent is None:
        allow_permanent = "always" in choices
    if not (tool or command or description):
        return None
    return {
        "tool": tool,
        "command": command,
        "description": description,
        "pattern_key": pattern_key,
        "pattern_keys": pattern_keys or ([pattern_key] if pattern_key else []),
        "args": args,
        "risk_level": risk,
        "run_id": run_id,
        "approval_id": approval_id,
        "choices": choices,
        "allow_permanent": bool(allow_permanent),
    }


class GatewayEventRelay:
    """Publish decoded Gateway activity through the run-owned event seam.

    The relay updates the process-local runtime snapshot and publishes the
    corresponding browser event as one operation.  Both Gateway transports use
    this module, preventing their token/reasoning/tool projections from drifting.
    """

    def __init__(
        self,
        stream_id: str,
        publish: Callable[[str, dict], None],
    ) -> None:
        self.stream_id = stream_id
        self._publish = publish

    def token(self, text: str) -> None:
        if not text:
            return
        append_runtime_partial_text(self.stream_id, text)
        self._publish("token", {"text": text})

    def replace_text(self, text: str) -> None:
        if text:
            replace_runtime_partial_text(self.stream_id, text)

    def reasoning(self, text: str) -> None:
        if not text:
            return
        append_runtime_reasoning_text(self.stream_id, text)
        self._publish("reasoning", {"text": text})

    def tool(self, translated: tuple[str, dict] | None) -> None:
        if not translated:
            return
        event_name, event_payload = translated
        if event_name == "reasoning":
            self.reasoning(str(event_payload.get("text") or ""))
            return
        if event_name == "tool":
            start_runtime_tool_call(
                self.stream_id,
                name=event_payload.get("name"),
                args=event_payload.get("args") or {},
                tool_call_id=event_payload.get("tid"),
            )
        else:
            finish_runtime_tool_call(
                self.stream_id,
                name=event_payload.get("name"),
                tool_call_id=event_payload.get("tid"),
                is_error=bool(event_payload.get("is_error")),
            )
        self._publish(event_name, event_payload)
        update_active_run(
            self.stream_id,
            phase="gateway-tool",
            latest_tool=event_payload.get("name"),
        )
