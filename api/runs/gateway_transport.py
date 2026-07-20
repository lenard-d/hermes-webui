"""HTTP transports for Gateway-backed WebUI runs.

This module owns request construction, byte-level SSE iteration, protocol event
decoding, and transport cancellation.  Durable session settlement remains in
``gateway_settlement`` so an HTTP failure cannot partially masquerade as a
successful transcript writeback.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .attachments import _build_native_multimodal_message
from .gateway_events import (
    GatewayEventRelay,
    gateway_reasoning_delta,
    gateway_runs_approval_event,
    gateway_sse_delta,
    gateway_sse_reasoning_delta,
    gateway_stream_usage,
    gateway_tool_progress_event,
)
from .gateway_runtime import bind_gateway_run
from .message_sanitization import _strip_oob_blocks
from .runtime_state import update_active_run


logger = logging.getLogger(__name__)

_GATEWAY_READ_TIMEOUT_ENV = "HERMES_WEBUI_GATEWAY_READ_TIMEOUT"
_GATEWAY_READ_TIMEOUT_DEFAULT = 600.0


@dataclass(frozen=True)
class GatewayStreamResult:
    """Terminal result of one Gateway HTTP transport."""

    final_text: str = ""
    usage: dict = field(default_factory=dict)
    terminal_error: str = ""
    cancelled: bool = False
    cancel_message: str = ""


def gateway_read_timeout_secs() -> float:
    """Return the terminal byte-silence budget for Gateway SSE reads."""
    raw = os.environ.get(_GATEWAY_READ_TIMEOUT_ENV)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return _GATEWAY_READ_TIMEOUT_DEFAULT


def iter_sse_lines_cancellable(resp, cancel_event):
    """Yield SSE lines, treating a poisoned/timeout socket as terminal.

    A cancellation observed while the socket raises ``OSError`` yields one
    blank line so the consuming transport can return its normal cancelled
    result.  Without cancellation the transport error is propagated.
    """
    resp_iter = iter(resp)
    while True:
        try:
            yield next(resp_iter)
        except StopIteration:
            return
        except OSError:
            if cancel_event.is_set():
                yield b""
                return
            raise


def _gateway_message_content(msg_text, attachments, workspace, cfg) -> Any:
    message_content: Any = str(msg_text or "")
    if not attachments:
        return message_content
    try:
        return _build_native_multimodal_message(
            "",
            str(msg_text or ""),
            attachments,
            str(workspace),
            cfg=cfg,
        )
    except Exception:
        logger.debug(
            "Failed to build Gateway multimodal attachment payload",
            exc_info=True,
        )
        return message_content


def _gateway_headers(session_id: str, api_key: str, *, sse: bool) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if sse else "application/json",
        "X-Hermes-Session-Id": session_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-Hermes-Session-Key"] = f"webui:{session_id}"
    return headers


def _gateway_runs_body(
    *,
    session_id,
    msg_text,
    model,
    workspace,
    prefill_messages,
    body_extras,
    attachments,
    cfg,
    session,
) -> dict:
    instructions_parts: list[str] = []
    conversation_history: list[dict] = []
    for entry in getattr(session, "context_messages", None) or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = entry.get("content")
        if content is not None:
            conversation_history.append(
                {"role": role, "content": _strip_oob_blocks(content)}
            )
    for entry in prefill_messages or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        content = entry.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions_parts.append(content)
            elif content is not None:
                instructions_parts.append(str(content))
            continue
        if role in {"user", "assistant"} and content is not None:
            conversation_history.append(
                {"role": role, "content": _strip_oob_blocks(content)}
            )

    run_input = _gateway_message_content(
        msg_text,
        attachments,
        workspace,
        cfg,
    )
    if isinstance(run_input, list):
        run_input = [{"role": "user", "content": run_input}]
    body = {
        "model": model or "default",
        "input": run_input,
        **body_extras,
        "session_id": session_id,
    }
    if instructions_parts:
        body["instructions"] = "\n\n".join(instructions_parts)
    if conversation_history:
        body["conversation_history"] = conversation_history
    return body


def stream_gateway_runs_api(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    base_url,
    api_key,
    prefill_messages,
    body_extras,
    *,
    publish,
    cancel_event,
    attachments=None,
    cfg=None,
    session=None,
) -> GatewayStreamResult:
    """Submit one turn through ``/v1/runs`` and observe its event stream."""
    headers = _gateway_headers(session_id, api_key, sse=False)
    body = _gateway_runs_body(
        session_id=session_id,
        msg_text=msg_text,
        model=model,
        workspace=workspace,
        prefill_messages=prefill_messages,
        body_extras=body_extras,
        attachments=attachments,
        cfg=cfg,
        session=session,
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/runs",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    update_active_run(stream_id, phase="gateway-request")
    with urllib.request.urlopen(request, timeout=30) as response:
        run_data = json.loads(response.read(65536))
    run_id = str(run_data.get("run_id") or run_data.get("id") or "").strip()
    if not run_id:
        raise ValueError(f"Gateway runs API returned no run_id: {run_data!r}")
    bind_gateway_run(stream_id, run_id)

    event_headers = dict(headers)
    event_headers["Accept"] = "text/event-stream"
    event_request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/runs/{run_id}/events",
        headers=event_headers,
        method="GET",
    )
    relay = GatewayEventRelay(stream_id, publish)
    final_text = ""
    usage: dict = {}
    sse_event = "message"
    with urllib.request.urlopen(
        event_request,
        timeout=gateway_read_timeout_secs(),
    ) as response:
        for raw_line in iter_sse_lines_cancellable(response, cancel_event):
            if cancel_event.is_set():
                return GatewayStreamResult(
                    usage=usage,
                    cancelled=True,
                    cancel_message="Cancelled by user",
                )
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                sse_event = "message"
                continue
            if line.startswith("event:"):
                sse_event = line[6:].strip() or "message"
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            payload_event = str(
                payload.get("event") or payload.get("type") or sse_event
            ).strip() or "message"
            if payload_event == "approval.request":
                approval = gateway_runs_approval_event(payload)
                if approval:
                    approval["run_id"] = run_id
                    publish("approval", approval)
                sse_event = "message"
                continue
            if payload_event in {
                "tool.started",
                "tool.completed",
                "reasoning.available",
            }:
                relay.tool(gateway_tool_progress_event(payload))
                sse_event = "message"
                continue
            if payload_event == "message.delta":
                delta = str(payload.get("delta") or "")
                final_text += delta
                relay.token(delta)
                sse_event = "message"
                continue
            if payload_event == "run.completed":
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                output = str(payload.get("output") or "")
                if output and not final_text:
                    final_text = output
                    relay.replace_text(output)
                usage.update(
                    {k: v for k, v in gateway_stream_usage(payload).items() if v}
                )
                sse_event = "message"
                continue
            if payload_event == "run.failed":
                raise RuntimeError(
                    str(payload.get("error") or "Gateway run failed")
                )
            if payload_event == "run.cancelled":
                return GatewayStreamResult(
                    usage=usage,
                    cancelled=True,
                    cancel_message="Cancelled by gateway",
                )
            reasoning_delta = gateway_sse_reasoning_delta(payload)
            relay.reasoning(reasoning_delta)
            delta = gateway_sse_delta(payload)
            final_text += delta
            relay.token(delta)
            usage.update(
                {k: v for k, v in gateway_stream_usage(payload).items() if v}
            )
    return GatewayStreamResult(final_text=final_text, usage=usage)


def stream_gateway_chat_completions(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    base_url,
    api_key,
    prefill_messages,
    request_extras,
    *,
    publish,
    cancel_event,
    attachments=None,
    cfg=None,
) -> GatewayStreamResult:
    """Submit one turn through the legacy chat-completions transport."""
    message_content = _gateway_message_content(
        msg_text,
        attachments,
        workspace,
        cfg,
    )
    body = {
        "model": model or "default",
        "stream": True,
        "messages": [
            *prefill_messages,
            {"role": "user", "content": message_content},
        ],
        **request_extras,
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=_gateway_headers(session_id, api_key, sse=True),
        method="POST",
    )
    update_active_run(stream_id, phase="gateway-request")
    relay = GatewayEventRelay(stream_id, publish)
    final_text = ""
    terminal_error = ""
    usage: dict = {}
    last_payload: dict = {}
    sse_event = "message"
    with urllib.request.urlopen(
        request,
        timeout=gateway_read_timeout_secs(),
    ) as response:
        for raw_line in iter_sse_lines_cancellable(response, cancel_event):
            if cancel_event.is_set():
                return GatewayStreamResult(
                    final_text=final_text,
                    usage=usage,
                    cancelled=True,
                    cancel_message="Cancelled by user",
                )
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                sse_event = "message"
                continue
            if line.startswith("event:"):
                sse_event = line[6:].strip() or "message"
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            payload_event = str(
                payload.get("event") or payload.get("type") or sse_event
            ).strip()
            if payload_event in {
                "hermes.approval.request",
                "approval.request",
            }:
                approval = gateway_runs_approval_event(payload)
                if approval:
                    run_id = str(approval.get("run_id") or "").strip()
                    if run_id:
                        bind_gateway_run(stream_id, run_id)
                    publish("approval", approval)
                    try:
                        from api.route_approvals import (
                            submit_gateway_pending_mirror,
                        )

                        submit_gateway_pending_mirror(session_id, approval)
                    except Exception:
                        logger.debug(
                            "submit_gateway_pending_mirror failed",
                            exc_info=True,
                        )
                else:
                    logger.debug("Ignoring malformed Gateway approval payload")
                sse_event = "message"
                continue
            if sse_event == "hermes.tool.progress":
                relay.tool(gateway_tool_progress_event(payload))
                sse_event = "message"
                continue
            if sse_event == "reasoning.available":
                relay.reasoning(gateway_reasoning_delta(payload))
                sse_event = "message"
                continue
            last_payload = payload
            if payload.get("error"):
                terminal_error = str(payload["error"])
            relay.reasoning(gateway_sse_reasoning_delta(payload))
            delta = gateway_sse_delta(payload)
            final_text += delta
            relay.token(delta)
            usage.update(
                {k: v for k, v in gateway_stream_usage(payload).items() if v}
            )
    usage.update(
        {k: v for k, v in gateway_stream_usage(last_payload).items() if v}
    )
    return GatewayStreamResult(
        final_text=final_text,
        usage=usage,
        terminal_error=terminal_error,
    )
