"""Chat turn startup, runtime recovery, and synchronous completion lifecycle."""

# Implementations are rebound to the canonical facade for compatibility.
# ruff: noqa: F401, F821

from __future__ import annotations

from api.routes_parts.chat_controls import (
    _handle_bg_task_complete_ack,
    _handle_goal_command,
    _handle_session_compression_recovery_start,
)
from api.routes_parts.chat_turns import (
    _handle_chat_start,
    _handle_chat_sync,
    _normalize_chat_attachments,
    _resolve_chat_workspace_with_recovery,
)
from api.sessions import foreign_session_access


def _handle_sessions_cleanup(handler, body, zero_only=False):
    result = cleanup_session_store(zero_only=zero_only)
    return j(
        handler,
        {
            "ok": True,
            "cleaned": result.cleaned,
            "skipped_active": result.skipped_active,
        },
    )


def _handle_btw(handler, body):
    """POST /api/btw — ephemeral side question using session context.

    Creates a temporary hidden session, streams the answer via SSE, then
    discards the session. The parent session is not modified.
    """
    try:
        require(body, "session_id")
        require(body, "question")
    except ValueError as e:
        return bad(handler, str(e))
    stale_response = _agent_runtime_barrier_response(runner_local_owned=False)
    if stale_response is not None:
        return j(handler, stale_response, status=409)
    if foreign_session_access.is_view_only(str(body.get("session_id") or "")):
        return bad(handler, "Subagent sessions are view-only and cannot be used for /btw from WebUI", 400)
    try:
        s = get_session(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    question = str(body["question"]).strip()
    if not question:
        return bad(handler, "question is required")
    from api.runs.background import start_btw

    response = start_btw(s, question)
    status = int(response.pop("_status", 200) or 200)
    return j(handler, response, status=status)


def _handle_background(handler, body):
    """POST /api/background — run prompt in parallel background agent.

    Creates a hidden session, starts streaming in a daemon thread.
    Frontend polls /api/background/status for completed results.
    """
    try:
        require(body, "session_id")
        require(body, "prompt")
    except ValueError as e:
        return bad(handler, str(e))
    stale_response = _agent_runtime_barrier_response(runner_local_owned=False)
    if stale_response is not None:
        return j(handler, stale_response, status=409)
    try:
        s = get_session(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    prompt = str(body["prompt"]).strip()
    if not prompt:
        return bad(handler, "prompt is required")
    from api.runs.background import start_background

    return j(handler, start_background(s, prompt))


def _active_run_stream_for_session(session_id: str | None) -> str | None:
    """Return a live worker stream for this session even if sidecar stream id is clear.

    cancel_stream() intentionally clears ``session.active_stream_id`` before the
    worker thread fully exits so Stop remains responsive. During that unwind
    window ACTIVE_RUNS is the worker-lifecycle truth; a successor chat/start for
    the same session must wait or it can reuse the cached agent while the old
    interrupt is still landing (#3808).

    Bounded: the post-cancel unwind is short (dominated by the worker finally's
    ``_ckpt_thread.join(timeout=15)``), and ``unregister_active_run`` runs in that
    finally, so a healthy worker leaves ACTIVE_RUNS within seconds. A detached /
    wedged worker that never reaches its finally (e.g. stuck in a provider call,
    or leaked by SIGKILL without restart) must NOT 409 the session forever — so an
    entry older than the unwind ceiling (180s) is treated as stale and ignored
    here. A legitimately long-running turn keeps ``active_stream_id`` SET
    and is handled by turn admission; this helper covers only the
    cleared-stream-id unwind window. (Codex brick-gate hardening, #3822.)
    """
    from api.runs.turn_start import active_run_stream_for_session

    return active_run_stream_for_session(session_id)


def _agent_runtime_barrier_response(
    *,
    runner_local_owned: bool = False,
    external_runtime_owned: bool | None = None,
) -> dict | None:
    """Return the typed stale-runtime response for local in-process turns."""
    from api.runs.turn_start import agent_runtime_barrier_response

    return agent_runtime_barrier_response(
        runner_local_owned=runner_local_owned,
        external_runtime_owned=external_runtime_owned,
        gateway_owned=lambda: webui_gateway_chat_enabled(get_config()),
        ensure_runtime_current=ensure_agent_runtime_current,
    )


def _start_chat_stream_for_session(
    s,
    *,
    msg: str,
    attachments=None,
    workspace: str,
    model: str,
    model_provider=None,
    normalized_model: bool = False,
    diag=None,
    goal_related: bool = False,
    source: str = "webui",
    moa_config=None,
    external_runtime_owned: bool | None = None,
):
    """Select the execution owner and delegate local admission as one transition."""
    from api.runs.turn_start import start_chat_stream_for_session

    return start_chat_stream_for_session(
        s,
        message=msg,
        attachments=attachments,
        workspace=workspace,
        model=model,
        model_provider=model_provider,
        normalized_model=normalized_model,
        clear_stale_stream=_clear_stale_stream_state,
        diagnostics=diag,
        goal_related=goal_related,
        source=source,
        moa_config=moa_config,
        external_runtime_owned=external_runtime_owned,
        gateway_owned=lambda: webui_gateway_chat_enabled(get_config()),
        runtime_barrier=_agent_runtime_barrier_response,
        local_worker=_run_agent_streaming,
        gateway_worker=_run_gateway_chat_streaming,
    )


def _runtime_runner_client_factory():
    """Return the configured runner-local client.

    `runner-local` remains default-off and bounded: without an explicit runner
    endpoint this factory preserves the existing "runner-local chat backend is
    not configured" 501 path. When
    `HERMES_WEBUI_RUNNER_BASE_URL` is set, the WebUI process only acts as a
    transport client; the runner endpoint owns execution, run ids, replay, and
    controls.
    """
    from api.runs.turn_start import runtime_runner_client_factory

    return runtime_runner_client_factory()


def _chat_start_response_from_run_start(result):
    """Expose only the legacy browser-facing chat-start response fields."""
    from api.runs.turn_start import chat_start_response_from_run_start

    return chat_start_response_from_run_start(result)


def _runtime_adapter_goal_action(goal_args: str) -> str:
    """Return the bounded RuntimeAdapter goal action for WebUI /goal args."""
    from api.runs.turn_start import runtime_adapter_goal_action

    return runtime_adapter_goal_action(goal_args)


def _start_run(
    s,
    *,
    msg: str,
    attachments,
    workspace: str,
    model,
    model_provider,
    normalized_model,
    source: str,
    route: str,
    diag=None,
    moa_config=None,
):
    """Shared start-run helper for /api/chat/start and start_session_turn.

    Centralizes the runtime-adapter selection block (Q-2979-A2 / Copilot
    discussion_r3305864087/r3305864173) so both entrypoints honor
    ``runtime_adapter_enabled()`` / ``runtime_adapter_runner_enabled()`` the
    same way. Prior to this helper ``start_session_turn`` bypassed the
    adapter path entirely, so a process-wakeup turn skipped the adapter that
    a human-typed turn would have hit — a behavioral divergence.

    ``source`` is the StartRunRequest.source (``"webui"`` for browser POSTs,
    ``"process_wakeup"`` for the drain-thread wakeup). ``route`` is the
    metadata.route label that lands on the run record for observability.

    Returns a dict with ``_status`` plus the legacy chat-start response
    fields (``stream_id``, ``session_id``, etc.). Adapter selection that
    returns no adapter is surfaced as ``{"error": str(exc), "_status": 501}``
    so both call sites can map it onto their own HTTP shape.
    """
    from api.runs.turn_start import start_run

    def _start_stream(session, **kwargs):
        message = kwargs.pop("message")
        diagnostics = kwargs.pop("diagnostics", None)
        kwargs.pop("clear_stale_stream", None)
        return _start_chat_stream_for_session(
            session,
            msg=message,
            diag=diagnostics,
            **kwargs,
        )

    return start_run(
        s,
        message=msg,
        attachments=attachments,
        workspace=workspace,
        model=model,
        model_provider=model_provider,
        normalized_model=normalized_model,
        source=source,
        route=route,
        clear_stale_stream=_clear_stale_stream_state,
        diagnostics=diag,
        moa_config=moa_config,
        gateway_owned=lambda: webui_gateway_chat_enabled(get_config()),
        start_stream=_start_stream,
        runner_client_factory=_runtime_runner_client_factory,
    )


def _process_wakeup_revalidation_provider(model, provider) -> str:
    """Compatibility alias for the run-owned wakeup policy."""
    from api.runs.process_wakeup import revalidation_provider

    return revalidation_provider(model, provider)


def _process_wakeup_provider_has_recovery_credential(
    session,
    *,
    model,
    provider,
    provider_id: str | None = None,
) -> bool:
    """Compatibility alias for profile-scoped credential revalidation."""
    from api.runs.process_wakeup import provider_has_recovery_credential

    return provider_has_recovery_credential(
        session,
        model=model,
        provider=provider,
        provider_id=provider_id,
        credential_available=provider_has_process_wakeup_recovery_credential,
    )


def _refresh_process_wakeup_pause_credential_fingerprint(session) -> bool:
    """Compatibility alias for the run-owned pause fingerprint update."""
    from api.runs.process_wakeup import refresh_pause_credential_fingerprint

    return refresh_pause_credential_fingerprint(session)


def start_session_turn(
    session_id: str,
    message: str,
    *,
    source: str = "process_wakeup",
):
    """Compose the remaining route-owned model seam into the run owner."""
    from api.runs.server_turn import (
        ServerTurnDependencies,
        start_prepared_session_turn,
    )
    from api.runs.process_wakeup import revalidate_process_wakeup

    def _revalidate_wakeup(session_id, **kwargs):
        return revalidate_process_wakeup(
            session_id,
            load_session=get_session,
            recovery_probe=_process_wakeup_provider_has_recovery_credential,
            **kwargs,
        )

    def _start_prepared_run(session, **kwargs):
        message = kwargs.pop("message")
        diagnostics = kwargs.pop("diagnostics", None)
        kwargs.pop("clear_stale_stream", None)
        return _start_run(
            session,
            msg=message,
            diag=diagnostics,
            **kwargs,
        )

    return start_prepared_session_turn(
        session_id,
        message,
        source=source,
        dependencies=ServerTurnDependencies(
            read_profile_model_config=_read_profile_model_config,
            resolve_model_state=_resolve_compatible_session_model_state,
            clear_stale_stream=_clear_stale_stream_state,
            runtime_barrier=_agent_runtime_barrier_response,
            load_session=get_session,
            resolve_workspace=_resolve_chat_workspace_with_recovery,
            revalidate_wakeup=_revalidate_wakeup,
            start_run=_start_prepared_run,
        ),
    )





__routes_exports__ = (
    "_handle_sessions_cleanup",
    "_handle_btw",
    "_handle_background",
    "_active_run_stream_for_session",
    "_agent_runtime_barrier_response",
    "_start_chat_stream_for_session",
    "_runtime_runner_client_factory",
    "_chat_start_response_from_run_start",
    "_runtime_adapter_goal_action",
    "_start_run",
    "_process_wakeup_revalidation_provider",
    "_process_wakeup_provider_has_recovery_credential",
    "_refresh_process_wakeup_pause_credential_fingerprint",
    "start_session_turn",
    "_handle_bg_task_complete_ack",
    "_handle_session_compression_recovery_start",
    "_handle_goal_command",
    "_handle_chat_start",
    "_resolve_chat_workspace_with_recovery",
    "_normalize_chat_attachments",
    "_handle_chat_sync",
)
