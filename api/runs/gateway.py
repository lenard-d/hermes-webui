"""Run orchestration for Gateway-backed browser chat turns."""

from __future__ import annotations

import logging
import urllib.error

from api.config import gateway_approval_unavailable_reason, gateway_supports_approval
from api.session_state import PENDING_GOAL_CONTINUATION
from api.helpers import _redact_text, redact_session_data
from api.sessions import get_session

from .execution import TurnExecution
from .gateway_config import (
    gateway_api_key,
    gateway_base_url,
    gateway_chat_config_status,
    gateway_http_error_event,
    gateway_reasoning_effort_for_request,
    gateway_use_runs_api_enabled,
    webui_chat_backend_mode,
    webui_gateway_chat_enabled,
)
from .gateway_runtime import release_gateway_run
from .gateway_settlement import (
    cleanup_gateway_pending_mirror,
    clear_gateway_pending_state,
    settle_gateway_cancel,
    settle_gateway_success,
    settle_gateway_terminal_error,
)
from .gateway_transport import (
    stream_gateway_chat_completions,
    stream_gateway_runs_api,
)
from .payloads import _session_payload_with_full_messages
from .prompts import _webui_ephemeral_system_prompt
from .webui_prefill import (
    _load_webui_prefill_context,
    _normalize_prefill_messages_before_user_turn,
    _prefill_messages_with_webui_context,
    _public_prefill_context_status,
)


logger = logging.getLogger(__name__)


def _gateway_prefill_messages(session_id, session, workspace, cfg, publish):
    """Build Gateway prefill through the same WebUI context seam as local runs."""
    try:
        prefill_context = _load_webui_prefill_context(cfg)
        system_prompt = _webui_ephemeral_system_prompt(
            None,
            surface_context={
                "source": "webui",
                "session_id": session_id,
                "profile": getattr(session, "profile", None),
                "workspace": (
                    session.workspace if session is not None else str(workspace)
                ),
            },
            config_data=cfg,
        )
        messages = _prefill_messages_with_webui_context(prefill_context, cfg)
        messages = _normalize_prefill_messages_before_user_turn(messages)
        publish(
            "context_status",
            {
                "session_id": session_id,
                "prefill": _public_prefill_context_status(prefill_context),
            },
        )
        return [{"role": "system", "content": system_prompt}, *messages]
    except Exception:
        logger.debug(
            "Failed to load WebUI Gateway prefill context",
            exc_info=True,
        )
        return []


def _gateway_request_extras(
    cfg,
    *,
    model,
    model_provider,
    reasoning_effort,
) -> dict:
    """Resolve authoritative provider/model overrides once per Gateway turn."""
    try:
        from api.config import main_model_request_overrides

        overrides = main_model_request_overrides(
            cfg,
            effective_model=model,
            effective_provider=model_provider,
        )
    except Exception:
        overrides = {}
    extras = {}
    if model_provider:
        extras["provider"] = model_provider
    if reasoning_effort is not None:
        extras["reasoning_effort"] = reasoning_effort
    if overrides.get("service_tier"):
        extras["service_tier"] = overrides["service_tier"]
    return extras


def _publish_gateway_approval_capability_warning(
    session,
    *,
    base_url,
    api_key,
    publish,
) -> None:
    """Publish the legacy-transport approval warning at most once per session."""
    approval_reason = gateway_approval_unavailable_reason(base_url, api_key)
    if approval_reason is None:
        return
    if not hasattr(session, "_approval_notice_emitted"):
        session._approval_notice_emitted = False
    if session._approval_notice_emitted:
        return
    approval_type = "approval_gateway_unsupported"
    approval_message = (
        "Approvals require a newer gateway. Upgrade the connected Hermes "
        "gateway to enable this."
    )
    if approval_reason == "unreachable":
        approval_type = "approval_gateway_offline"
        approval_message = (
            "Gateway connection failed. Check that the connected Hermes "
            "gateway is running and reachable."
        )
    publish(
        "warning",
        {"type": approval_type, "message": approval_message},
    )
    session._approval_notice_emitted = True


def _evaluate_gateway_goal_continuation(
    session_id,
    session,
    assistant_text,
    *,
    goal_related,
    publish,
) -> None:
    """Evaluate and publish the existing post-turn goal continuation contract."""
    try:
        from api.goals import evaluate_goal_after_turn, has_active_goal
        from api.profiles import get_hermes_home_for_profile

        profile_home = get_hermes_home_for_profile(
            getattr(session, "profile", None)
        )
        if not goal_related or not has_active_goal(
            session_id,
            profile_home=profile_home,
        ):
            return
        publish(
            "goal",
            {
                "session_id": session_id,
                "state": "evaluating",
                "message": "Evaluating goal progress…",
                "message_key": "goal_evaluating_progress",
            },
        )
        decision = evaluate_goal_after_turn(
            session_id,
            assistant_text,
            user_initiated=True,
            profile_home=profile_home,
        ) or {}
        goal_message = str(decision.get("message") or "").strip()
        if goal_message:
            publish(
                "goal",
                {
                    "session_id": session_id,
                    "state": (
                        "continuing" if decision.get("should_continue") else "idle"
                    ),
                    "message": goal_message,
                    "message_key": decision.get("message_key")
                    or "goal_continuing",
                    "message_args": decision.get("message_args") or [],
                    "decision": decision,
                },
            )
        if not decision.get("should_continue"):
            return
        continuation_prompt = str(
            decision.get("continuation_prompt") or ""
        ).strip()
        if not continuation_prompt:
            return
        PENDING_GOAL_CONTINUATION.add(session_id)
        publish(
            "goal_continue",
            {
                "session_id": session_id,
                "continuation_prompt": continuation_prompt,
                "text": continuation_prompt,
                "message": goal_message,
                "message_key": decision.get("message_key") or "goal_continuing",
                "message_args": decision.get("message_args") or [],
                "decision": decision,
            },
        )
    except Exception as exc:
        logger.debug(
            "Gateway goal continuation hook failed for session %s: %s",
            session_id,
            exc,
        )


def _run_gateway_chat_streaming(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    model_provider=None,
    goal_related=False,
):
    """Run one WebUI chat turn through the configured Hermes Gateway."""
    execution = TurnExecution.start(
        stream_id=stream_id,
        session_id=session_id,
        phase="gateway-starting",
        logger=logger,
        log_label="gateway run",
        workspace=str(workspace),
        model=model,
        provider=model_provider,
        backend="gateway",
        record_worker_started=True,
    )
    if execution is None:
        return
    cancel_event = execution.cancel_event
    success_writeback_committed = False
    session = None
    preserve_pending_after_settlement_failure = False

    def publish(event, data):
        if (
            cancel_event.is_set()
            and not success_writeback_committed
            and event not in ("cancel", "error", "apperror")
        ):
            return
        if event == "apperror" and isinstance(data, dict):
            data = dict(data)
            data.setdefault("session_id", session_id)
        execution.event_sink.publish(event, data)

    def settle_and_publish_error(terminal_message, *, event_payload=None):
        nonlocal preserve_pending_after_settlement_failure
        try:
            settled = settle_gateway_terminal_error(
                session_id,
                stream_id,
                workspace,
                model,
                model_provider,
                terminal_message,
                event_payload=event_payload,
                session=session,
            )
        except Exception:
            preserve_pending_after_settlement_failure = True
            logger.exception("Failed to persist Gateway terminal settlement")
            publish(
                "apperror",
                {
                    "label": "Gateway session persistence failed",
                    "type": "gateway_persistence_error",
                    "message": "The gateway turn could not be saved safely.",
                    "hint": (
                        "Reload the session before retrying so recovery can "
                        "reconcile the pending turn."
                    ),
                },
            )
            return False
        if settled is None:
            return False
        publish("apperror", settled)
        return True

    try:
        session = get_session(session_id)
        from api.config import get_config

        cfg = get_config()
        reasoning_effort = gateway_reasoning_effort_for_request(
            cfg,
            model=model,
            model_provider=model_provider,
        )
        prefill_messages = _gateway_prefill_messages(
            session_id,
            session,
            workspace,
            cfg,
            publish,
        )
        base_url = gateway_base_url(cfg)
        api_key = gateway_api_key()
        request_extras = _gateway_request_extras(
            cfg,
            model=model,
            model_provider=model_provider,
            reasoning_effort=reasoning_effort,
        )
        use_runs_api = gateway_use_runs_api_enabled(
            cfg
        ) and gateway_supports_approval(base_url, api_key)
        if use_runs_api:
            try:
                result = stream_gateway_runs_api(
                    session_id,
                    msg_text,
                    model,
                    workspace,
                    stream_id,
                    base_url,
                    api_key,
                    prefill_messages,
                    request_extras,
                    publish=publish,
                    cancel_event=cancel_event,
                    attachments=attachments,
                    cfg=cfg,
                    session=session,
                )
            except Exception as exc:
                settle_and_publish_error(str(exc))
                return
        else:
            _publish_gateway_approval_capability_warning(
                session,
                base_url=base_url,
                api_key=api_key,
                publish=publish,
            )
            result = stream_gateway_chat_completions(
                session_id,
                msg_text,
                model,
                workspace,
                stream_id,
                base_url,
                api_key,
                prefill_messages,
                request_extras,
                publish=publish,
                cancel_event=cancel_event,
                attachments=attachments,
                cfg=cfg,
            )

        if result.cancelled:
            settle_gateway_cancel(
                session_id,
                stream_id,
                session=session,
                message=result.cancel_message or "Cancelled by gateway",
            )
            publish(
                "cancel",
                {"message": result.cancel_message or "Cancelled by gateway"},
            )
            return
        if result.terminal_error:
            settle_and_publish_error(result.terminal_error)
            return
        assistant_text = result.final_text.strip()
        if not assistant_text:
            settle_and_publish_error(
                "Gateway returned no assistant message for this turn.",
                event_payload={
                    "label": "Gateway returned no response",
                    "type": "gateway_empty_response",
                    "message": (
                        "Gateway returned no assistant message for this turn."
                    ),
                    "hint": (
                        "Check that Hermes Gateway API server is running and "
                        "reachable."
                    ),
                },
            )
            return

        settlement = settle_gateway_success(
            session_id=session_id,
            stream_id=stream_id,
            session=session,
            msg_text=msg_text,
            assistant_text=assistant_text,
            workspace=workspace,
            model=model,
            model_provider=model_provider,
            attachments=attachments,
            cancel_event=cancel_event,
        )
        if settlement is None:
            return
        session = settlement.session
        success_writeback_committed = settlement.success_committed
        if settlement.cancelled:
            publish("cancel", {"message": "Cancelled by user"})
            return

        _evaluate_gateway_goal_continuation(
            session_id,
            session,
            assistant_text,
            goal_related=goal_related,
            publish=publish,
        )
        session_payload = _session_payload_with_full_messages(
            session,
            tool_calls=[],
        )
        publish(
            "done",
            {
                "session": redact_session_data(session_payload),
                "usage": result.usage,
            },
        )
        publish("stream_end", {"session_id": session_id})
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        error_payload = gateway_http_error_event(
            exc,
            err_body,
            api_key_configured=bool(gateway_api_key()),
        )
        settle_and_publish_error(
            str(error_payload.get("message") or exc),
            event_payload=error_payload,
        )
    except Exception as exc:
        safe = _redact_text(str(exc))[:500]
        settle_and_publish_error(
            safe or "Gateway request failed.",
            event_payload={
                "label": "Gateway request failed",
                "type": "gateway_error",
                "message": safe or "Gateway request failed.",
                "hint": (
                    "Check HERMES_WEBUI_GATEWAY_BASE_URL and Gateway API "
                    "server health."
                ),
            },
        )
    finally:
        if session is not None and not preserve_pending_after_settlement_failure:
            try:
                clear_gateway_pending_state(
                    session_id,
                    stream_id,
                    session=session,
                )
            except Exception:
                logger.debug(
                    "Failed to clear Gateway stream state",
                    exc_info=True,
                )
            cleanup_gateway_pending_mirror(session_id)
        try:
            execution.finish()
        finally:
            release_gateway_run(stream_id)


__all__ = [
    "gateway_chat_config_status",
    "webui_chat_backend_mode",
    "webui_gateway_chat_enabled",
]
