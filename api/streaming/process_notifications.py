"""Delivery and acknowledgement of background process completions."""

from __future__ import annotations

import logging
import os
import queue
import time

from api.config import PROCESS_SESSION_INDEX, PROCESS_SESSION_INDEX_LOCK
from api.process_event_utils import (
    claim_async_delegation_delivery,
    complete_async_delegation_delivery,
    completion_delivery_id,
    release_async_delegation_delivery,
    requeue_async_delegation_event,
    schedule_async_delegation_claim_retry,
)


logger = logging.getLogger(__name__)


def _stale_completion_max_age_seconds() -> float:
    """Max age (seconds) a background-process completion may sit in the queue
    before the WebUI drain treats it as stale and drops it instead of
    prepending it to the user's next turn.

    Completions older than this are silently consumed (not requeued) so a
    notification that finally fires long after the user moved on cannot
    contaminate an unrelated later turn. See nesquena/hermes-webui#4029.

    Configurable via HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS. A value of
    0 (or negative) disables age-gating and restores the legacy drain-all
    behavior. Defaults to 6 hours.
    """
    raw = os.environ.get("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS=%r; using default",
                raw,
            )
    return 6 * 60 * 60  # 6 hours

def _format_process_notification(evt: dict) -> str:
    """Format a completed background process notification for agent input."""
    if not isinstance(evt, dict):
        return ''
    if evt.get('type') == 'async_delegation':
        try:
            from tools.process_registry import format_process_notification

            return format_process_notification(evt) or ''
        except Exception:
            logger.debug("Failed to format async delegation notification", exc_info=True)
            return ''
    if evt.get('type') != 'completion':
        return ''
    _sid = evt.get('session_id', '')
    _cmd = evt.get('command', '')
    _exit = evt.get('exit_code', '')
    _out = evt.get('output') or ''
    if len(_out) > 4000:
        _out = _out[:4000] + '\n... (truncated)'
    return (
        f"[IMPORTANT: Background process {_sid} completed (exit code {_exit}).\n"
        f"Command: {_cmd}\n"
        f"Output:\n{_out}]"
    )

def _mark_process_completion_consumed(process_registry, process_id: str) -> None:
    """Best-effort bridge to the agent registry's private completion marker."""
    try:
        with process_registry._lock:
            process_registry._completion_consumed.add(process_id)
    except Exception:
        logger.debug("Failed to mark process completion consumed", exc_info=True)

def _completion_event_targets_webui_session(evt_session_key: str, session_id: str) -> bool:
    """Return whether a completion event belongs to this WebUI session.

    WebUI normally registers ``PROCESS_SESSION_INDEX[session_id] = session_id``.
    Gateway/agent session keys can differ, so match the direct WebUI case first
    and otherwise resolve through the same session-key index used by the
    background wakeup path.
    """
    if not evt_session_key or not session_id:
        return False
    if evt_session_key == session_id:
        return True
    try:
        with PROCESS_SESSION_INDEX_LOCK:
            return PROCESS_SESSION_INDEX.get(evt_session_key) == session_id
    except Exception:
        logger.debug("Failed to resolve completion event session key", exc_info=True)
        return False

def _drain_webui_process_notifications(
    session_id: str,
    *,
    pending_async_acceptances: list | None = None,
) -> list[str]:
    """Return completion notifications that belong to this WebUI session.

    The agent registry completion queue is process-wide and events do not carry
    the WebUI session key directly. Look up the live process session before
    delivery so completions from other tabs remain queued for their owners.
    """
    if not session_id:
        return []
    try:
        from tools.process_registry import process_registry
    except Exception:
        return []

    notifications: list[str] = []
    skipped_events: list[dict] = []
    async_retry_events: list[tuple[dict, bool]] = []
    completion_queue = getattr(process_registry, 'completion_queue', None)
    if completion_queue is None:
        return []

    # Computed once per drain (not per event): reads/validates the env cap a
    # single time so an invalid value logs at most one warning per drain.
    stale_completion_max_age = _stale_completion_max_age_seconds()

    while True:
        try:
            evt = completion_queue.get_nowait()
        except queue.Empty:
            break
        except Exception:
            logger.debug("Failed to drain process completion queue", exc_info=True)
            break

        evt_sid = completion_delivery_id(evt) if isinstance(evt, dict) else ''
        if not evt_sid:
            skipped_events.append(evt)
            continue
        is_async_delegation = (
            isinstance(evt, dict) and evt.get('type') == 'async_delegation'
        )
        try:
            if (
                not is_async_delegation
                and process_registry.is_completion_consumed(evt_sid)
            ):
                continue
            evt_session_key = str(evt.get('session_key') or '') if isinstance(evt, dict) else ''
            evt_origin_ui_session_id = (
                str(evt.get('origin_ui_session_id') or '') if isinstance(evt, dict) else ''
            )
            if not evt_session_key or not evt_origin_ui_session_id:
                proc = process_registry.get(evt_sid)
                if not evt_session_key:
                    evt_session_key = str(getattr(proc, 'session_key', '') or '')
                if not evt_origin_ui_session_id:
                    evt_origin_ui_session_id = (
                        str(getattr(proc, 'origin_ui_session_id', '') or '')
                        or str(getattr(proc, 'spawn_session_id', '') or '')
                    )
        except Exception:
            evt_session_key = ''
            evt_origin_ui_session_id = ''

        # origin_ui_session_id is the exact, immutable return address and is
        # authoritative over the mutable session-key index (mirrors the
        # background _process_one path via _resolve_completion_target). When it
        # is present, this drain claims/ACKs the event ONLY for the origin
        # session — otherwise the next-turn drain could win the shared-queue
        # race and deliver+ACK a completion to the wrong (session-key-index)
        # session, leaving the true origin empty. Fall back to the session-key
        # target check only for legacy events that carry no origin address.
        if evt_origin_ui_session_id:
            if evt_origin_ui_session_id != session_id:
                skipped_events.append(evt)
                continue
        elif not _completion_event_targets_webui_session(evt_session_key, session_id):
            skipped_events.append(evt)
            continue
        # Age-gate stale completions: a completion that fires long after the
        # user moved on must not be prepended to an unrelated later turn
        # (nesquena/hermes-webui#4029). Drop (consume, do not requeue) any
        # completion whose enqueue time is older than the configured cap.
        # Events without a 'completed_at' (older agent builds) are never
        # dropped here, preserving backward-compatible behavior.
        is_stale = False
        stale_age = 0.0
        if stale_completion_max_age > 0 and isinstance(evt, dict):
            completed_at = evt.get('completed_at')
            if isinstance(completed_at, (int, float)) and completed_at > 0:
                stale_age = time.time() - completed_at
                is_stale = stale_age > stale_completion_max_age

        if is_async_delegation:
            try:
                claim = claim_async_delegation_delivery(evt, "webui-next-turn")
            except Exception:
                skipped_events.append(evt)
                continue
            if claim is None:
                schedule_async_delegation_claim_retry(evt, completion_queue)
                continue
            notification_added = False
            try:
                if is_stale:
                    notification = ''
                else:
                    notification = _format_process_notification(evt)
                    if not notification:
                        raise ValueError(
                            "async delegation formatter returned an empty notification"
                        )
                if notification:
                    notifications.append(notification)
                    notification_added = True
                if is_stale:
                    # Stale async events are an explicit terminal disposition.
                    complete_async_delegation_delivery(evt, claim)
                elif pending_async_acceptances is not None:
                    pending_async_acceptances.append(
                        (evt, claim, notification, completion_queue)
                    )
                else:
                    # Direct callers without a live agent turn retain the
                    # historical synchronous acceptance behavior used by
                    # CLI-style drains.
                    complete_async_delegation_delivery(evt, claim)
            except Exception:
                if notification_added:
                    notifications.pop()
                release_async_delegation_delivery(evt, claim)
                async_retry_events.append(
                    (evt, bool(getattr(claim, "durable", False)))
                )
                logger.warning(
                    "Failed to accept async delegation completion for session %s",
                    session_id,
                    exc_info=True,
                )
                continue
            if is_stale:
                logger.info(
                    "Dropping stale async-delegation completion for session %s "
                    "(age %.0fs > cap %.0fs)",
                    evt_sid, stale_age, stale_completion_max_age,
                )
            continue

        if is_stale:
            logger.info(
                "Dropping stale background-process completion for "
                "session %s (age %.0fs > cap %.0fs)",
                evt_sid, stale_age, stale_completion_max_age,
            )
            _mark_process_completion_consumed(process_registry, evt_sid)
            continue

        notification = _format_process_notification(evt)
        if notification:
            notifications.append(notification)
        # Matched but unformattable process completions are consumed rather than
        # replayed forever on later turns.
        _mark_process_completion_consumed(process_registry, evt_sid)

    for evt, durable in async_retry_events:
        requeue_async_delegation_event(
            evt,
            completion_queue,
            durable=durable,
        )
    for evt in skipped_events:
        try:
            completion_queue.put(evt)
        except Exception:
            logger.debug("Failed to requeue process completion event", exc_info=True)
            break
    return notifications

def _accept_pending_async_delegations(
    pending_async_acceptances: list,
    *,
    session_id: str,
) -> list[str]:
    """ACK turn-bound delegation claims and return rejected notifications."""
    rejected_notifications: list[str] = []
    for evt, claim, notification, completion_queue in pending_async_acceptances:
        try:
            complete_async_delegation_delivery(evt, claim)
        except Exception:
            release_async_delegation_delivery(evt, claim)
            requeue_async_delegation_event(
                evt,
                completion_queue,
                durable=bool(getattr(claim, "durable", False)),
            )
            rejected_notifications.append(notification)
            logger.warning(
                "Async delegation was not accepted into session %s; retrying later",
                session_id,
                exc_info=True,
            )
    return rejected_notifications
