"""Background-completion payload, fan-out, coalescing, and dedup ownership.

This module owns the complete browser-observation transition for a process
completion: normalize the agent event, create the stable public payload, fan it
out without cross-session leakage, coalesce bursts, and mark the upstream
registry dedup key.  The facade remains the late-bound compatibility seam so
existing integrations and tests can replace collaborators without duplicating
state.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from api.background_process_parts.bindings import background_process_api
# Preserve the established operational category during the facade split.
logger = logging.getLogger("api.background_process")


EMIT_COALESCE_WINDOW_SECS = 1.0
EMIT_COALESCE_LOCK = threading.Lock()
LAST_EMIT_TS: dict[str, float] = {}
PENDING_EMIT_PAYLOADS: dict[str, dict] = {}
PENDING_EMIT_TIMERS: dict[str, threading.Timer] = {}


def truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    value = str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + "\n…(truncated)"


def format_wakeup_prompt(evt: object) -> str | None:
    """Build the synthetic ``[IMPORTANT: ...]`` message the agent will see."""
    if not isinstance(evt, dict) or not evt:
        return None

    api = background_process_api()
    evt_type = evt.get("type", "completion")
    process_id = str(evt.get("session_id") or "").strip()
    command = str(evt.get("command") or "").strip()
    if evt_type in {"watch_overflow_tripped", "watch_overflow_released"}:
        message = str(evt.get("message") or "").strip()
        return f"[IMPORTANT: {message}]" if message else None
    if evt_type == "watch_disabled":
        message = str(evt.get("message") or "").strip()
        return f"[IMPORTANT: {message}]" if message else None
    if evt_type == "watch_match":
        pattern = evt.get("pattern", "?")
        output = api._truncate(evt.get("output", ""), 4000)
        suppressed = evt.get("suppressed", 0)
        body = (
            f'[IMPORTANT: Background process {process_id} matched watch pattern "{pattern}".\n'
            f"Command: {command}\n"
            f"Matched output:\n{output}"
        )
        if suppressed:
            body += f"\n({suppressed} earlier matches were suppressed by rate limit)"
        return body + "]"
    if evt_type == "async_delegation":
        try:
            from tools.process_registry import format_process_notification

            result = format_process_notification(evt)
            if result:
                return result
        except Exception:
            logger.debug(
                "agent-side format_process_notification fallback failed for "
                "evt_type=%s",
                evt_type,
                exc_info=True,
            )
        return None
    if evt_type != "completion":
        return None
    if not (
        process_id
        or command
        or "exit_code" in evt
        or evt.get("output")
    ):
        return None

    exit_code = evt.get("exit_code", "?")
    output = api._truncate(evt.get("output", ""), 4000)
    return (
        f"[IMPORTANT: Background process {process_id} completed "
        f"(exit_code={exit_code}).\nCommand: {command}\nOutput:\n{output}]"
    )


def build_payload(evt: dict, session_id: str) -> dict:
    """Build the minimal public completion-event payload."""
    api = background_process_api()
    process_id = api.completion_delivery_id(evt)
    payload: dict[str, Any] = {
        "session_id": str(session_id),
        "task_id": process_id,
        "completed_at": api.time.time(),
        "event_id": api.uuid.uuid4().hex,
    }
    try:
        wakeup_body = api.format_wakeup_prompt(evt)
        if wakeup_body:
            first_line = next(
                (
                    line.strip().lstrip("[").rstrip("]").strip()
                    for line in wakeup_body.splitlines()
                    if line.strip()
                ),
                "",
            )
            if first_line:
                payload["summary"] = api._truncate(first_line, 200)
    except Exception:
        logger.debug("summary derivation failed", exc_info=True)
    return payload


def emit_to_session_streams(session_id: str, event: str, data: dict) -> int:
    """Push one event only to active transports owned by ``session_id``."""
    from api import config as _cfg

    api = background_process_api()
    emitted = 0
    if hasattr(_cfg, "ACTIVE_RUNS") and hasattr(_cfg, "ACTIVE_RUNS_LOCK"):
        with _cfg.ACTIVE_RUNS_LOCK:
            active_runs_snapshot: dict = dict(_cfg.ACTIVE_RUNS)
    elif hasattr(_cfg, "ACTIVE_RUNS"):
        active_runs_snapshot = dict(_cfg.ACTIVE_RUNS)
    else:
        active_runs_snapshot = {}
    with _cfg.STREAMS_LOCK:
        stream_items = list(_cfg.STREAMS.items())
    for stream_id, channel in stream_items:
        metadata = active_runs_snapshot.get(stream_id)
        owner_sid = (
            (metadata or {}).get("session_id")
            if isinstance(metadata, dict)
            else None
        )
        # Copilot review #3: owner-unknown channels are not safe fallbacks.
        if owner_sid != session_id:
            continue
        try:
            channel.put_nowait((event, data))
            emitted += 1
        except Exception:
            logger.debug(
                "process_complete emit failed for stream %s",
                stream_id,
                exc_info=True,
            )

    session_channel = api.get_session_channel(session_id)
    if session_channel is not None:
        try:
            emitted += session_channel.emit(event, data)
        except Exception:
            logger.debug(
                "SessionChannel emit failed for session %s",
                session_id,
                exc_info=True,
            )
    return emitted


def emit_now(session_id: str, payload: dict) -> int:
    """Emit the canonical completion event and its temporary legacy alias."""
    api = background_process_api()
    return (
        api._emit_to_session_streams(
            session_id, "bg_task_complete", dict(payload)
        )
        + api._emit_to_session_streams(
            session_id, "process_complete", dict(payload)
        )
    )


def flush_coalesced(session_id: str) -> None:
    """Flush the latest pending payload for one session."""
    api = background_process_api()
    payload: dict | None = None
    with api._EMIT_COALESCE_LOCK:
        payload = api._PENDING_EMIT_PAYLOADS.pop(session_id, None)
        api._PENDING_EMIT_TIMERS.pop(session_id, None)
        if payload is not None:
            api._LAST_EMIT_TS[session_id] = api.time.time()
    if payload is None:
        return
    try:
        api._emit_bg_task_complete_events_now(session_id, payload)
    except Exception:
        logger.debug(
            "coalesced bg_task_complete flush failed for session %s",
            session_id,
            exc_info=True,
        )


def emit_coalesced(session_id: str, payload: dict) -> int:
    """Coalesce completion frames per session while preserving the newest one."""
    if not session_id:
        return 0

    api = background_process_api()
    should_emit_now = False
    now = api.time.time()
    window = api._EMIT_COALESCE_WINDOW_SECS
    with api._EMIT_COALESCE_LOCK:
        last = api._LAST_EMIT_TS.get(session_id)
        has_pending = session_id in api._PENDING_EMIT_TIMERS
        if last is None or (now - last) >= window:
            api._LAST_EMIT_TS[session_id] = now
            should_emit_now = True
            if has_pending:
                api._PENDING_EMIT_PAYLOADS.pop(session_id, None)
                old_timer = api._PENDING_EMIT_TIMERS.pop(session_id, None)
                if old_timer is not None:
                    try:
                        old_timer.cancel()
                    except Exception:
                        logger.debug(
                            "coalesced bg_task_complete timer cancel failed for "
                            "session %s",
                            session_id,
                            exc_info=True,
                        )
        else:
            api._PENDING_EMIT_PAYLOADS[session_id] = payload

        if not should_emit_now:
            old_timer = api._PENDING_EMIT_TIMERS.get(session_id)
            if old_timer is not None:
                try:
                    old_timer.cancel()
                except Exception:
                    logger.debug(
                        "coalesced bg_task_complete timer cancel failed for "
                        "session %s",
                        session_id,
                        exc_info=True,
                    )
            timer = api.threading.Timer(
                window,
                api._flush_coalesced_bg_task_complete,
                args=(session_id,),
            )
            timer.daemon = True
            api._PENDING_EMIT_TIMERS[session_id] = timer
            timer.start()

    if should_emit_now:
        return api._emit_bg_task_complete_events_now(session_id, payload)
    return 0


REGISTRY_CONSUMED_CONTRACT = (
    "_lock",
    "_completion_consumed",
    "is_completion_consumed",
)


def mark_registry_completion_consumed(process_id: str) -> None:
    """Set the shared cross-consumer dedup marker on ProcessRegistry."""
    try:
        from tools.process_registry import process_registry
    except ImportError:
        logger.debug(
            "tools.process_registry not importable; skipping shared "
            "completion-consumed marker (best-effort, expected off-agent)",
            exc_info=True,
        )
        return
    try:
        lock = process_registry._lock
        consumed = process_registry._completion_consumed
    except AttributeError:
        logger.error(
            "ProcessRegistry coupling contract VIOLATED: expected private attrs "
            "%s for cross-A/B wakeup dedupe are missing; wakeups may double-fire",
            background_process_api()._REGISTRY_CONSUMED_CONTRACT,
            exc_info=True,
        )
        return
    try:
        with lock:
            consumed.add(process_id)
    except (AttributeError, TypeError):
        logger.error(
            "ProcessRegistry coupling contract VIOLATED: _lock/"
            "_completion_consumed changed shape; wakeups may double-fire",
            exc_info=True,
        )
