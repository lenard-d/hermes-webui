"""Admission owner for one in-process browser turn.

This module owns the short, synchronous transition from a validated request to
one persisted pending turn, one durable journal entry, one registered SSE
transport, and one worker. Provider execution and finalization remain behind
the runtime adapter/streaming seams; HTTP response handling remains in routes.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from api.config import (
    PENDING_BG_TASK_COMPLETIONS,
    PENDING_GOAL_CONTINUATION,
    get_webui_session_save_mode,
)
from .channels import create_stream_channel
from .runtime_state import (
    blocking_runtime_stream,
    finish_runtime_run,
    register_runtime_stream,
)
from api.sessions import (
    REPAIR_STALE_PENDING_GRACE_SECONDS,
    SessionWriteRejected,
    admission_write_owner,
    compensate_failed_admission,
    publish_session_list_changed,
    title_from,
)
from api.turn_journal import (
    TurnJournalCommitUnknown,
    append_turn_journal_event,
    confirm_turn_journal_event,
    make_turn_id,
)
from api.workspace import set_last_workspace

logger = logging.getLogger(__name__)


_ADMISSION_SESSION_FIELDS = (
    "workspace",
    "model",
    "model_provider",
    "active_stream_id",
    "post_compression_context_tokens_estimate",
    "pending_user_message",
    "pending_attachments",
    "pending_started_at",
    "pending_user_source",
    "title",
    "messages",
    "truncation_watermark",
    "updated_at",
)


def _journal_event_matches(actual: Any, expected: dict) -> bool:
    return isinstance(actual, dict) and all(
        actual.get(key) == value for key, value in expected.items()
    )


def _append_confirmed_journal_event(session_id: str, expected: dict) -> dict:
    """Append or durably reconcile one pre-identified admission event."""
    append_error = None
    try:
        written = append_turn_journal_event(session_id, expected)
    except Exception as exc:
        append_error = exc
    else:
        if _journal_event_matches(written, expected):
            return written

    try:
        confirmed = confirm_turn_journal_event(session_id, expected)
    except TurnJournalCommitUnknown:
        raise
    except Exception as exc:
        raise TurnJournalCommitUnknown(
            "unable to reconcile turn journal append"
        ) from exc
    if confirmed is not None:
        return confirmed
    if append_error is not None:
        raise append_error
    raise RuntimeError("turn journal append returned an unconfirmed event")


def _snapshot_admission_session(session) -> dict[str, tuple[bool, Any]]:
    return {
        field: (hasattr(session, field), copy.deepcopy(getattr(session, field, None)))
        for field in _ADMISSION_SESSION_FIELDS
    }


def _admission_baseline_was_persisted(session) -> bool:
    path = getattr(session, "path", None)
    if path is None:
        # Non-model collaborators used behind this seam predate sidecar-aware
        # compensation and are treated as already materialized.
        return True
    return bool(path.is_file())


def _rollback_admission_session(
    session,
    *,
    stream_id: str,
    snapshot: dict[str, tuple[bool, Any]],
    admitted_snapshot: dict[str, tuple[bool, Any]],
    baseline_persisted: bool,
    restore_goal_continuation: bool = False,
    restore_bg_completion: bool = False,
) -> bool:
    def restore(current) -> None:
        # The lifecycle fields are one state vector.  If any writer changed any
        # member after the pending checkpoint, fail closed rather than clearing
        # ownership while retaining a rejected eager message (or vice versa).
        for field in snapshot:
            admitted_present, admitted_value = admitted_snapshot[field]
            current_present = hasattr(current, field)
            if current_present != admitted_present or (
                current_present and getattr(current, field) != admitted_value
            ):
                raise RuntimeError("admission state changed before compensation")
        for field, (was_present, value) in snapshot.items():
            current_present = hasattr(current, field)
            if was_present:
                setattr(current, field, copy.deepcopy(value))
            elif current_present:
                delattr(current, field)

    def restore_markers() -> None:
        if restore_goal_continuation:
            PENDING_GOAL_CONTINUATION.add(session.session_id)
        if restore_bg_completion:
            PENDING_BG_TASK_COMPLETIONS.add(session.session_id)

    return compensate_failed_admission(
        session.session_id,
        expected_stream_id=stream_id,
        restore=restore,
        on_committed=restore_markers,
        baseline_persisted=baseline_persisted,
    )


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
    goal_related = request.goal_related
    consumed_goal_continuation = False
    consumed_bg_completion = False

    diag.stage("session_lock_wait") if diag else None
    while True:
        pending_save_error = None
        pending_save_traceback = None
        try:
            with admission_write_owner(session_id, session=session) as current:
                session = current
                # The request may have loaded its Session before waiting behind
                # a concurrent delete. Never materialize that stale seed after
                # deletion won the shared owner lock.
                locked_stream_id = getattr(session, "active_stream_id", None)
                blocking = blocking_runtime_stream(
                    session_id,
                    active_stream_id=locked_stream_id,
                    pending_user_message=getattr(session, "pending_user_message", None),
                    pending_started_at=getattr(session, "pending_started_at", None),
                    pending_grace_seconds=float(REPAIR_STALE_PENDING_GRACE_SECONDS),
                )
                if blocking:
                    diag.stage("response_write") if diag else None
                    return _conflict(blocking)
                if locked_stream_id:
                    needs_stale_cleanup = True
                else:
                    needs_stale_cleanup = False
                    admission_snapshot = _snapshot_admission_session(session)
                    baseline_persisted = _admission_baseline_was_persisted(session)
                    stream_id = uuid.uuid4().hex
                    turn_id = make_turn_id()
                    diag.stage("save_pending_state") if diag else None
                    was_hidden = _was_hidden_empty_session(session)
                    try:
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
                    except Exception as exc:
                        pending_save_error = exc
                        pending_save_traceback = exc.__traceback__
                    admitted_snapshot = _snapshot_admission_session(session)
                    if pending_save_error is None:
                        # Consume single-use wakeup markers only after the
                        # pending checkpoint succeeded.
                        if not goal_related and session_id in PENDING_GOAL_CONTINUATION:
                            goal_related = True
                            consumed_goal_continuation = True
                            PENDING_GOAL_CONTINUATION.discard(session_id)
                        if session_id in PENDING_BG_TASK_COMPLETIONS:
                            consumed_bg_completion = True
                            PENDING_BG_TASK_COMPLETIONS.discard(session_id)
        except SessionWriteRejected:
            return {"error": "Session not found", "_status": 404}
        if pending_save_error is not None:
            try:
                _rollback_admission_session(
                    session,
                    stream_id=stream_id,
                    snapshot=admission_snapshot,
                    admitted_snapshot=admitted_snapshot,
                    baseline_persisted=baseline_persisted,
                    restore_goal_continuation=consumed_goal_continuation,
                    restore_bg_completion=consumed_bg_completion,
                )
            except Exception:
                logger.exception("Failed to roll back turn admission after pending save error")
            raise pending_save_error.with_traceback(pending_save_traceback)
        if not needs_stale_cleanup:
            break
        if needs_stale_cleanup:
            diag.stage("stale_stream_cleanup") if diag else None
            cleared = clear_stale_stream(session)
            if not cleared and getattr(session, "active_stream_id", None):
                diag.stage("response_write") if diag else None
                return _conflict(getattr(session, "active_stream_id", None))

    diag.stage("turn_journal_submitted") if diag else None
    submitted_event = {
        "version": 1,
        "session_id": session_id,
        "event": "submitted",
        "turn_id": turn_id,
        "stream_id": stream_id,
        "role": "user",
        "content": request.message,
        "attachments": request.attachments,
        "workspace": request.workspace,
        "model": request.model,
        "model_provider": request.model_provider,
        "created_at": session.pending_started_at,
    }
    try:
        _append_confirmed_journal_event(session_id, submitted_event)
    except TurnJournalCommitUnknown:
        logger.exception("Submitted turn journal commit state is unknown")
        raise
    except Exception:
        logger.warning("Failed to append submitted turn journal event", exc_info=True)
        try:
            _rollback_admission_session(
                session,
                stream_id=stream_id,
                snapshot=admission_snapshot,
                admitted_snapshot=admitted_snapshot,
                baseline_persisted=baseline_persisted,
                restore_goal_continuation=consumed_goal_continuation,
                restore_bg_completion=consumed_bg_completion,
            )
        except Exception:
            logger.exception("Failed to roll back turn admission after journal error")
        raise

    admission_stage = "stream_channel_creation"
    try:
        stream = create_stream_channel()
        worker_kwargs = {
            "model_provider": request.model_provider,
            "goal_related": goal_related,
        }
        if request.moa_config is not None:
            worker_kwargs["moa_config"] = request.moa_config
        admission_stage = "worker_thread_creation"
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

        admission_stage = "stream_registration"
        diag.stage("stream_registration") if diag else None
        register_runtime_stream(
            stream_id,
            session_id,
            stream,
            goal_related=goal_related,
        )

        admission_stage = "ownership_recheck"
        with admission_write_owner(session_id) as current:
            if getattr(current, "active_stream_id", None) != stream_id:
                raise RuntimeError("turn admission ownership lost before publication")
            session = current
            response_pending_started_at = session.pending_started_at
            response_title = session.title

        admission_stage = "worker_thread_start"
        diag.stage("worker_thread_start") if diag else None
        thread.start()
    except Exception as admission_error:
        deleted_before_publication = isinstance(admission_error, SessionWriteRejected)
        try:
            finish_runtime_run(stream_id)
        except Exception:
            logger.exception("Failed to release runtime state after admission error")
            raise
        if deleted_before_publication:
            # Delete already removed the sidecar, journals, and cache while
            # holding this same owner lock. Do not recreate any of them while
            # compensating a stale admission candidate.
            return {"error": "Session not found", "_status": 404}
        interrupted_event = {
            "version": 1,
            "session_id": session_id,
            "event": "interrupted",
            "turn_id": turn_id,
            "stream_id": stream_id,
            "reason": f"admission_{admission_stage}_failed",
            "created_at": time.time(),
            "terminal": True,
        }
        terminal_confirmed = False
        try:
            _append_confirmed_journal_event(session_id, interrupted_event)
            terminal_confirmed = True
        except Exception:
            logger.exception("Failed to interrupt turn journal after admission error")
        if terminal_confirmed:
            try:
                _rollback_admission_session(
                    session,
                    stream_id=stream_id,
                    snapshot=admission_snapshot,
                    admitted_snapshot=admitted_snapshot,
                    baseline_persisted=baseline_persisted,
                    restore_goal_continuation=consumed_goal_continuation,
                    restore_bg_completion=consumed_bg_completion,
                )
            except Exception:
                logger.exception("Failed to roll back turn admission state")
        raise

    diag.stage("set_last_workspace") if diag else None
    try:
        set_last_workspace(request.workspace)
    except Exception:
        logger.warning("Failed to persist last workspace after turn admission", exc_info=True)

    if was_hidden:
        try:
            publish_session_list_changed(
                "session_new",
                profile=getattr(session, "profile", None),
                session_id=session_id,
            )
        except Exception:
            logger.warning("Failed to publish new-session event after admission", exc_info=True)

    response = {
        "stream_id": stream_id,
        "session_id": session_id,
        "pending_started_at": response_pending_started_at,
        "turn_id": turn_id,
        "title": response_title,
    }
    if request.normalized_model:
        response["effective_model"] = request.model
    if request.model_provider:
        response["effective_model_provider"] = request.model_provider
    return response
