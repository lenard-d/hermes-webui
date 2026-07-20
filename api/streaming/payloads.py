"""Session terminal payloads and compact visible-text comparison helpers."""

from __future__ import annotations

import re

import logging

from api.helpers import redact_session_data
from api.todo_state import attach_todo_state


logger = logging.getLogger(__name__)


def _session_payload_with_full_messages(
    session,
    *,
    tool_calls=None,
):
    """Return compact session metadata plus the embedded full transcript."""
    messages = list(getattr(session, "messages", None) or [])
    raw = session.compact() | {
        "messages": messages,
        "message_count": len(messages),
    }
    attach_todo_state(raw, messages)
    if tool_calls is not None:
        raw["tool_calls"] = tool_calls
    return raw


def _compact_for_echo_compare(value: str) -> str:
    """Normalize visible stream text for duplicate echo detection."""
    return re.sub(r"\s+", "", str(value or ""))


def _strip_compact_echo_suffix(
    value: str,
    suffix: str,
    *,
    search_window: int = 4096,
) -> tuple[str, bool]:
    """Remove ``suffix`` from ``value`` after whitespace folding."""
    raw = str(value or "")
    candidate = _compact_for_echo_compare(suffix)
    if not raw or not candidate:
        return raw, False
    tail = raw[-max(len(str(suffix or "")) * 3, search_window):]
    offset = len(raw) - len(tail)
    for idx in range(len(tail) + 1):
        if _compact_for_echo_compare(tail[idx:]) == candidate:
            return raw[: offset + idx].rstrip(), True
    return raw, False


def _redacted_session_payload_with_full_messages(
    session,
    *,
    tool_calls=None,
) -> dict | None:
    """Build a best-effort redacted payload from persisted session state."""
    try:
        return redact_session_data(
            _session_payload_with_full_messages(
                session,
                tool_calls=tool_calls,
            )
        )
    except Exception:
        logger.debug(
            "Failed to build redacted session payload",
            exc_info=True,
        )
        return None


def _cancel_event_payload(
    message: str = "Cancelled by user",
    *,
    session: dict | None = None,
) -> dict:
    """Return base cancel terminal event metadata."""
    payload = {
        "message": message,
        "type": "cancelled",
        "status": "cancelled",
    }
    if session:
        payload["session"] = session
        payload["session_id"] = session.get("session_id")
    return payload
