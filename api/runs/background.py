"""Background and ephemeral task tracking for /background and /btw commands."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from api.config import SESSION_DIR
from api.sessions.store import Session, new_session

from .channels import create_stream_channel
from .local_entrypoint import run_agent_streaming
from .runtime_state import register_runtime_stream, runtime_stream_alive

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# parent_session_id -> list of task dicts
_BACKGROUND_TASKS: dict[str, list[dict[str, Any]]] = {}

# btw ephemeral session tracking: parent_sid -> {ephemeral_sid, stream_id, question}
_BTW_TRACKING: dict[str, dict[str, Any]] = {}


def track_background(parent_sid: str, bg_sid: str, stream_id: str,
                     task_id: str, prompt: str) -> None:
    with _lock:
        _BACKGROUND_TASKS.setdefault(parent_sid, []).append({
            "task_id": task_id,
            "bg_session_id": bg_sid,
            "stream_id": stream_id,
            "prompt": prompt,
            "status": "running",
            "started_at": time.time(),
            "answer": None,
            "completed_at": None,
        })


def track_btw(parent_sid: str, ephemeral_sid: str, stream_id: str,
              question: str) -> None:
    with _lock:
        _BTW_TRACKING[parent_sid] = {
            "ephemeral_session_id": ephemeral_sid,
            "stream_id": stream_id,
            "question": question,
        }


def complete_background(parent_sid: str, task_id: str, answer: str) -> None:
    with _lock:
        for t in _BACKGROUND_TASKS.get(parent_sid, []):
            if t["task_id"] == task_id and t["status"] == "running":
                t["status"] = "done"
                t["answer"] = answer
                t["completed_at"] = time.time()
                break


def get_results(parent_sid: str) -> list[dict[str, Any]]:
    """Return completed background task results and remove only the done ones
    from tracking.  Tasks still in ``status="running"`` MUST stay in the list
    so that ``complete_background()`` can still find them when the worker
    thread finishes — otherwise the first poll during a long-running task
    silently drops it and the result is lost forever.
    """
    with _lock:
        tasks = _BACKGROUND_TASKS.get(parent_sid, [])
        done = [t for t in tasks if t["status"] == "done"]
        still_running = [t for t in tasks if t["status"] != "done"]
        if still_running:
            _BACKGROUND_TASKS[parent_sid] = still_running
        else:
            _BACKGROUND_TASKS.pop(parent_sid, None)
        return [{
            "task_id": t["task_id"],
            "prompt": t["prompt"],
            "answer": t["answer"],
            "completed_at": t["completed_at"],
        } for t in done]


def get_background_tasks(parent_sid: str) -> list[dict[str, Any]]:
    """Return all background tasks (running and done) for a parent session."""
    with _lock:
        return list(_BACKGROUND_TASKS.get(parent_sid, []))


def cleanup_btw(parent_sid: str) -> dict[str, Any] | None:
    """Remove and return btw tracking for a parent session."""
    with _lock:
        return _BTW_TRACKING.pop(parent_sid, None)


def start_btw(parent_session, question: str) -> dict:
    """Start one ephemeral side-question run without mutating the parent."""
    current_stream_id = getattr(parent_session, "active_stream_id", None)
    if current_stream_id:
        if runtime_stream_alive(current_stream_id):
            return {
                "error": "session already has an active stream",
                "_status": 409,
            }
        parent_session.active_stream_id = None

    model_provider = getattr(parent_session, "model_provider", None)
    ephemeral = new_session(
        workspace=parent_session.workspace,
        model=parent_session.model,
        model_provider=model_provider,
        profile=getattr(parent_session, "profile", None),
    )
    ephemeral.messages = list(parent_session.messages or [])
    ephemeral.title = f"btw: {question[:60]}"
    ephemeral.save()
    stream_id = uuid.uuid4().hex
    ephemeral.active_stream_id = stream_id
    ephemeral.save()
    register_runtime_stream(
        stream_id,
        ephemeral.session_id,
        create_stream_channel(),
    )
    track_btw(
        parent_session.session_id,
        ephemeral.session_id,
        stream_id,
        question,
    )
    threading.Thread(
        target=run_agent_streaming,
        args=(
            ephemeral.session_id,
            question,
            parent_session.model,
            parent_session.workspace,
            stream_id,
            None,
        ),
        kwargs={"ephemeral": True, "model_provider": model_provider},
        daemon=True,
    ).start()
    return {
        "stream_id": stream_id,
        "session_id": ephemeral.session_id,
        "parent_session_id": parent_session.session_id,
    }


def _last_assistant_answer(session_id: str) -> str:
    reloaded = Session.load(session_id)
    for message in reversed((reloaded.messages if reloaded else None) or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("_error"):
            continue
        content = str(message.get("content") or "").strip()
        if content:
            return content
    return ""


def _run_background_and_complete(
    *,
    parent_session_id: str,
    background_session_id: str,
    task_id: str,
    prompt: str,
    model,
    model_provider,
    workspace,
    stream_id: str,
) -> None:
    try:
        run_agent_streaming(
            background_session_id,
            prompt,
            model,
            workspace,
            stream_id,
            None,
            model_provider=model_provider,
        )
        try:
            answer = _last_assistant_answer(background_session_id)
        except Exception:
            complete_background(
                parent_session_id,
                task_id,
                "(background task failed)",
            )
            answer = None
        if answer is not None:
            complete_background(
                parent_session_id,
                task_id,
                answer or "(no answer produced)",
            )
        try:
            (SESSION_DIR / f"{background_session_id}.json").unlink(
                missing_ok=True
            )
        except Exception:
            logger.debug(
                "failed to remove hidden background session %s",
                background_session_id,
                exc_info=True,
            )
    except Exception:
        complete_background(
            parent_session_id,
            task_id,
            "(background task failed)",
        )


def start_background(parent_session, prompt: str) -> dict:
    """Create, register, and run one tracked background task."""
    model_provider = getattr(parent_session, "model_provider", None)
    background = new_session(
        workspace=parent_session.workspace,
        model=parent_session.model,
        model_provider=model_provider,
        profile=getattr(parent_session, "profile", None),
    )
    background.title = f"bg: {prompt[:60]}"
    background.save()
    stream_id = uuid.uuid4().hex
    background.active_stream_id = stream_id
    background.save()
    register_runtime_stream(
        stream_id,
        background.session_id,
        create_stream_channel(),
    )
    task_id = uuid.uuid4().hex[:8]
    track_background(
        parent_session.session_id,
        background.session_id,
        stream_id,
        task_id,
        prompt,
    )
    threading.Thread(
        target=_run_background_and_complete,
        kwargs={
            "parent_session_id": parent_session.session_id,
            "background_session_id": background.session_id,
            "task_id": task_id,
            "prompt": prompt,
            "model": parent_session.model,
            "model_provider": model_provider,
            "workspace": parent_session.workspace,
            "stream_id": stream_id,
        },
        daemon=True,
    ).start()
    return {
        "task_id": task_id,
        "stream_id": stream_id,
        "session_id": background.session_id,
    }
