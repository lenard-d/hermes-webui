"""Admission owner for one in-process browser turn.

This module owns the short, synchronous transition from a validated request to
one persisted pending turn, one durable journal entry, one registered SSE
transport, and one worker. Provider execution and finalization remain behind
the runtime adapter/streaming seams; HTTP response handling remains in routes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from api.config import (
    PENDING_BG_TASK_COMPLETIONS,
    PENDING_GOAL_CONTINUATION,
    _get_session_agent_lock,
    blocking_runtime_stream,
    create_stream_channel,
    get_webui_session_save_mode,
    register_runtime_stream,
)
from api.models import _REPAIR_STALE_PENDING_GRACE_SECONDS, title_from
from api.session_events import publish_session_list_changed
from api.turn_journal import append_turn_journal_event
from api.workspace import set_last_workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalTurnRequest:
    message: str
    attachments: list
    workspace: str
    model: str
    model_provider: str | None = None
    normalized_model: bool = False
    goal_related: bool = False
    source: str = "webui"
    moa_config: Any = None


def checkpoint_user_message(
    session,
    message: str,
    attachments,
    started_at: float | None,
    *,
    source: str = "webui",
) -> None:
    """Materialize the submitted user turn for eager persistence mode."""
    if not message:
        return
    existing = list(getattr(session, "messages", None) or [])
    if existing:
        latest = existing[-1]
        if isinstance(latest, dict) and latest.get("role") == "user":
            latest_text = " ".join(str(latest.get("content") or "").split())
            message_text = " ".join(str(message or "").split())
            if latest_text == message_text:
                return
    user_message = {"role": "user", "content": message}
    if source and source != "webui":
        user_message["_source"] = source
    if isinstance(started_at, (int, float)) and started_at > 0:
        user_message["timestamp"] = int(started_at)
    if attachments:
        user_message["attachments"] = list(attachments)
    session.messages.append(user_message)
    if getattr(session, "truncation_watermark", None):
        session.truncation_watermark = user_message.get("timestamp") or time.time()


def _is_provisional_title(title: Any) -> bool:
    return str(title or "").strip() in ("", "Untitled", "New Chat")


def _title_from_prompt(prompt: str, fallback: str = "Untitled") -> str:
    text = str(prompt or "").strip()
    if not text:
        return fallback
    return title_from([{"role": "user", "content": text}], fallback) or fallback


def prepare_session_for_turn(
    session,
    *,
    message: str,
    attachments,
    workspace: str,
    model: str,
    model_provider,
    stream_id: str,
    started_at: float | None = None,
    source: str = "webui",
) -> None:
    """Persist the pending owner for a validated, non-empty turn."""
    session.workspace = workspace
    session.model = model
    session.model_provider = model_provider
    session.active_stream_id = stream_id
    session.post_compression_context_tokens_estimate = None
    session.pending_user_message = message
    session.pending_attachments = attachments
    session.pending_started_at = started_at if started_at is not None else time.time()
    session.pending_user_source = source
    current_title = getattr(session, "title", None)
    if _is_provisional_title(current_title):
        provisional_title = _title_from_prompt(message, current_title or "Untitled")
        if provisional_title and not _is_provisional_title(provisional_title):
            session.title = provisional_title
    if get_webui_session_save_mode() == "eager":
        checkpoint_user_message(
            session,
            message,
            attachments,
            session.pending_started_at,
            source=source,
        )
    session.save()


def _was_hidden_empty_session(session) -> bool:
    return (
        getattr(session, "title", "Untitled") == "Untitled"
        and not getattr(session, "messages", None)
        and not getattr(session, "active_stream_id", None)
        and not getattr(session, "pending_user_message", None)
        and not getattr(session, "worktree_path", None)
    )


def _conflict(stream_id: str) -> dict:
    return {
        "error": "session already has an active stream",
        "active_stream_id": stream_id,
        "_status": 409,
    }


def start_local_turn(
    session,
    request: LocalTurnRequest,
    *,
    worker_target: Callable[..., Any],
    clear_stale_stream: Callable[[Any], bool],
    diag=None,
) -> dict:
    """Atomically admit, persist, publish, and launch one local turn."""
    session_id = str(getattr(session, "session_id", "") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")

    diag.stage("active_stream_check") if diag else None
    current_stream_id = getattr(session, "active_stream_id", None)
    if current_stream_id:
        blocking = blocking_runtime_stream(
            session_id,
            active_stream_id=current_stream_id,
            pending_user_message=getattr(session, "pending_user_message", None),
            pending_started_at=getattr(session, "pending_started_at", None),
            pending_grace_seconds=float(_REPAIR_STALE_PENDING_GRACE_SECONDS),
        )
        if blocking:
            diag.stage("response_write") if diag else None
            return _conflict(blocking)
        diag.stage("stale_stream_cleanup") if diag else None
        clear_stale_stream(session)

    goal_related = request.goal_related

    session_lock = _get_session_agent_lock(session_id)
    diag.stage("session_lock_wait") if diag else None
    while True:
        with session_lock:
            locked_stream_id = getattr(session, "active_stream_id", None)
            blocking = blocking_runtime_stream(
                session_id,
                active_stream_id=locked_stream_id,
                pending_user_message=getattr(session, "pending_user_message", None),
                pending_started_at=getattr(session, "pending_started_at", None),
                pending_grace_seconds=float(_REPAIR_STALE_PENDING_GRACE_SECONDS),
            )
            if blocking:
                diag.stage("response_write") if diag else None
                return _conflict(blocking)
            if locked_stream_id:
                needs_stale_cleanup = True
            else:
                needs_stale_cleanup = False
                # Consume single-use wakeup markers only after this request has
                # won admission. A racing request that receives 409 must not
                # steal the continuation from the eventual successor.
                if not goal_related and session_id in PENDING_GOAL_CONTINUATION:
                    goal_related = True
                    PENDING_GOAL_CONTINUATION.discard(session_id)
                PENDING_BG_TASK_COMPLETIONS.discard(session_id)
                stream_id = uuid.uuid4().hex
                diag.stage("save_pending_state") if diag else None
                was_hidden = _was_hidden_empty_session(session)
                prepare_session_for_turn(
                    session,
                    message=request.message,
                    attachments=request.attachments,
                    workspace=request.workspace,
                    model=request.model,
                    model_provider=request.model_provider,
                    stream_id=stream_id,
                    source=request.source,
                )
                break
        if needs_stale_cleanup:
            diag.stage("stale_stream_cleanup") if diag else None
            cleared = clear_stale_stream(session)
            if not cleared and getattr(session, "active_stream_id", None):
                diag.stage("response_write") if diag else None
                return _conflict(getattr(session, "active_stream_id", None))

    if was_hidden:
        publish_session_list_changed(
            "session_new",
            profile=getattr(session, "profile", None),
            session_id=session_id,
        )

    diag.stage("turn_journal_submitted") if diag else None
    journal_event = {}
    try:
        journal_event = append_turn_journal_event(
            session_id,
            {
                "event": "submitted",
                "stream_id": stream_id,
                "role": "user",
                "content": request.message,
                "attachments": request.attachments,
                "workspace": request.workspace,
                "model": request.model,
                "model_provider": request.model_provider,
                "created_at": session.pending_started_at,
            },
        )
    except Exception:
        logger.warning("Failed to append submitted turn journal event", exc_info=True)

    diag.stage("set_last_workspace") if diag else None
    set_last_workspace(request.workspace)
    diag.stage("stream_registration") if diag else None
    stream = create_stream_channel()
    register_runtime_stream(
        stream_id,
        session_id,
        stream,
        goal_related=goal_related,
    )

    diag.stage("worker_thread_start") if diag else None
    worker_kwargs = {
        "model_provider": request.model_provider,
        "goal_related": goal_related,
    }
    if request.moa_config is not None:
        worker_kwargs["moa_config"] = request.moa_config
    thread = threading.Thread(
        target=worker_target,
        args=(
            session_id,
            request.message,
            request.model,
            request.workspace,
            stream_id,
            request.attachments,
        ),
        kwargs=worker_kwargs,
        daemon=True,
    )
    thread.start()

    response = {
        "stream_id": stream_id,
        "session_id": session_id,
        "pending_started_at": session.pending_started_at,
        "turn_id": journal_event.get("turn_id"),
        "title": session.title,
    }
    if request.normalized_model:
        response["effective_model"] = request.model
    if request.model_provider:
        response["effective_model_provider"] = request.model_provider
    return response
