"""Public run-domain entry point for autonomous server-side turns.

The HTTP assembly layer installs the concrete orchestration callback during
startup.  Background domains call this module and therefore never depend on
the route facade; the callback remains late-bound so compatibility monkeypatch
seams and route-module reloads continue to work during the migration.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from api.sessions import get_session

from . import process_wakeup, turn_start, turn_input


ServerTurnStarter = Callable[..., dict[str, Any]]

_STARTER_LOCK = threading.Lock()
_starter: ServerTurnStarter | None = None


@dataclass(frozen=True)
class ServerTurnDependencies:
    """Application-composed seams not yet public outside the route facade."""

    read_profile_model_config: Callable
    resolve_model_state: Callable
    clear_stale_stream: Callable
    runtime_barrier: Callable[..., dict | None] = (
        turn_start.agent_runtime_barrier_response
    )
    load_session: Callable = get_session
    resolve_workspace: Callable = turn_input.resolve_chat_workspace
    revalidate_wakeup: Callable = process_wakeup.revalidate_process_wakeup
    start_run: Callable[..., dict] = turn_start.start_run


def configure_start_session_turn(starter: ServerTurnStarter) -> None:
    """Install the application-composed server-turn orchestration callback."""
    if not callable(starter):
        raise TypeError("starter must be callable")
    global _starter
    with _STARTER_LOCK:
        _starter = starter


def start_session_turn(
    session_id: str,
    message: str,
    *,
    source: str = "process_wakeup",
) -> dict[str, Any]:
    """Start one autonomous turn through the configured run orchestration."""
    with _STARTER_LOCK:
        starter = _starter
    if starter is None:
        raise RuntimeError("server-turn orchestration is not configured")
    return starter(session_id, message, source=source)


def start_prepared_session_turn(
    session_id: str,
    message: str,
    *,
    source: str = "process_wakeup",
    dependencies: ServerTurnDependencies,
) -> dict[str, Any]:
    """Resolve, admit, and publish one autonomous server-side turn."""
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return {"error": "message is required", "_status": 400}
    stale_response = dependencies.runtime_barrier(runner_local_owned=True)
    if stale_response is not None:
        stale_response["_status"] = 409
        return stale_response
    turn_source = str(source or "process_wakeup").strip() or "process_wakeup"
    try:
        session = dependencies.load_session(session_id)
    except KeyError:
        return {"error": "Session not found", "_status": 404}
    try:
        workspace = dependencies.resolve_workspace(session, None)
    except ValueError as exc:
        return {"error": str(exc), "_status": 400}

    requested_model = session.model
    requested_provider = getattr(session, "model_provider", None)
    profile_provider, profile_default, profile_config = (
        dependencies.read_profile_model_config(session, requested_provider)
    )
    model, model_provider, normalized_model = dependencies.resolve_model_state(
        requested_model,
        requested_provider,
        profile_provider=profile_provider,
        profile_default_model=profile_default,
        profile_config=profile_config,
        prefer_cached_catalog=True,
    )
    try:
        admission = dependencies.revalidate_wakeup(
            session.session_id,
            model=model,
            provider=model_provider,
            source=turn_source,
        )
    except KeyError:
        return {"error": "Session not found", "_status": 404}
    if admission.rejection is not None:
        return admission.rejection
    session = admission.session

    response = dependencies.start_run(
        session,
        message=normalized_message,
        attachments=[],
        workspace=workspace,
        model=model,
        model_provider=model_provider,
        normalized_model=normalized_model,
        source=turn_source,
        route="start_session_turn",
        clear_stale_stream=dependencies.clear_stale_stream,
    )
    try:
        status = int((response or {}).get("_status", 200) or 200)
        stream_id = (response or {}).get("stream_id")
        if status < 400 and stream_id:
            from api.background_process import get_session_channel

            channel = get_session_channel(session_id)
            if channel is not None:
                channel.emit(
                    "server_turn_started",
                    {
                        "session_id": str(session_id),
                        "stream_id": str(stream_id),
                        "pending_started_at": (response or {}).get(
                            "pending_started_at"
                        ),
                        "source": source,
                    },
                )
    except Exception:
        import logging

        logging.getLogger(__name__).debug(
            "server_turn_started fan-out failed for session %s",
            session_id,
            exc_info=True,
        )
    return response
