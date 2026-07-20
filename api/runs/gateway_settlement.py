"""Durable terminal settlement for Gateway-backed WebUI turns."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from api.helpers import redact_session_data
from api.sessions import (
    clear_process_wakeup_pause,
    commit_stream_writeback,
    merge_session_messages_append_only,
)

from .compression_anchors import _is_context_compression_marker
from .message_sanitization import _assign_stable_message_ids
from .payloads import _session_payload_with_full_messages
from .provider_errors import _classify_provider_error, _provider_error_payload
from .runtime_state import runtime_progress_snapshot
from .terminal_outcomes import _persist_cancelled_turn
from .transcript import (
    _materialize_pending_user_turn_before_error,
    _merge_display_messages_after_agent_result,
    _snapshot_and_append_partial_on_error,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewaySuccessSettlement:
    """Outcome of the durable success/cancellation reconciliation."""

    session: object
    cancelled: bool
    success_committed: bool


def settle_gateway_terminal_error(
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
    """Persist a safe terminal error and return its browser event payload."""
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
            "label": str(
                error_payload.get("label") or "Gateway request failed"
            ),
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
            logger.debug(
                "Failed to snapshot Gateway partials on terminal error",
                exc_info=True,
            )
        error_message = {
            "role": "assistant",
            "content": (
                f"**{error_classification['label']}:** "
                f"{error_payload.get('message') or error_classification['label']}"
            )
            + (
                f"\n\n*{error_payload['hint']}*"
                if error_payload.get("hint")
                else ""
            ),
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


def clear_gateway_pending_state(
    session_id: str,
    stream_id: str,
    *,
    session=None,
) -> bool:
    """Clear pending fields only while this stream still owns the session."""

    def clear(current):
        current.active_stream_id = None
        current.pending_user_message = None
        current.pending_attachments = None
        current.pending_started_at = None
        current.pending_user_source = None

    return (
        commit_stream_writeback(
            session_id,
            expected_stream_id=stream_id,
            mutate=clear,
            session=session,
        )
        is not None
    )


def settle_gateway_cancel(
    session_id,
    stream_id,
    *,
    session=None,
    message="Cancelled by gateway",
):
    """Persist one owner-checked cancelled terminal turn."""

    def settle(current):
        _materialize_pending_user_turn_before_error(current)
        try:
            _snapshot_and_append_partial_on_error(current, stream_id)
        except Exception:
            logger.debug(
                "Failed to snapshot Gateway partials on cancellation",
                exc_info=True,
            )
        _persist_cancelled_turn(current, message=message)

    return commit_stream_writeback(
        session_id,
        expected_stream_id=stream_id,
        mutate=settle,
        session=session,
    )


def cleanup_gateway_pending_mirror(session_id: str) -> None:
    """Reconcile the mirrored approval queue after Gateway teardown."""
    try:
        from api.route_approvals import (
            _approval_sse_notify_locked,
            _lock as approval_lock,
            reconcile_gateway_pending_mirror_locked,
        )

        with approval_lock:
            head, total, _ = reconcile_gateway_pending_mirror_locked(session_id)
            _approval_sse_notify_locked(session_id, head, total)
    except Exception:
        logger.debug(
            "Failed to reconcile Gateway pending mirror during teardown",
            exc_info=True,
        )


def settle_gateway_success(
    *,
    session_id,
    stream_id,
    session,
    msg_text,
    assistant_text,
    workspace,
    model,
    model_provider,
    attachments,
    cancel_event,
) -> GatewaySuccessSettlement | None:
    """Persist success while reconciling a Stop racing with writeback.

    The first save and the late-cancel reconciliation stay under the session
    repository's owner check.  If the reconciliation save fails after success
    is already durable, the returned result reports that durable success instead
    of fabricating a cancellation that never persisted.
    """
    writeback_state = {"cancelled": False}

    def restore_cancelled_success_writeback(current):
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

    def settle_success(current):
        if cancel_event.is_set():
            _materialize_pending_user_turn_before_error(current)
            try:
                _snapshot_and_append_partial_on_error(current, stream_id)
            except Exception:
                logger.debug(
                    "Failed to snapshot Gateway partials before cancellation",
                    exc_info=True,
                )
            _persist_cancelled_turn(current, message="Cancelled by user")
            writeback_state["cancelled"] = True
            return

        now = time.time()
        user_msg = {
            "role": "user",
            "content": str(msg_text or ""),
            "timestamp": now,
        }
        pending_source = getattr(current, "pending_user_source", None) or "webui"
        if pending_source != "webui":
            user_msg["_source"] = pending_source
        if attachments:
            user_msg["attachments"] = list(attachments)
        assistant_msg = {
            "role": "assistant",
            "content": assistant_text,
            "timestamp": now + 0.000001,
        }
        saved_reasoning = runtime_progress_snapshot(stream_id).reasoning_text
        if saved_reasoning:
            assistant_msg["reasoning"] = saved_reasoning

        previous_messages = list(getattr(current, "messages", None) or [])
        previous_context = list(
            getattr(current, "context_messages", None)
            or getattr(current, "messages", None)
            or []
        )
        previous_pause = dict(
            getattr(current, "process_wakeup_pause", {}) or {}
        )
        writeback_state.update(
            pending_source=pending_source,
            previous_messages=previous_messages,
            previous_context=previous_context,
            previous_process_wakeup_pause=previous_pause,
        )
        try:
            _assign_stable_message_ids(
                [user_msg, assistant_msg],
                previous_context,
                list(getattr(current, "messages", None) or []),
            )
        except Exception:
            logger.debug(
                "Failed to stamp stable ids on Gateway turn rows",
                exc_info=True,
            )
        current.context_messages = previous_context + [user_msg, assistant_msg]
        try:
            display_context = [
                message
                for message in previous_context
                if not _is_context_compression_marker(message)
            ]
        except Exception:
            logger.debug(
                "Failed to filter Gateway display context markers",
                exc_info=True,
            )
            display_context = previous_context
        display = merge_session_messages_append_only(
            previous_messages,
            display_context,
        )
        try:
            current.messages = _merge_display_messages_after_agent_result(
                display,
                previous_context,
                current.context_messages,
                str(msg_text or ""),
                source=pending_source,
            )
        except Exception:
            logger.debug(
                "Failed to merge Gateway display transcript",
                exc_info=True,
            )
            if display:
                latest = display[-1]
                if isinstance(latest, dict) and latest.get("role") == "user":
                    latest_text = " ".join(
                        str(latest.get("content") or "").split()
                    )
                    message_text = " ".join(str(msg_text or "").split())
                    if latest_text == message_text:
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

        if cancel_event.is_set():
            restore_cancelled_success_writeback(current)
            writeback_state["cancelled"] = True
            return
        clear_process_wakeup_pause(current, reason="run_completed")
        if cancel_event.is_set():
            restore_cancelled_success_writeback(current)
            writeback_state["cancelled"] = True

    success_writeback_committed = False

    def reconcile_success_after_save(current):
        nonlocal success_writeback_committed
        if writeback_state["cancelled"]:
            return False
        if cancel_event.is_set():
            restore_cancelled_success_writeback(current)
            writeback_state["cancelled"] = True
            return True
        success_writeback_committed = True
        return False

    writeback_result = commit_stream_writeback(
        session_id,
        expected_stream_id=stream_id,
        mutate=settle_success,
        reconcile_after_save=reconcile_success_after_save,
        session=session,
    )
    if writeback_result is None:
        return None
    if writeback_result.reconciliation_error is not None:
        writeback_state["cancelled"] = False
        success_writeback_committed = True
    if not writeback_state["cancelled"]:
        success_writeback_committed = True
    return GatewaySuccessSettlement(
        session=writeback_result.session,
        cancelled=bool(writeback_state["cancelled"]),
        success_committed=success_writeback_committed,
    )
