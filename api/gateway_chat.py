"""Default-off Hermes Gateway bridge for browser-originated chat turns."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from api.config import (
    PENDING_GOAL_CONTINUATION,
    append_runtime_partial_text,
    append_runtime_reasoning_text,
    coerce_reasoning_effort_for_model,
    finish_runtime_tool_call,
    gateway_approval_unavailable_reason,
    gateway_supports_approval,
    replace_runtime_partial_text,
    runtime_progress_snapshot,
    start_runtime_tool_call,
    update_active_run,
)
from api.helpers import _redact_text, redact_session_data
from api.models import clear_process_wakeup_pause, get_session, merge_session_messages_append_only
from api.run_journal import bound_run_journal_snapshot_args
from api.session_repository import commit_stream_writeback
from api.turn_execution import TurnExecution

logger = logging.getLogger(__name__)

# Maps stream_id -> gateway run_id for approval response relay.
_STREAM_RUN_IDS: dict[str, str] = {}

_WEBUI_CHAT_BACKEND_ENV = "HERMES_WEBUI_CHAT_BACKEND"
_WEBUI_GATEWAY_BASE_URL_ENV = "HERMES_WEBUI_GATEWAY_BASE_URL"
_WEBUI_GATEWAY_API_KEY_ENV = "HERMES_WEBUI_GATEWAY_API_KEY"
_WEBUI_GATEWAY_USE_RUNS_API_ENV = "HERMES_WEBUI_GATEWAY_USE_RUNS_API"
_GATEWAY_CHAT_BACKENDS = {"gateway", "api_server", "api-server"}


# Total byte-silence budget (seconds) for the gateway SSE socket, applied via
# ``urlopen(timeout=...)``. A stream that emits ANY byte within the window never
# trips it, so a genuinely alive (if slow) token stream is untouched; only more
# than this much *total* byte-silence is treated as a dead/stalled gateway.
#
# This is a TERMINAL budget, not a per-read grace: CPython's ``socket.makefile``
# latches ``_timeout_occurred`` on the first ``socket.timeout``, after which every
# further read raises a bare ``OSError`` — the connection cannot be resumed. So a
# read timeout ends the turn (surfacing Stop if pressed). It replaces the old flat
# 600s timeout, under which a half-open gateway (TCP open, zero bytes) pinned the
# worker for the full 10 minutes and ignored Stop (cancel is only re-checked
# between SSE lines). We KEEP the 600s default budget: there is no Gateway
# protocol heartbeat guaranteeing sub-600s progress bytes, so a legitimately
# long/fully-silent server-side tool call must not be terminated early — reducing
# the default below 600s would kill currently-working turns (gate finding, #5789).
# The win here is that a read timeout is now TERMINAL and Stop-honoring (the old
# flat timeout ignored Stop on a half-open gateway); the budget itself stays 600s
# for backward compatibility. Deployments that want a tighter dead-gateway cap can
# lower ``HERMES_WEBUI_GATEWAY_READ_TIMEOUT``.
_GATEWAY_READ_TIMEOUT_ENV = "HERMES_WEBUI_GATEWAY_READ_TIMEOUT"
_GATEWAY_READ_TIMEOUT_DEFAULT = 600.0


def _gateway_read_timeout_secs() -> float:
    """Total byte-silence budget for gateway SSE reads (default 600s, env-tunable)."""
    raw = os.environ.get(_GATEWAY_READ_TIMEOUT_ENV)
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return _GATEWAY_READ_TIMEOUT_DEFAULT


def _iter_sse_lines_cancellable(resp, cancel_event):
    """Yield raw SSE lines from ``resp``, unblocking cleanly on a read timeout.

    ``resp``'s socket carries a read timeout (``urlopen(timeout=...)``). A read
    that blocks past it raises ``socket.timeout``, and that timeout is TERMINAL:
    CPython's ``socket.makefile`` latches ``_timeout_occurred`` on the first
    timeout, so every subsequent read raises a bare ``OSError`` ("cannot read
    from timed out object") — there is no multi-read grace to reclaim. So on a
    read timeout (or the poisoned-socket ``OSError``/any read error) this either
    surfaces the user's Stop or tears the stalled turn down:

      - cancel set -> yield ``b""`` (the caller's ``if cancel_event.is_set()``
        branch emits its cancel event), then stop. This is why the old flat 600s
        pin — where a stalled gateway ignored Stop until it eventually errored —
        is gone: Stop is honored within one timeout window.
      - otherwise -> re-raise, so the caller's error handling reports the stall.

    A stream that keeps emitting bytes within the timeout window never trips it,
    so a genuinely alive (if slow) token stream is untouched. Emitting ``b""`` is
    safe: the SSE loops decode it to an empty line and ``continue`` (same as a
    real blank line).

    Iterates ``resp`` via the iterator protocol so a real ``HTTPResponse`` and the
    test fakes (which implement ``__iter__``) behave identically.
    """
    resp_iter = iter(resp)
    while True:
        try:
            raw_line = next(resp_iter)
        except StopIteration:
            return  # EOF
        except OSError:
            # socket.timeout / TimeoutError are OSError subclasses, as is the
            # post-timeout poisoned-socket "cannot read" error. All are terminal
            # for this connection.
            if cancel_event.is_set():
                yield b""  # let the caller emit its cancel event
                return
            raise
        yield raw_line


def webui_chat_backend_mode(config_data=None, environ: dict[str, str] | None = None) -> str:
    """Return the explicitly selected browser chat backend.

    The default remains the in-process WebUI runtime. Only explicit gateway
    values opt browser chat into the Hermes API server bridge; generic truthy
    strings are deliberately ignored so deployments do not change execution
    ownership by accident.
    """
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_CHAT_BACKEND_ENV)
        or cfg.get("webui_chat_backend")
        or ""
    ).strip().lower()
    if raw in _GATEWAY_CHAT_BACKENDS:
        return "gateway"
    return "legacy"


def webui_gateway_chat_enabled(config_data=None, environ: dict[str, str] | None = None) -> bool:
    return webui_chat_backend_mode(config_data, environ) == "gateway"


def _gateway_base_url(config_data=None, environ: dict[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_GATEWAY_BASE_URL_ENV)
        or cfg.get("webui_gateway_base_url")
        or "http://127.0.0.1:8642"
    ).strip()
    return raw.rstrip("/") or "http://127.0.0.1:8642"


def _gateway_api_key(environ: dict[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    return str(
        source.get(_WEBUI_GATEWAY_API_KEY_ENV)
        or source.get("API_SERVER_KEY")
        or ""
    ).strip()


def _gateway_use_runs_api_enabled(config_data=None, environ: dict[str, str] | None = None) -> bool:
    """Return True only when the operator has explicitly opted into the runs API path."""
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_GATEWAY_USE_RUNS_API_ENV)
        or cfg.get("webui_gateway_use_runs_api")
        or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _gateway_reasoning_effort_for_request(cfg, *, model=None, model_provider=None):
    """Read and coerce user-configured reasoning effort for a gateway request."""
    try:
        cfg_data = cfg if isinstance(cfg, dict) else {}
        effort_cfg = cfg_data.get("agent", {}) if isinstance(cfg_data, dict) else {}
        effort_raw = effort_cfg.get("reasoning_effort") if isinstance(effort_cfg, dict) else None
        coerced = coerce_reasoning_effort_for_model(
            effort_raw,
            model,
            provider_id=model_provider,
        )
        # Preserve explicit "none" while still omitting absent or invalid effort.
        return None if not coerced else str(coerced)
    except Exception:
        return None


def gateway_chat_config_status(config_data=None, environ: dict[str, str] | None = None) -> dict:
    """Return redacted Gateway-backed chat configuration status."""
    mode = webui_chat_backend_mode(config_data, environ)
    base_url = _gateway_base_url(config_data, environ)
    return {
        "enabled": mode == "gateway",
        "backend": mode,
        "base_url_configured": bool(base_url),
        "api_key_configured": bool(_gateway_api_key(environ)),
    }


def _gateway_http_error_event(exc: urllib.error.HTTPError, err_body: str, *, api_key_configured: bool) -> dict:
    safe = _redact_text(err_body or str(exc))[:500]
    if exc.code == 401:
        return {
            "label": "Gateway authentication failed",
            "type": "gateway_auth_error",
            "message": "Gateway rejected the WebUI API key (HTTP 401).",
            "hint": (
                "Set HERMES_WEBUI_GATEWAY_API_KEY to the same value as the Hermes Gateway "
                "API_SERVER_KEY, or disable HERMES_WEBUI_CHAT_BACKEND=gateway."
                if not api_key_configured
                else "Check that HERMES_WEBUI_GATEWAY_API_KEY matches the Hermes Gateway API_SERVER_KEY."
            ),
        }
    return {
        "label": "Gateway request failed",
        "type": "gateway_http_error",
        "message": f"Gateway returned HTTP {exc.code}.",
        "hint": safe or "Check the configured Gateway API server.",
    }


def _gateway_sse_delta(payload: dict) -> str:
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


def _gateway_sse_reasoning_delta(payload: dict) -> str:
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
        return reasoning if isinstance(reasoning, str) and reasoning.strip() else ""
    except Exception:
        return ""


def _gateway_stream_usage(payload: dict) -> dict:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "estimated_cost": usage.get("estimated_cost") or usage.get("estimated_cost_usd") or 0,
    }


def _gateway_reasoning_delta(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "preview", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _gateway_tool_progress_event(payload: dict) -> tuple[str, dict] | None:
    """Translate Hermes Gateway tool-progress SSE payloads to WebUI events."""
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("event") or "").strip().lower()
    if event_type == "reasoning.available":
        reason_delta = _gateway_reasoning_delta(payload)
        if not reason_delta:
            return None
        return "reasoning", {"text": reason_delta}
    name = str(payload.get("tool") or payload.get("name") or payload.get("function_name") or "").strip()
    if not name:
        return None
    if name == "_thinking":
        reason_delta = _gateway_reasoning_delta(payload)
        if not reason_delta:
            return None
        return "reasoning", {"text": reason_delta}
    if name.startswith("_"):
        return None
    status = str(payload.get("status") or "running").strip().lower()
    tid = payload.get("toolCallId") or payload.get("tool_call_id") or payload.get("id")
    is_complete = event_type == "tool.completed" or status in {"completed", "complete", "success", "error", "failed"}
    event_payload = {
        "event_type": "tool.completed" if is_complete else "tool.started",
        "name": name,
        "preview": payload.get("label") or payload.get("preview"),
        "args": bound_run_journal_snapshot_args(payload.get("args"))
        if isinstance(payload.get("args"), dict)
        else {},
        "is_error": bool(payload.get("error")) or status in {"error", "failed"},
    }
    if tid:
        event_payload["tid"] = str(tid)
    return ("tool_complete" if is_complete else "tool"), event_payload


def _gateway_runs_approval_event(payload: dict) -> dict | None:
    """Map a runs-API approval.request payload to the WebUI approval contract."""
    if not isinstance(payload, dict):
        return None
    tool = str(payload.get("tool") or payload.get("function_name") or payload.get("pattern_key") or "").strip()
    command = str(payload.get("command") or "").strip()
    description = str(payload.get("description") or "").strip()
    pattern_keys = payload.get("pattern_keys") if isinstance(payload.get("pattern_keys"), list) else []
    pattern_key = str(payload.get("pattern_key") or "").strip()
    args = payload.get("args") if isinstance(payload.get("args"), (list, dict)) else []
    run_id = str(payload.get("run_id") or "").strip()
    approval_id = str(payload.get("approval_id") or payload.get("id") or "").strip()
    risk = str(payload.get("risk_level") or "high").strip()
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
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


def _run_gateway_runs_api_streaming(
    session_id, msg_text, model, workspace, stream_id,
    base_url, api_key, prefill_messages, body_extras,
    *, put_gateway_event, cancel_event,
    attachments=None, cfg=None, session=None,
):
    """Submit via POST /v1/runs and relay SSE events including approval."""
    url_runs = f"{base_url.rstrip('/')}/v1/runs"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Hermes-Session-Id": session_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-Hermes-Session-Key"] = f"webui:{session_id}"
    message_content: Any = str(msg_text or "")
    if attachments:
        try:
            from api.streaming import _build_native_multimodal_message

            message_content = _build_native_multimodal_message("", str(msg_text or ""), attachments, str(workspace), cfg=cfg)
        except Exception:
            logger.debug("Failed to build runs-API multimodal attachment payload", exc_info=True)
            message_content = str(msg_text or "")
    from api.streaming import _strip_oob_blocks

    instructions_parts = []
    conversation_history = []
    for entry in getattr(session, "context_messages", None) or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = entry.get("content")
        if content is not None:
            content = _strip_oob_blocks(content)
            conversation_history.append({"role": role, "content": content})
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
        if role not in {"user", "assistant"}:
            continue
        if content is not None:
            content = _strip_oob_blocks(content)
        conversation_history.append({"role": role, "content": content})
    run_input = message_content
    if isinstance(run_input, list):
        run_input = [{"role": "user", "content": run_input}]
    run_body = {
        "model": model or "default",
        "input": run_input,
        **body_extras,
        "session_id": session_id,
    }
    if instructions_parts:
        run_body["instructions"] = "\n\n".join(part for part in instructions_parts if part)
    if conversation_history:
        run_body["conversation_history"] = conversation_history
    req = urllib.request.Request(
        url_runs,
        data=json.dumps(run_body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    update_active_run(stream_id, phase="gateway-request")
    with urllib.request.urlopen(req, timeout=30) as resp:
        run_data = json.loads(resp.read(65536))
    run_id = str(run_data.get("run_id") or run_data.get("id") or "").strip()
    if not run_id:
        raise ValueError(f"Gateway runs API returned no run_id: {run_data!r}")

    _STREAM_RUN_IDS[stream_id] = run_id

    url_events = f"{base_url.rstrip('/')}/v1/runs/{run_id}/events"
    headers_sse = dict(headers)
    headers_sse["Accept"] = "text/event-stream"
    req_events = urllib.request.Request(url_events, headers=headers_sse, method="GET")
    final_text = ""
    usage: dict = {}
    sse_event = "message"
    with urllib.request.urlopen(req_events, timeout=_gateway_read_timeout_secs()) as resp:
        for raw_line in _iter_sse_lines_cancellable(resp, cancel_event):
            if cancel_event.is_set():
                put_gateway_event("cancel", {"message": "Cancelled by user"})
                return None, usage
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
            payload_event = str(payload.get("event") or payload.get("type") or sse_event).strip() or "message"
            if payload_event == "approval.request":
                approval_data = _gateway_runs_approval_event(payload)
                if approval_data:
                    approval_data["run_id"] = run_id
                    put_gateway_event("approval", approval_data)
                sse_event = "message"
                continue
            if payload_event in {"tool.started", "tool.completed", "reasoning.available"}:
                translated = _gateway_tool_progress_event(payload)
                if translated:
                    event_name, event_payload = translated
                    if event_name == "reasoning":
                        reason_delta = event_payload.get("text")
                        if reason_delta:
                            append_runtime_reasoning_text(stream_id, reason_delta)
                    else:
                        if event_name == "tool":
                            start_runtime_tool_call(
                                stream_id,
                                name=event_payload.get("name"),
                                args=event_payload.get("args") or {},
                                tool_call_id=event_payload.get("tid"),
                            )
                        elif event_name == "tool_complete":
                            finish_runtime_tool_call(
                                stream_id,
                                name=event_payload.get("name"),
                                tool_call_id=event_payload.get("tid"),
                                is_error=bool(event_payload.get("is_error")),
                            )
                    put_gateway_event(event_name, event_payload)
                    if event_name != "reasoning":
                        update_active_run(stream_id, phase="gateway-tool", latest_tool=event_payload.get("name"))
                sse_event = "message"
                continue
            if payload_event == "message.delta":
                delta = str(payload.get("delta") or "")
                if delta:
                    final_text += delta
                    append_runtime_partial_text(stream_id, delta)
                    put_gateway_event("token", {"text": delta})
                sse_event = "message"
                continue
            if payload_event == "run.completed":
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                output = str(payload.get("output") or "")
                if output and not final_text:
                    final_text = output
                    replace_runtime_partial_text(stream_id, output)
                usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
                sse_event = "message"
                continue
            if payload_event == "run.failed":
                raise RuntimeError(str(payload.get("error") or "Gateway run failed"))
            if payload_event == "run.cancelled":
                put_gateway_event("cancel", {"message": "Cancelled by gateway"})
                return None, usage
            reasoning_delta = _gateway_sse_reasoning_delta(payload)
            if reasoning_delta:
                append_runtime_reasoning_text(stream_id, reasoning_delta)
                put_gateway_event("reasoning", {"text": reasoning_delta})
            delta = _gateway_sse_delta(payload)
            if delta:
                final_text += delta
                append_runtime_partial_text(stream_id, delta)
                put_gateway_event("token", {"text": delta})
            usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
    return final_text, usage


def _settle_gateway_terminal_error(
    session_id,
    stream_id,
    workspace,
    model,
    model_provider,
    terminal_error,
    *,
    event_payload=None,
    session=None,
):
    from api.streaming import (
        _classify_provider_error,
        _materialize_pending_user_turn_before_error,
        _provider_error_payload,
        _session_payload_with_full_messages,
        _snapshot_and_append_partial_on_error,
    )

    if event_payload is None:
        error_classification = _classify_provider_error(terminal_error)
        error_payload = _provider_error_payload(
            terminal_error,
            error_classification["type"],
            error_classification.get("hint", ""),
        )
        error_payload.setdefault("label", error_classification["label"])
    else:
        error_payload = dict(event_payload)
        error_classification = {
            "label": str(error_payload.get("label") or "Gateway request failed"),
        }

    def settle(current):
        _materialize_pending_user_turn_before_error(current)
        current.active_stream_id = None
        current.pending_user_message = None
        current.pending_attachments = []
        current.pending_started_at = None
        current.pending_user_source = None
        try:
            _snapshot_and_append_partial_on_error(current, stream_id)
        except Exception:
            logger.debug("Failed to snapshot gateway partials on terminal error", exc_info=True)
        error_message = {
            "role": "assistant",
            "content": (
                f"**{error_classification['label']}:** "
                f"{error_payload.get('message') or error_classification['label']}"
            ) + (f"\n\n*{error_payload['hint']}*" if error_payload.get("hint") else ""),
            "timestamp": int(time.time()),
            "_error": True,
        }
        if error_payload.get("details"):
            error_message["provider_details"] = error_payload["details"]
        if not isinstance(current.messages, list):
            current.messages = []
        current.messages.append(error_message)
        current.workspace = str(workspace)
        current.model = model
        current.model_provider = model_provider

    result = commit_stream_writeback(
        session_id,
        expected_stream_id=stream_id,
        mutate=settle,
        session=session,
    )
    if result is None:
        return None
    current = result.session
    error_payload["session"] = redact_session_data(
        _session_payload_with_full_messages(current, tool_calls=[])
    )
    error_payload["session_id"] = current.session_id
    return error_payload


def _clear_gateway_pending_state(
    session_id: str,
    stream_id: str,
    *,
    session=None,
) -> bool:
    def clear(current):
        current.active_stream_id = None
        current.pending_user_message = None
        current.pending_attachments = None
        current.pending_started_at = None
        current.pending_user_source = None

    return commit_stream_writeback(
        session_id,
        expected_stream_id=stream_id,
        mutate=clear,
        session=session,
    ) is not None


def _settle_gateway_cancel(
    session_id,
    stream_id,
    *,
    session=None,
    message="Cancelled by gateway",
):
    from api.streaming import (
        _materialize_pending_user_turn_before_error,
        _persist_cancelled_turn,
        _snapshot_and_append_partial_on_error,
    )

    def settle(current):
        _materialize_pending_user_turn_before_error(current)
        try:
            _snapshot_and_append_partial_on_error(current, stream_id)
        except Exception:
            logger.debug("Failed to snapshot gateway partials on cancellation", exc_info=True)
        _persist_cancelled_turn(current, message=message)

    return commit_stream_writeback(
        session_id,
        expected_stream_id=stream_id,
        mutate=settle,
        session=session,
    )


def _cleanup_gateway_pending_mirror(session_id: str) -> None:
    try:
        from api.route_approvals import (
            _approval_sse_notify_locked,
            _lock as _approval_lock,
            reconcile_gateway_pending_mirror_locked,
        )

        with _approval_lock:
            head, total, _ = reconcile_gateway_pending_mirror_locked(session_id)
            _approval_sse_notify_locked(session_id, head, total)
    except Exception:
        logger.debug("Failed to reconcile gateway pending mirror during teardown", exc_info=True)


def _run_gateway_chat_streaming(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    model_provider=None,
    goal_related=False,
):
    """Bridge a WebUI chat turn through Hermes Gateway's API server.

    This default-off path keeps the browser contract unchanged: /api/chat/start
    still returns a local stream_id and /api/chat/stream still receives WebUI SSE
    event names. The worker translates OpenAI-compatible streaming chunks from
    the configured Gateway API server into those local events and persists the
    final user/assistant turn back into the WebUI session.
    """
    execution = TurnExecution.start(
        stream_id=stream_id,
        session_id=session_id,
        phase="gateway-starting",
        logger=logger,
        log_label="gateway run",
        workspace=str(workspace),
        model=model,
        provider=model_provider,
        backend="gateway",
        record_worker_started=True,
    )
    if execution is None:
        return
    cancel_event = execution.cancel_event
    event_sink = execution.event_sink

    success_writeback_committed = False

    def put_gateway_event(event, data):
        if cancel_event.is_set() and not success_writeback_committed and event not in ("cancel", "error", "apperror"):
            return
        if event == "apperror" and isinstance(data, dict):
            data = data.copy()
            data.setdefault("session_id", session_id)
        event_sink.publish(event, data)

    s = None
    final_text = ""
    terminal_error = ""
    preserve_pending_after_settlement_failure = False
    usage = {"input_tokens": 0, "output_tokens": 0, "estimated_cost": 0}

    def settle_and_publish_error(terminal_message, *, event_payload=None):
        nonlocal preserve_pending_after_settlement_failure
        try:
            settled = _settle_gateway_terminal_error(
                session_id,
                stream_id,
                workspace,
                model,
                model_provider,
                terminal_message,
                event_payload=event_payload,
                session=s,
            )
        except Exception:
            preserve_pending_after_settlement_failure = True
            logger.exception("Failed to persist gateway terminal settlement")
            put_gateway_event(
                "apperror",
                {
                    "label": "Gateway session persistence failed",
                    "type": "gateway_persistence_error",
                    "message": "The gateway turn could not be saved safely.",
                    "hint": (
                        "Reload the session before retrying so recovery can "
                        "reconcile the pending turn."
                    ),
                },
            )
            return False
        if settled is None:
            return False
        put_gateway_event("apperror", settled)
        return True

    try:
        s = get_session(session_id)
        from api.config import get_config  # imported lazily to avoid config-cycle churn

        cfg = get_config()
        reasoning_effort = _gateway_reasoning_effort_for_request(
            cfg,
            model=model,
            model_provider=model_provider,
        )
        try:
            from api.streaming import (
                _load_webui_prefill_context,
                _prefill_messages_with_webui_context,
                _normalize_prefill_messages_before_user_turn,
                _public_prefill_context_status,
                _webui_ephemeral_system_prompt,
            )

            prefill_context = _load_webui_prefill_context(cfg)
            # #3324: the WebUI session/delivery context (connected platforms,
            # home channels, delivery hints, session framing) is now carried in
            # the ephemeral system prompt rather than a prefill `user` message.
            # The gateway-backed path must build the SAME system prompt so that
            # context is not silently dropped on Gateway-routed WebUI chats.
            _gateway_system_prompt = _webui_ephemeral_system_prompt(
                None,
                surface_context={
                    "source": "webui",
                    "session_id": session_id,
                    "profile": getattr(s, "profile", None),
                    "workspace": s.workspace if s is not None else str(workspace),
                },
                config_data=cfg,
            )
            prefill_messages = _prefill_messages_with_webui_context(prefill_context, cfg)
            prefill_messages = _normalize_prefill_messages_before_user_turn(prefill_messages)
            prefill_messages = [
                {"role": "system", "content": _gateway_system_prompt},
                *prefill_messages,
            ]
            put_gateway_event("context_status", {
                "session_id": session_id,
                "prefill": _public_prefill_context_status(prefill_context),
            })
        except Exception:
            logger.debug("Failed to load WebUI gateway prefill context", exc_info=True)
            prefill_messages = []
        base_url = _gateway_base_url(cfg)
        api_key = _gateway_api_key()
        try:
            from api.config import _main_model_request_overrides
            _gw_overrides = _main_model_request_overrides(
                cfg,
                effective_model=model,
                effective_provider=model_provider,
            )
        except Exception:
            _gw_overrides = {}
        # Capability gate: use runs API when gateway advertises approval support.
        _use_runs_api = _gateway_use_runs_api_enabled(cfg) and gateway_supports_approval(base_url, api_key)
        if _use_runs_api:
            body_extras = {}
            if model_provider:
                body_extras["provider"] = model_provider
            if reasoning_effort is not None:
                body_extras["reasoning_effort"] = reasoning_effort
            if _gw_overrides.get("service_tier"):
                body_extras["service_tier"] = _gw_overrides["service_tier"]
            try:
                final_text, usage = _run_gateway_runs_api_streaming(
                    session_id, msg_text, model, workspace, stream_id,
                    base_url, api_key, prefill_messages, body_extras,
                    put_gateway_event=put_gateway_event,
                    cancel_event=cancel_event,
                    attachments=attachments,
                    cfg=cfg,
                    session=s,
                )
            except Exception as exc:
                settle_and_publish_error(str(exc))
                return
            if final_text is None:
                _settle_gateway_cancel(
                    session_id,
                    stream_id,
                    session=s,
                    message="Cancelled by gateway",
                )
                return
        else:
            # Legacy gateway path: emit unsupported approval notice once per session,
            # but only when the gateway genuinely lacks approval capability.
            approval_reason = gateway_approval_unavailable_reason(base_url, api_key)
            if approval_reason is not None:
                if not hasattr(s, "_approval_notice_emitted"):
                    s._approval_notice_emitted = False
                if not s._approval_notice_emitted:
                    approval_message = "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
                    approval_type = "approval_gateway_unsupported"
                    if approval_reason == "unreachable":
                        approval_type = "approval_gateway_offline"
                        approval_message = "Gateway connection failed. Check that the connected Hermes gateway is running and reachable."
                    put_gateway_event("warning", {
                        "type": approval_type,
                        "message": approval_message,
                    })
                    s._approval_notice_emitted = True

            url = f"{base_url}/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "X-Hermes-Session-Id": session_id,
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
                # Scope Gateway long-term continuity to this WebUI conversation
                # without exposing the browser's auth cookie or CSRF material.
                headers["X-Hermes-Session-Key"] = f"webui:{session_id}"
            message_content: Any = str(msg_text or "")
            if attachments:
                try:
                    from api.streaming import _build_native_multimodal_message

                    message_content = _build_native_multimodal_message("", str(msg_text or ""), attachments, str(workspace), cfg=cfg)
                except Exception:
                    logger.debug("Failed to build gateway multimodal attachment payload", exc_info=True)
                    message_content = str(msg_text or "")
            body = {
                "model": model or "default",
                "stream": True,
                "messages": [*prefill_messages, {"role": "user", "content": message_content}],
            }
            if model_provider:
                body["provider"] = model_provider
            if reasoning_effort is not None:
                body["reasoning_effort"] = reasoning_effort
            if _gw_overrides.get("service_tier"):
                body["service_tier"] = _gw_overrides["service_tier"]
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            update_active_run(stream_id, phase="gateway-request")
            last_payload = {}
            sse_event = "message"
            with urllib.request.urlopen(req, timeout=_gateway_read_timeout_secs()) as resp:
                for raw_line in _iter_sse_lines_cancellable(resp, cancel_event):
                    if cancel_event.is_set():
                        _settle_gateway_cancel(
                            session_id,
                            stream_id,
                            session=s,
                            message="Cancelled by user",
                        )
                        put_gateway_event("cancel", {"message": "Cancelled by user"})
                        return
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
                    _payload_event = str(payload.get("event") or payload.get("type") or sse_event).strip()
                    if _payload_event in {"hermes.approval.request", "approval.request"}:
                        approval_data = _gateway_runs_approval_event(payload)
                        if approval_data:
                            # Record the gateway run_id so /api/approval/respond
                            # can relay the choice back and resume the parked run
                            # (legacy path never creates a local run; without this
                            # the card renders but approve/deny returns ok:false).
                            # No-op when the payload omits run_id.
                            _approval_run_id = str(approval_data.get("run_id") or "").strip()
                            if _approval_run_id:
                                _STREAM_RUN_IDS[stream_id] = _approval_run_id
                            put_gateway_event("approval", approval_data)
                            try:
                                from api.route_approvals import submit_gateway_pending_mirror
                                submit_gateway_pending_mirror(session_id, approval_data)
                            except Exception:
                                logger.debug("submit_gateway_pending_mirror failed", exc_info=True)
                        else:
                            logger.debug("Ignoring malformed gateway approval payload")
                        sse_event = "message"
                        continue
                    if sse_event == "hermes.tool.progress":
                        translated = _gateway_tool_progress_event(payload)
                        if translated:
                            event_name, event_payload = translated
                            if event_name == "reasoning":
                                reason_delta = event_payload.get("text")
                                if reason_delta:
                                    append_runtime_reasoning_text(stream_id, reason_delta)
                            else:
                                if event_name == "tool":
                                    start_runtime_tool_call(
                                        stream_id,
                                        name=event_payload.get("name"),
                                        args=event_payload.get("args") or {},
                                        tool_call_id=event_payload.get("tid"),
                                    )
                                else:
                                    finish_runtime_tool_call(
                                        stream_id,
                                        name=event_payload.get("name"),
                                        tool_call_id=event_payload.get("tid"),
                                        is_error=bool(event_payload.get("is_error")),
                                    )
                            put_gateway_event(event_name, event_payload)
                            if event_name != "reasoning":
                                update_active_run(stream_id, phase="gateway-tool", latest_tool=event_payload.get("name"))
                        sse_event = "message"
                        continue
                    if sse_event == "reasoning.available":
                        reason_delta = _gateway_reasoning_delta(payload)
                        if reason_delta:
                            append_runtime_reasoning_text(stream_id, reason_delta)
                            put_gateway_event("reasoning", {"text": reason_delta})
                        sse_event = "message"
                        continue
                    last_payload = payload
                    if payload.get("error"):
                        terminal_error = str(payload["error"])
                    reasoning_delta = _gateway_sse_reasoning_delta(payload)
                    if reasoning_delta:
                        append_runtime_reasoning_text(stream_id, reasoning_delta)
                        put_gateway_event("reasoning", {"text": reasoning_delta})
                    delta = _gateway_sse_delta(payload)
                    if delta:
                        final_text += delta
                        append_runtime_partial_text(stream_id, delta)
                        put_gateway_event("token", {"text": delta})
                    usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
            usage.update({k: v for k, v in _gateway_stream_usage(last_payload).items() if v})
        assistant_text = final_text.strip()
        if terminal_error:
            settle_and_publish_error(terminal_error)
            return
        if not assistant_text:
            settle_and_publish_error(
                "Gateway returned no assistant message for this turn.",
                event_payload={
                    "label": "Gateway returned no response",
                    "type": "gateway_empty_response",
                    "message": "Gateway returned no assistant message for this turn.",
                    "hint": (
                        "Check that Hermes Gateway API server is running and reachable."
                    ),
                },
            )
            return
        writeback_state = {"cancelled": False}

        def _restore_cancelled_success_writeback(current):
            pending_source = writeback_state["pending_source"]
            previous_pause = writeback_state["previous_process_wakeup_pause"]
            if pending_source == "process_wakeup":
                current.context_messages = writeback_state["previous_context"]
                current.messages = writeback_state["previous_messages"]
                current.process_wakeup_pause = dict(previous_pause)
            elif previous_pause:
                current.process_wakeup_pause = dict(previous_pause)
            else:
                clear_process_wakeup_pause(current, reason="run_completed")

        def _settle_success(current):
            nonlocal s
            s = current
            # A late Stop can land after Gateway has yielded a full answer but
            # before success writeback. Persist an honest cancelled terminal
            # state rather than letting final cleanup discard the pending turn.
            if cancel_event.is_set():
                from api.streaming import (
                    _materialize_pending_user_turn_before_error,
                    _persist_cancelled_turn,
                    _snapshot_and_append_partial_on_error,
                )

                _materialize_pending_user_turn_before_error(current)
                try:
                    _snapshot_and_append_partial_on_error(current, stream_id)
                except Exception:
                    logger.debug(
                        "Failed to snapshot gateway partials before success cancellation",
                        exc_info=True,
                    )
                _persist_cancelled_turn(current, message="Cancelled by user")
                writeback_state["cancelled"] = True
                return
            now = time.time()
            # Preserve subsecond ordering for gateway-backed turns. Using an
            # integer seconds timestamp gives the user and assistant rows the
            # same sort key; later transcript merges can then fall back to
            # role/content ordering instead of turn order.
            assistant_ts = now + 0.000001
            user_msg = {"role": "user", "content": str(msg_text or ""), "timestamp": now}
            pending_source = getattr(current, "pending_user_source", None) or "webui"
            if pending_source != "webui":
                user_msg["_source"] = pending_source
            if attachments:
                user_msg["attachments"] = list(attachments)
            assistant_msg = {"role": "assistant", "content": assistant_text, "timestamp": assistant_ts}
            saved_reasoning = runtime_progress_snapshot(stream_id).reasoning_text
            if saved_reasoning:
                assistant_msg["reasoning"] = saved_reasoning
            previous_messages = list(getattr(current, "messages", None) or [])
            previous_context = list(
                getattr(current, "context_messages", None)
                or getattr(current, "messages", None)
                or []
            )
            previous_process_wakeup_pause = dict(
                getattr(current, "process_wakeup_pause", {}) or {}
            )
            writeback_state.update(
                pending_source=pending_source,
                previous_messages=previous_messages,
                previous_context=previous_context,
                previous_process_wakeup_pause=previous_process_wakeup_pause,
            )
            # Stamp stable ids on the two new rows (shared with the display merge
            # below) so display and model-context copies share an id for the
            # fork/truncate aligner (#context-message-stable-id).
            try:
                from api.streaming import _assign_stable_message_ids

                _assign_stable_message_ids(
                    [user_msg, assistant_msg],
                    previous_context,
                    list(getattr(current, "messages", None) or []),
                )
            except Exception:
                logger.debug("Failed to stamp stable ids on gateway turn rows", exc_info=True)
            current.context_messages = previous_context + [user_msg, assistant_msg]
            try:
                from api.streaming import _is_context_compression_marker

                display_context = [
                    msg
                    for msg in previous_context
                    if not _is_context_compression_marker(msg)
                ]
            except Exception:
                logger.debug("Failed to filter gateway display context markers", exc_info=True)
                display_context = previous_context
            display = merge_session_messages_append_only(
                previous_messages,
                display_context,
            )
            try:
                from api.streaming import _merge_display_messages_after_agent_result

                current.messages = _merge_display_messages_after_agent_result(
                    display,
                    previous_context,
                    current.context_messages,
                    str(msg_text or ""),
                    source=pending_source,
                )
            except Exception:
                logger.debug("Failed to merge gateway display transcript", exc_info=True)
                # Avoid duplicating the eager-save checkpointed user message.
                if display:
                    latest = display[-1]
                    if isinstance(latest, dict) and latest.get("role") == "user":
                        latest_text = " ".join(str(latest.get("content") or "").split())
                        msg_norm = " ".join(str(msg_text or "").split())
                        if latest_text == msg_norm:
                            display = display[:-1]
                current.messages = display + [user_msg, assistant_msg]
            current.active_stream_id = None
            current.pending_user_message = None
            current.pending_attachments = None
            current.pending_started_at = None
            current.pending_user_source = None
            current.workspace = str(workspace)
            current.model = model
            current.model_provider = model_provider

            # Recheck immediately before clearing the pause; Stop can arrive
            # while the success transcript is being assembled.
            if cancel_event.is_set():
                _restore_cancelled_success_writeback(current)
                writeback_state["cancelled"] = True
                return
            clear_process_wakeup_pause(current, reason="run_completed")
            if cancel_event.is_set():
                _restore_cancelled_success_writeback(current)
                writeback_state["cancelled"] = True
                return

        def _reconcile_success_after_save(current):
            nonlocal success_writeback_committed
            if writeback_state["cancelled"]:
                return False
            if cancel_event.is_set():
                _restore_cancelled_success_writeback(current)
                writeback_state["cancelled"] = True
                return True
            success_writeback_committed = True
            return False

        writeback_result = commit_stream_writeback(
            session_id,
            expected_stream_id=stream_id,
            mutate=_settle_success,
            reconcile_after_save=_reconcile_success_after_save,
            session=s,
        )
        if writeback_result is None:
            return
        s = writeback_result.session
        if writeback_result.reconciliation_error is not None:
            # The first success checkpoint is durable. A failed late-cancel
            # reconciliation must report that durable success, not fabricate a
            # cancellation that did not persist.
            writeback_state["cancelled"] = False
            success_writeback_committed = True
        if writeback_state["cancelled"]:
            put_gateway_event("cancel", {"message": "Cancelled by user"})
            return
        success_writeback_committed = True
        try:
            from api.goals import evaluate_goal_after_turn, has_active_goal
            from api.profiles import get_hermes_home_for_profile

            profile_home = get_hermes_home_for_profile(getattr(s, "profile", None))
            if goal_related and has_active_goal(session_id, profile_home=profile_home):
                put_gateway_event("goal", {
                    "session_id": session_id,
                    "state": "evaluating",
                    "message": "Evaluating goal progress…",
                    "message_key": "goal_evaluating_progress",
                })
                decision = evaluate_goal_after_turn(
                    session_id,
                    assistant_text,
                    user_initiated=True,
                    profile_home=profile_home,
                ) or {}
                goal_message = str(decision.get("message") or "").strip()
                if goal_message:
                    put_gateway_event("goal", {
                        "session_id": session_id,
                        "state": "continuing" if decision.get("should_continue") else "idle",
                        "message": goal_message,
                        "message_key": decision.get("message_key") or (
                            "goal_continuing" if goal_message else ""
                        ),
                        "message_args": decision.get("message_args") or [],
                        "decision": decision,
                    })
                if decision.get("should_continue"):
                    continuation_prompt = str(decision.get("continuation_prompt") or "").strip()
                    if continuation_prompt:
                        PENDING_GOAL_CONTINUATION.add(session_id)
                        put_gateway_event("goal_continue", {
                            "session_id": session_id,
                            "continuation_prompt": continuation_prompt,
                            "text": continuation_prompt,
                            "message": goal_message,
                            "message_key": decision.get("message_key") or "goal_continuing",
                            "message_args": decision.get("message_args") or [],
                            "decision": decision,
                        })
        except Exception as goal_exc:
            logger.debug(
                "Gateway goal continuation hook failed for session %s: %s",
                session_id,
                goal_exc,
            )
        from api.streaming import _session_payload_with_full_messages
        gateway_session_payload = _session_payload_with_full_messages(s, tool_calls=[])
        put_gateway_event("done", {"session": redact_session_data(gateway_session_payload), "usage": usage})
        put_gateway_event("stream_end", {"session_id": session_id})
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        http_error_payload = _gateway_http_error_event(
            exc,
            err_body,
            api_key_configured=bool(_gateway_api_key()),
        )
        settle_and_publish_error(
            str(http_error_payload.get("message") or exc),
            event_payload=http_error_payload,
        )
    except Exception as exc:
        safe = _redact_text(str(exc))[:500]
        settle_and_publish_error(
            safe or "Gateway request failed.",
            event_payload={
                "label": "Gateway request failed",
                "type": "gateway_error",
                "message": safe or "Gateway request failed.",
                "hint": "Check HERMES_WEBUI_GATEWAY_BASE_URL and Gateway API server health.",
            },
        )
    finally:
        if s is not None and not preserve_pending_after_settlement_failure:
            try:
                _clear_gateway_pending_state(session_id, stream_id, session=s)
            except Exception:
                logger.debug("Failed to clear gateway stream state", exc_info=True)
            _cleanup_gateway_pending_mirror(session_id)
        try:
            execution.finish()
        finally:
            _STREAM_RUN_IDS.pop(stream_id, None)
