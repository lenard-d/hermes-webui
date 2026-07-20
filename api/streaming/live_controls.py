"""Active-run steering and cancellation transitions.

The public compatibility interface remains in :mod:`streaming`.  This
module owns the implementation so active-run control decisions, progress
recovery, terminal persistence, and transport notification stay together.
"""

from __future__ import annotations

import logging
import time

from api.sessions import edit_session, get_session

from api.runs.agent_cache import (
    _cached_agent_matches_session,
    _cached_agent_session_identity,
    _close_cached_agent_entry_at_session_boundary,
)
from api.runs.payloads import _cancel_event_payload, _redacted_session_payload_with_full_messages
from api.runs.terminal_copy import _preferred_agent_display_name_for_session
from api.runs.terminal_outcomes import _cancelled_turn_content, _session_has_cancel_marker
from api.runs.tool_events import _partial_marker_already_present
from api.runs.transcript import _build_partial_message
from api.runs.turn_context import _stream_writeback_is_current


logger = logging.getLogger(__name__)
_CANCEL_MARKER_PATTERNS = ("task cancelled", "task canceled", "response interrupted")


def _handle_chat_steer(handler, body: dict) -> bool:
    """Inject a /steer payload into the active agent for a session."""
    from api.helpers import j, bad
    from api import config as _cfg

    sid = str((body or {}).get("session_id", "") or "").strip()
    text = str((body or {}).get("text", "") or "").strip()
    if not sid:
        return bad(handler, "session_id required")
    if not text:
        return bad(handler, "text required")

    evicted_cached_entry = None
    with _cfg.SESSION_AGENT_CACHE_LOCK:
        cached = _cfg.SESSION_AGENT_CACHE.get(sid)
        if cached:
            agent = cached[0]
            if not _cached_agent_matches_session(agent, sid):
                evicted_cached_entry = _cfg.SESSION_AGENT_CACHE.pop(sid, None)
                logger.warning(
                    '[webui] Evicted cached agent before steer due to mismatched session identity: cache_key=%s agent_session_id=%s',
                    sid,
                    _cached_agent_session_identity(agent),
                )
                cached = None
    if evicted_cached_entry is not None:
        try:
            _close_cached_agent_entry_at_session_boundary(sid, evicted_cached_entry)
        except Exception:
            logger.debug(
                "Failed to close steer identity-mismatched cached agent for session %s",
                sid,
                exc_info=True,
            )
    if not cached:
        try:
            session = get_session(sid)
            active_stream_id = getattr(session, "active_stream_id", None) or None
        except KeyError:
            active_stream_id = None
        if active_stream_id:
            with _cfg.STREAMS_LOCK:
                stream_alive = active_stream_id in _cfg.STREAMS
            if stream_alive:
                try:
                    with _cfg.ACTIVE_RUNS_LOCK:
                        active_run = dict((_cfg.ACTIVE_RUNS or {}).get(str(active_stream_id)) or {})
                    if active_run.get("backend") == "gateway":
                        return j(
                            handler,
                            {
                                "accepted": False,
                                "fallback": "gateway_steer_queued",
                                "stream_id": active_stream_id,
                            },
                        )
                except Exception:
                    logger.warning(
                        "Gateway ownership lookup failed before steer fallback for session=%s stream_id=%s",
                        sid,
                        active_stream_id,
                        exc_info=True,
                    )
        return j(
            handler,
            {"accepted": False, "fallback": "no_cached_agent", "stream_id": None},
        )
    agent = cached[0]
    if not hasattr(agent, "steer"):
        return j(
            handler,
            {"accepted": False, "fallback": "agent_lacks_steer", "stream_id": None},
        )

    try:
        session = get_session(sid)
    except KeyError:
        return j(
            handler,
            {"accepted": False, "fallback": "session_not_found", "stream_id": None},
        )
    active_stream_id = getattr(session, "active_stream_id", None) or None
    if not active_stream_id:
        return j(
            handler,
            {"accepted": False, "fallback": "not_running", "stream_id": None},
        )
    with _cfg.STREAMS_LOCK:
        stream_alive = active_stream_id in _cfg.STREAMS
    if not stream_alive:
        return j(
            handler,
            {"accepted": False, "fallback": "stream_dead", "stream_id": None},
        )

    try:
        accepted = bool(agent.steer(text))
    except Exception as exc:
        logger.debug("agent.steer() raised for session=%s: %s", sid, exc)
        return j(
            handler,
            {"accepted": False, "fallback": "steer_error", "stream_id": active_stream_id},
        )

    return j(
        handler,
        {"accepted": accepted, "fallback": None, "stream_id": active_stream_id},
    )


def cancel_stream(stream_id: str) -> bool:
    """Cancel a run while preserving its recoverable and terminal state."""
    from api import config as _live_config

    cancellation = _live_config.begin_runtime_cancel(stream_id)
    if cancellation is None:
        return False

    stream_present = cancellation.had_transport
    active_run_session_id = cancellation.session_id
    agent = cancellation.agent
    channel = cancellation.channel
    cancel_partial_text = cancellation.partial_text
    cancel_reasoning = cancellation.reasoning_text
    cancel_tool_calls = list(cancellation.live_tool_calls)
    cancel_session_payload = None

    if agent is None and active_run_session_id:
        try:
            with _live_config.SESSION_AGENT_CACHE_LOCK:
                cached = _live_config.SESSION_AGENT_CACHE.get(active_run_session_id)
            if cached and _cached_agent_matches_session(cached[0], active_run_session_id):
                agent = cached[0]
        except Exception:
            pass
    if agent:
        try:
            agent.interrupt("Cancelled by user")
        except Exception as exc:
            logger.debug("Failed to interrupt agent for stream %s: %s", stream_id, exc)
    elif stream_present:
        logger.debug(
            "Cancel requested for stream %s before agent ready - cancel_event flag set, will be checked on agent startup",
            stream_id,
        )

    try:
        from api.clarify import clear_pending as clear_clarify_pending

        clarify_session_id = getattr(agent, "session_id", None) if agent else active_run_session_id
        if clarify_session_id:
            clear_clarify_pending(clarify_session_id)
    except Exception:
        logger.debug("Failed to clear clarify prompt during cancel")

    emit_cancel_event = True
    cancel_session_id = getattr(agent, "session_id", None) if agent else None
    if not cancel_session_id and active_run_session_id:
        cancel_session_id = active_run_session_id

    # Session cleanup stays outside STREAMS_LOCK and the other process-runtime
    # locks acquired by begin_runtime_cancel(). The repository edit owner may
    # wait on a streaming worker that acquired session ownership before touching
    # runtime state; holding a runtime lock here would invert that order and can
    # deadlock. It also reloads the authoritative full session so cancellation
    # serializes with checkpoint, retry, undo, and endpoint writers.
    if cancel_session_id:
        cancel_persisted = False
        try:
            with edit_session(
                cancel_session_id,
                save_when=lambda _current: cancel_persisted,
            ) as current_session:
                if not isinstance(getattr(current_session, "messages", None), list):
                    current_session.messages = []
                if not _stream_writeback_is_current(current_session, stream_id):
                    logger.info(
                        "Skipping stale cancel writeback for session %s stream %s; active_stream_id=%s",
                        cancel_session_id,
                        stream_id,
                        getattr(current_session, "active_stream_id", None),
                    )
                    emit_cancel_event = False
                    return True

                try:
                    pending_user = getattr(current_session, "pending_user_message", None)
                    pending_source = getattr(current_session, "pending_user_source", None)
                    pending_atts_raw = getattr(current_session, "pending_attachments", None)
                    pending_atts = (
                        list(pending_atts_raw)
                        if isinstance(pending_atts_raw, (list, tuple))
                        else []
                    )
                    pending_started = getattr(current_session, "pending_started_at", None) or 0
                    messages_for_recovery = (
                        current_session.messages
                        if isinstance(current_session.messages, list)
                        else None
                    )
                    if pending_user and messages_for_recovery is not None:
                        last_user = None
                        for message in reversed(messages_for_recovery):
                            if isinstance(message, dict) and message.get("role") == "user":
                                last_user = message
                                break
                        already_persisted = False
                        if last_user is not None:
                            last_content = last_user.get("content")
                            last_timestamp = last_user.get("timestamp") or 0
                            if isinstance(last_content, str) and last_timestamp >= pending_started:
                                if pending_user == last_content or pending_user in last_content:
                                    already_persisted = True
                        if not already_persisted:
                            recovered_timestamp = int(time.time())
                            if isinstance(pending_started, (int, float)) and pending_started > 0:
                                recovered_timestamp = int(pending_started)
                            user_turn: dict = {
                                "role": "user",
                                "content": pending_user,
                                "timestamp": recovered_timestamp,
                            }
                            if pending_source and pending_source != "webui":
                                user_turn["_source"] = pending_source
                            if pending_atts:
                                user_turn["attachments"] = pending_atts
                            messages_for_recovery.append(user_turn)
                except Exception:
                    logger.debug(
                        "Failed to recover pending user message on cancel for %s",
                        cancel_session_id,
                    )

                current_session.active_stream_id = None
                current_session.pending_user_message = None
                current_session.pending_attachments = []
                current_session.pending_started_at = None
                current_session.pending_user_source = None

                partial_message = _build_partial_message(
                    cancel_partial_text,
                    cancel_reasoning,
                    cancel_tool_calls,
                )
                cancel_marker_exists = _session_has_cancel_marker(current_session)
                cancel_marker_index = len(current_session.messages)
                if cancel_marker_exists:
                    for index in range(len(current_session.messages) - 1, -1, -1):
                        message = current_session.messages[index]
                        if not isinstance(message, dict) or message.get("role") != "assistant":
                            continue
                        content = str(message.get("content") or "").strip().lower()
                        if any(pattern in content for pattern in _CANCEL_MARKER_PATTERNS):
                            cancel_marker_index = index
                            break
                if partial_message is not None and not _partial_marker_already_present(
                    current_session.messages,
                    partial_message,
                    before_idx=cancel_marker_index,
                ):
                    current_session.messages.insert(cancel_marker_index, partial_message)
                if not cancel_marker_exists:
                    current_session.messages.append(
                        {
                            "role": "assistant",
                            "content": _cancelled_turn_content(
                                "Task cancelled.",
                                _preferred_agent_display_name_for_session(current_session),
                            ),
                            "_error": True,
                            "provider_details": "Task cancelled.",
                            "provider_details_label": "Cancellation details",
                            "timestamp": int(time.time()),
                        }
                    )
                cancel_persisted = True
            if cancel_persisted:
                cancel_session_payload = _redacted_session_payload_with_full_messages(current_session)
        except Exception:
            logger.debug("Failed to clear session state on cancel for %s", cancel_session_id)

    if emit_cancel_event and channel:
        cancel_event_id = cancellation.last_event_id
        if cancel_event_id and hasattr(channel, "note_last_event_id"):
            try:
                channel.note_last_event_id(cancel_event_id)
            except Exception:
                logger.debug(
                    "Failed to note cancel event_id %s for stream %s",
                    cancel_event_id,
                    stream_id,
                    exc_info=True,
                )
        try:
            payload = _cancel_event_payload(
                "Cancelled by user",
                session=cancel_session_payload,
            )
            channel.put_nowait(("cancel", payload))
        except Exception:
            logger.debug("Failed to put cancel event to queue")

    return True
