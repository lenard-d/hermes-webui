"""Late-bound adapters for the process-local chat runtime registry.

``api.config`` remains the compatibility facade and the owner of its mutable
runtime globals.  Resolving that facade at call time is intentional: callers
and tests have long patched ``api.config.RUNTIME_STATE``, and an eager import or
captured state object would silently bypass that established seam.
"""

from typing import Protocol, cast

from api.config_parts.facade import config_api
from api.runtime_state import ProcessRuntimeState


class ConfigRuntimeAPI(Protocol):
    """The small facade surface required by the runtime adapters."""

    RUNTIME_STATE: ProcessRuntimeState
    LAST_RUN_FINISHED_AT: float | None


def _config_api() -> ConfigRuntimeAPI:
    return cast(ConfigRuntimeAPI, config_api())


def register_stream_owner(stream_id: str, session_id: str) -> None:
    """Record the session that owns a stream before worker startup."""
    _config_api().RUNTIME_STATE.register_owner(stream_id, session_id)


def stream_owner_session_id(stream_id: str) -> str | None:
    """Return the synchronously-recorded owner session for a stream, if any."""
    return _config_api().RUNTIME_STATE.owner_session_id(stream_id)


def unregister_stream_owner(stream_id: str) -> None:
    """Forget the pre-worker stream owner once the stream has torn down."""
    _config_api().RUNTIME_STATE.unregister_owner(stream_id)


def register_active_run(stream_id: str, **metadata) -> None:
    """Mark a WebUI agent worker as alive until its outer finally exits."""
    _config_api().RUNTIME_STATE.register_worker(stream_id, **metadata)


def update_active_run(stream_id: str, **metadata) -> None:
    """Update active-run metadata without creating a new run implicitly."""
    _config_api().RUNTIME_STATE.update_worker(stream_id, **metadata)


def unregister_active_run(stream_id: str) -> None:
    """Remove a worker from the active-run registry and record idle start."""
    config_api = _config_api()
    config_api.RUNTIME_STATE.unregister_worker(stream_id)
    config_api.LAST_RUN_FINISHED_AT = config_api.RUNTIME_STATE.last_run_finished_at


def register_runtime_stream(
    stream_id: str,
    session_id: str,
    channel,
    *,
    goal_related: bool = False,
) -> None:
    """Publish a stream, authorization owner, and optional goal state together."""
    _config_api().RUNTIME_STATE.register_stream(
        stream_id,
        session_id,
        channel,
        goal_related=goal_related,
    )


def blocking_runtime_stream(
    session_id: str,
    *,
    active_stream_id: str | None = None,
    pending_user_message: str | None = None,
    pending_started_at: float | None = None,
    pending_grace_seconds: float = 30.0,
    worker_unwind_seconds: float = 180.0,
) -> str | None:
    """Return the process-local run that currently blocks session admission."""
    return _config_api().RUNTIME_STATE.blocking_stream_for_session(
        session_id,
        active_stream_id=active_stream_id,
        pending_user_message=pending_user_message,
        pending_started_at=pending_started_at,
        pending_grace_seconds=pending_grace_seconds,
        worker_unwind_seconds=worker_unwind_seconds,
    )


def runtime_stream_alive(stream_id: str) -> bool:
    return _config_api().RUNTIME_STATE.has_stream(stream_id)


def runtime_worker_alive(stream_id: str) -> bool:
    return _config_api().RUNTIME_STATE.has_worker(stream_id)


def runtime_transport(stream_id: str):
    return _config_api().RUNTIME_STATE.transport(stream_id)


def runtime_transport_items():
    return _config_api().RUNTIME_STATE.transport_items()


def runtime_transport_count(*, timeout: float | None = None) -> int | None:
    return _config_api().RUNTIME_STATE.transport_count(timeout=timeout)


def runtime_worker_items():
    return _config_api().RUNTIME_STATE.worker_items()


def runtime_last_run_finished_at() -> float | None:
    return _config_api().RUNTIME_STATE.last_run_finished_at


def runtime_active_run_ids():
    return _config_api().RUNTIME_STATE.active_run_ids()


def runtime_run_session_id(stream_id: str) -> str | None:
    return _config_api().RUNTIME_STATE.run_session_id(stream_id)


def runtime_last_event_id(stream_id: str) -> str | None:
    return _config_api().RUNTIME_STATE.last_event_id(stream_id)


def note_runtime_last_event_id(stream_id: str, event_id: str) -> None:
    _config_api().RUNTIME_STATE.note_last_event_id(stream_id, event_id)


def initialize_runtime_execution(stream_id: str, cancel_event) -> bool:
    """Initialize recoverable producer buffers for an admitted live run."""
    return _config_api().RUNTIME_STATE.initialize_execution(stream_id, cancel_event)


def append_runtime_partial_text(stream_id: str, text) -> bool:
    return _config_api().RUNTIME_STATE.append_partial_text(stream_id, text)


def replace_runtime_partial_text(stream_id: str, text) -> bool:
    return _config_api().RUNTIME_STATE.replace_partial_text(stream_id, text)


def append_runtime_reasoning_text(stream_id: str, text) -> bool:
    return _config_api().RUNTIME_STATE.append_reasoning_text(stream_id, text)


def replace_runtime_reasoning_text(stream_id: str, text) -> bool:
    return _config_api().RUNTIME_STATE.replace_reasoning_text(stream_id, text)


def start_runtime_tool_call(
    stream_id: str,
    *,
    name,
    args,
    tool_call_id=None,
) -> bool:
    return _config_api().RUNTIME_STATE.start_tool_call(
        stream_id,
        name=name,
        args=args,
        tool_call_id=tool_call_id,
    )


def finish_runtime_tool_call(
    stream_id: str,
    *,
    name=None,
    tool_call_id=None,
    **metadata,
) -> bool:
    return _config_api().RUNTIME_STATE.finish_tool_call(
        stream_id,
        name=name,
        tool_call_id=tool_call_id,
        **metadata,
    )


def attach_runtime_agent(stream_id: str, agent) -> bool:
    """Expose an agent for cancellation only while its run remains live."""
    return _config_api().RUNTIME_STATE.attach_agent(stream_id, agent)


def runtime_progress_snapshot(stream_id: str):
    """Return an immutable copy of terminally relevant live progress."""
    return _config_api().RUNTIME_STATE.progress_snapshot(stream_id)


def begin_runtime_cancel(stream_id: str):
    """Claim cancellation and snapshot the process-local run state."""
    return _config_api().RUNTIME_STATE.begin_cancel(stream_id)


def finish_runtime_run(stream_id: str) -> bool:
    """Release every process-local value owned by a completed run."""
    config_api = _config_api()
    removed = config_api.RUNTIME_STATE.finish_run(stream_id)
    config_api.LAST_RUN_FINISHED_AT = config_api.RUNTIME_STATE.last_run_finished_at
    return removed
