"""Run-owner selection and synchronous admission for one prepared chat turn.

HTTP routes and autonomous wakeups both arrive here only after they have
validated identity and resolved the session's workspace/model context.  This
module owns the remaining transition: reject a stale local runtime, select the
configured execution owner, and admit exactly one local/gateway worker.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from api.agent_runtime import AgentRuntimeChangedError, ensure_agent_runtime_current
from api.config import get_config

from . import adapter as runtime_adapter
from .admission import LocalTurnRequest, start_local_turn
from .gateway import run_gateway_chat_streaming
from .gateway_config import webui_gateway_chat_enabled
from .local_entrypoint import run_agent_streaming
from .runtime_state import blocking_runtime_stream


ClearStaleStream = Callable[[Any], bool]


def active_run_stream_for_session(session_id: str | None) -> str | None:
    """Return a live worker stream during the post-cancel unwind window."""
    return blocking_runtime_stream(str(session_id or ""))


def agent_runtime_barrier_response(
    *,
    runner_local_owned: bool = False,
    external_runtime_owned: bool | None = None,
    gateway_owned: Callable[[], bool] | None = None,
    ensure_runtime_current: Callable[[], None] | None = None,
) -> dict | None:
    """Return the typed stale-runtime response for local in-process turns."""
    if external_runtime_owned is True:
        return None
    if gateway_owned is None:
        gateway_owned = lambda: webui_gateway_chat_enabled(get_config())
    if ensure_runtime_current is None:
        ensure_runtime_current = ensure_agent_runtime_current
    if runner_local_owned and gateway_owned():
        return None
    if runner_local_owned and runtime_adapter.runtime_adapter_runner_enabled():
        return None
    try:
        ensure_runtime_current()
    except AgentRuntimeChangedError as exc:
        return {
            "error": str(exc),
            "type": "agent_runtime_stale",
            "retryable": True,
        }
    return None


def start_chat_stream_for_session(
    session,
    *,
    message: str,
    workspace: str,
    model: str,
    clear_stale_stream: ClearStaleStream,
    attachments=None,
    model_provider=None,
    normalized_model: bool = False,
    diagnostics=None,
    goal_related: bool = False,
    source: str = "webui",
    moa_config=None,
    external_runtime_owned: bool | None = None,
    gateway_owned: Callable[[], bool] | None = None,
    runtime_barrier: Callable[..., dict | None] | None = None,
    local_worker: Callable | None = None,
    gateway_worker: Callable | None = None,
):
    """Select the execution owner and admit one local/gateway turn."""
    if gateway_owned is None:
        gateway_owned = lambda: webui_gateway_chat_enabled(get_config())
    if runtime_barrier is None:
        runtime_barrier = agent_runtime_barrier_response
    if local_worker is None:
        local_worker = run_agent_streaming
    if gateway_worker is None:
        gateway_worker = run_gateway_chat_streaming
    if external_runtime_owned is None:
        external_runtime_owned = gateway_owned()
    backend_is_gateway = bool(external_runtime_owned)
    stale_response = runtime_barrier(
        external_runtime_owned=backend_is_gateway,
    )
    if stale_response is not None:
        stale_response["_status"] = 409
        return stale_response
    worker_target = gateway_worker if backend_is_gateway else local_worker
    return start_local_turn(
        session,
        LocalTurnRequest(
            message=message,
            attachments=list(attachments or []),
            workspace=workspace,
            model=model,
            model_provider=model_provider,
            normalized_model=normalized_model,
            goal_related=goal_related,
            source=source,
            moa_config=moa_config if not backend_is_gateway else None,
        ),
        worker_target=worker_target,
        clear_stale_stream=clear_stale_stream,
        diag=diagnostics,
    )


def runtime_runner_client_factory():
    """Return the configured runner-local transport client."""
    from api.runner_client import HttpRunnerClient

    return HttpRunnerClient.from_env()


def chat_start_response_from_run_start(result) -> dict:
    """Project an adapter result onto the legacy browser response interface."""
    payload = dict(getattr(result, "payload", {}) or {})
    response = {
        key: payload[key]
        for key in (
            "stream_id",
            "session_id",
            "pending_started_at",
            "turn_id",
            "title",
            "effective_model",
            "effective_model_provider",
            "error",
            "active_stream_id",
            "_status",
        )
        if key in payload
    }
    response.setdefault("stream_id", result.stream_id)
    response.setdefault("session_id", result.session_id)
    return response


def runtime_adapter_goal_action(goal_args: str) -> str:
    """Return the bounded RuntimeAdapter goal action for WebUI arguments."""
    action = str(goal_args or "").strip().lower()
    if not action or action == "status":
        return "status"
    if action in ("pause", "resume"):
        return action
    if action in ("clear", "stop", "done"):
        return "clear"
    return "set"


def start_run(
    session,
    *,
    message: str,
    attachments,
    workspace: str,
    model,
    model_provider,
    normalized_model,
    source: str,
    route: str,
    clear_stale_stream: ClearStaleStream,
    diagnostics=None,
    moa_config=None,
    gateway_owned: Callable[[], bool] | None = None,
    start_stream: Callable[..., dict] | None = None,
    runner_client_factory: Callable[[], Any] | None = None,
) -> dict:
    """Start one prepared run through the configured runtime owner."""
    if gateway_owned is None:
        gateway_owned = lambda: webui_gateway_chat_enabled(get_config())
    if start_stream is None:
        start_stream = start_chat_stream_for_session
    if runner_client_factory is None:
        runner_client_factory = runtime_runner_client_factory
    if (
        runtime_adapter.runtime_adapter_enabled()
        or runtime_adapter.runtime_adapter_runner_enabled()
    ):

        def legacy_start_run(request: runtime_adapter.StartRunRequest) -> dict:
            return start_stream(
                session,
                message=request.message,
                attachments=request.attachments,
                workspace=request.workspace or workspace,
                model=request.model or model,
                model_provider=request.provider or model_provider,
                normalized_model=normalized_model,
                clear_stale_stream=clear_stale_stream,
                diagnostics=diagnostics,
                source=request.source or source,
                moa_config=moa_config,
            )

        def legacy_adapter_factory():
            return runtime_adapter.LegacyJournalRuntimeAdapter(
                start_run_delegate=legacy_start_run
            )

        try:
            adapter = runtime_adapter.build_runtime_adapter(
                legacy_adapter_factory=legacy_adapter_factory,
                runner_client_factory=runner_client_factory,
            )
            if adapter is None:
                raise NotImplementedError(
                    "runtime adapter selection returned no adapter"
                )
            result = adapter.start_run(
                runtime_adapter.StartRunRequest(
                    session_id=session.session_id,
                    message=message,
                    attachments=attachments,
                    workspace=workspace,
                    profile=getattr(session, "profile", None),
                    provider=model_provider,
                    model=model,
                    source=source,
                    metadata={"route": route},
                )
            )
        except NotImplementedError as exc:
            return {"error": str(exc), "_status": 501}
        return chat_start_response_from_run_start(result)

    return start_stream(
        session,
        message=message,
        attachments=attachments,
        workspace=workspace,
        model=model,
        model_provider=model_provider,
        normalized_model=normalized_model,
        clear_stale_stream=clear_stale_stream,
        diagnostics=diagnostics,
        source=source,
        moa_config=moa_config,
        external_runtime_owned=gateway_owned(),
    )
