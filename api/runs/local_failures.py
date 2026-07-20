"""Failure classification, credential recovery, and durable error projection."""

from __future__ import annotations

import contextlib
import logging
import re
import time
from dataclasses import dataclass

from api.agent_cache import locked_agent_cache
from api.compression_recovery import stamp_compression_exhausted_recovery
from api.config import attach_runtime_agent
from api.helpers import redact_session_data
from api.sessions.process_wakeup import record_process_wakeup_provider_unavailable_pause
from api.turn_journal import append_turn_journal_event_for_stream

from .agent_cache import _replace_session_db_in_kwargs
from .local_result import merge_local_result
from .message_sanitization import _sanitize_messages_for_api
from .payloads import _cancel_event_payload
from .provider_errors import _provider_error_payload
from .runtime_resolution import (
    _resolve_custom_provider_runtime_overrides,
    _runtime_preferred_base_url,
)
from .terminal_outcomes import (
    _finalize_cancelled_turn,
    _mark_latest_assistant_tool_limit_status,
)
from .transcript import (
    _agent_result_terminal_failure,
    _assistant_reply_added_after_current_turn,
    _has_new_assistant_reply,
    _materialize_pending_user_turn_before_error,
    _merged_transcript_lacks_final_assistant_answer,
    _session_lacks_final_assistant_answer,
    _snapshot_and_append_partial_on_error,
)
from .turn_context import _stream_writeback_is_current


@dataclass
class LocalFailureContext:
    session: object
    session_id: str
    stream_id: str
    message_text: str
    pending_source: str
    route_model: str
    route_provider: str | None
    ephemeral: bool
    cancel_event: object
    checkpoint: object
    session_lock: object | None
    publish: object
    classify: object
    session_payload: object
    credential_self_heal: object
    prepared_agent: object
    conversation: object
    event_translator: object
    previous_messages: list
    previous_context_messages: list
    logger: logging.Logger


@dataclass(frozen=True)
class LocalFailureOutcome:
    handled: bool
    result: dict
    agent: object
    provider: str | None
    base_url: str | None
    api_key: str | None
    tool_limit_reached: bool


class LocalFailureOwner:
    """Own every terminal/error exit and the single credential retry."""

    def __init__(self, context: LocalFailureContext) -> None:
        self.ctx = context
        self._self_healed = False

    def inspect_terminal_result(
        self,
        result: dict,
        *,
        agent,
        tool_limit_reached: bool,
        captured_terminal_error,
        compression_origin_id: str,
        compression_continuation_id: str | None,
    ) -> LocalFailureOutcome:
        messages = result.get("messages") or []
        assistant_added = _assistant_reply_added_after_current_turn(
            messages,
            self.ctx.previous_context_messages,
            self.ctx.message_text,
        )
        last_error = getattr(agent, "_last_error", None) or result.get("error") or ""
        captured_failure = bool(captured_terminal_error[0])
        if not last_error and captured_failure:
            last_error = captured_terminal_error[0]
        classification = self.ctx.classify(
            str(last_error) if last_error else "",
            last_error,
            silent_failure=not bool(last_error),
        )
        result_terminal = _agent_result_terminal_failure(result)
        lacks_answer = _merged_transcript_lacks_final_assistant_answer(
            self.ctx.previous_messages,
            self.ctx.previous_context_messages,
            messages,
            self.ctx.message_text,
            source=getattr(self.ctx.session, "pending_user_source", None) or "webui",
            drop_replayed_assistant=(
                captured_failure
                or result_terminal
                or bool(getattr(agent, "_last_error", None))
                or ("error" in result and result.get("error") is not None)
            ),
        )
        terminal_failure = captured_failure or result_terminal or (
            lacks_answer and classification["type"] not in {"cancelled", "interrupted"}
        )
        status = str(result.get("status") or result.get("state") or "").strip().lower()
        soft_partial = (
            result_terminal
            and (status == "partial" or bool(result.get("partial")))
            and status not in {"failed", "error", "compression_exhausted"}
            and not result.get("failed")
            and not result.get("compression_exhausted")
            and not tool_limit_reached
            and not last_error
        )
        if (
            terminal_failure
            and (soft_partial or tool_limit_reached)
            and classification["type"] == "no_response"
            and not lacks_answer
        ):
            terminal_failure = False
        if terminal_failure:
            assistant_added = False
        elif tool_limit_reached and not _session_lacks_final_assistant_answer(
            self.ctx.session.messages
        ):
            _mark_latest_assistant_tool_limit_status(self.ctx.session.messages)

        if not terminal_failure and (
            assistant_added or self.ctx.event_translator.token_sent
        ):
            return self._outcome(False, result, agent, tool_limit_reached)
        if self.ctx.cancel_event.is_set():
            self._cancel(lock_held=True)
            return self._outcome(True, result, agent, tool_limit_reached)

        if classification["type"] == "auth_mismatch" and not self._self_healed:
            healed = self._retry_with_fresh_credentials(agent)
            if healed is not None:
                healed_agent, healed_result = healed
                merged = merge_local_result(
                    self.ctx.session,
                    healed_result,
                    previous_messages=self.ctx.previous_messages,
                    previous_context_messages=self.ctx.previous_context_messages,
                    message_text=self.ctx.message_text,
                )
                return self._outcome(
                    False,
                    merged.result,
                    healed_agent,
                    merged.tool_limit_reached,
                )

        label, error_type, hint, message = _terminal_error_description(
            classification,
            tool_limit_reached=tool_limit_reached,
            raw_error=str(last_error or ""),
        )
        self._persist_error(
            label=label,
            error_type=error_type,
            hint=hint,
            message=message,
            include_session=True,
            old_session_id=compression_origin_id,
            continuation_session_id=compression_continuation_id,
            tool_limit_reached=tool_limit_reached,
            lock_held=True,
        )
        return self._outcome(True, result, agent, tool_limit_reached)

    def handle_exception(self, exc: Exception, *, agent) -> bool:
        """Classify and persist an exception raised by any local-run phase."""

        message = _sanitize_provider_exception(str(exc))
        classification = self.ctx.classify(message, exc)
        if self.ctx.cancel_event.is_set():
            if self.ctx.session is not None:
                if (
                    not self.ctx.ephemeral
                    and self.ctx.pending_source == "process_wakeup"
                    and classification["type"] == "credential_pool_empty"
                ):
                    record_process_wakeup_provider_unavailable_pause(
                        self.ctx.session,
                        classification=classification["type"],
                        model=self.ctx.route_model,
                        provider=self.ctx.route_provider,
                    )
                self._cancel()
            else:
                self.ctx.publish("cancel", _cancel_event_payload("Cancelled by user"))
            return True

        if classification["type"] == "auth_mismatch" and not self._self_healed:
            healed = self._retry_with_fresh_credentials(agent)
            if healed is not None:
                healed_agent, healed_result = healed
                if self.ctx.session is not None:
                    self.ctx.checkpoint.close()
                    lock = self.ctx.session_lock or contextlib.nullcontext()
                    with lock:
                        if not self.ctx.ephemeral and not _stream_writeback_is_current(
                            self.ctx.session,
                            self.ctx.stream_id,
                        ):
                            return True
                        merge_local_result(
                            self.ctx.session,
                            healed_result,
                            previous_messages=self.ctx.previous_messages,
                            previous_context_messages=self.ctx.previous_context_messages,
                            message_text=self.ctx.message_text,
                        )
                        self.ctx.session.save()
                self.ctx.logger.info("local credential self-heal retry succeeded")
                return True

        label, error_type, hint = _exception_error_description(classification)
        self._persist_error(
            label=label,
            error_type=error_type,
            hint=hint,
            message=message,
            include_session=False,
            old_session_id=self.ctx.session_id,
            continuation_session_id=None,
            tool_limit_reached=False,
            append_interrupted=True,
        )
        return True

    def _retry_with_fresh_credentials(self, agent):
        prepared = self.ctx.prepared_agent
        if prepared is None or self.ctx.conversation is None:
            return None
        runtime = self.ctx.credential_self_heal(
            prepared.provider or "",
            self.ctx.session_id,
            self.ctx.session_lock,
            target_model=prepared.model,
        )
        if runtime is None:
            return None
        self._self_healed = True
        provider = prepared.provider or runtime.get("provider")
        api_key = runtime.get("api_key")
        base_url = _runtime_preferred_base_url(
            runtime,
            provider,
            prepared.configured_base_url,
        )
        provider, api_key, base_url = _resolve_custom_provider_runtime_overrides(
            provider,
            api_key,
            base_url,
        )
        kwargs = dict(prepared.kwargs)
        kwargs.update(
            api_key=api_key,
            base_url=base_url,
            model=prepared.model,
            provider=provider,
        )
        _replace_session_db_in_kwargs(kwargs, prepared.state_db_path)
        if "credential_pool" in prepared.parameters:
            kwargs["credential_pool"] = runtime.get("credential_pool")
        healed_agent = prepared.agent_class(**kwargs)
        if not attach_runtime_agent(self.ctx.stream_id, healed_agent):
            try:
                healed_agent.interrupt("Cancelled during agent replacement")
            except Exception:
                self.ctx.logger.debug("Failed to interrupt replacement agent")
            return None
        with locked_agent_cache() as session_agent_cache:
            session_agent_cache[self.ctx.session_id] = (
                healed_agent,
                prepared.signature,
            )
            session_agent_cache.move_to_end(self.ctx.session_id)
        self.ctx.event_translator.token_sent = False
        retry_kwargs = dict(self.ctx.conversation.kwargs)
        retry_kwargs["conversation_history"] = _sanitize_messages_for_api(
            self.ctx.previous_context_messages,
            cfg=prepared.config,
            effective_model=prepared.model,
            effective_provider=provider,
            effective_base_url=base_url,
        )
        try:
            result = healed_agent.run_conversation(**retry_kwargs)
        except Exception as retry_exc:
            self.ctx.logger.warning("local credential self-heal retry failed: %s", retry_exc)
            return None
        if not (
            _has_new_assistant_reply(
                result.get("messages") or [],
                len(self.ctx.previous_context_messages),
            )
            or self.ctx.event_translator.token_sent
        ):
            return None
        prepared.agent = healed_agent
        prepared.provider = provider
        prepared.api_key = api_key
        prepared.base_url = base_url
        prepared.runtime = runtime
        return healed_agent, result

    def _persist_error(
        self,
        *,
        label: str,
        error_type: str,
        hint: str,
        message: str,
        include_session: bool,
        old_session_id: str,
        continuation_session_id: str | None,
        tool_limit_reached: bool,
        append_interrupted: bool = False,
        lock_held: bool = False,
    ) -> None:
        payload = _provider_error_payload(message or f"{label}.", error_type, hint)
        session = self.ctx.session
        if session is not None:
            self.ctx.checkpoint.close()
            lock = (
                contextlib.nullcontext()
                if lock_held
                else self.ctx.session_lock or contextlib.nullcontext()
            )
            with lock:
                if not self.ctx.ephemeral and not _stream_writeback_is_current(
                    session,
                    self.ctx.stream_id,
                ):
                    if self.ctx.pending_source == "process_wakeup":
                        pause = record_process_wakeup_provider_unavailable_pause(
                            session,
                            classification=error_type,
                            model=self.ctx.route_model,
                            provider=self.ctx.route_provider,
                        )
                        if pause is not None:
                            try:
                                session.save(touch_updated_at=False)
                            except Exception:
                                self.ctx.logger.debug(
                                    "Failed to persist stale-stream process wakeup pause",
                                    exc_info=True,
                                )
                    return
                hint = self._record_process_wakeup_pause(
                    session,
                    error_type=error_type,
                    hint=hint,
                    payload=payload,
                )
                _materialize_pending_user_turn_before_error(session)
                _clear_pending_turn(session)
                try:
                    _snapshot_and_append_partial_on_error(session, self.ctx.stream_id)
                except Exception:
                    self.ctx.logger.debug(
                        "Failed to snapshot partials on error for %s",
                        self.ctx.stream_id,
                        exc_info=True,
                    )
                error_message = {
                    "role": "assistant",
                    "content": f"**{label}:** {payload.get('message') or label}"
                    + (f"\n\n*{hint}*" if hint else ""),
                    "timestamp": int(time.time()),
                    "_error": True,
                }
                if error_type == "compression_exhausted":
                    recovery = stamp_compression_exhausted_recovery(
                        session,
                        message=payload.get("message") or label,
                        details=payload.get("details") or "",
                    )
                    error_message["_compressionRecovery"] = recovery
                    payload["compression_recovery"] = recovery
                    payload["recommended_recovery_action"] = recovery.get(
                        "recommended_action"
                    )
                if payload.get("details"):
                    error_message["provider_details"] = payload["details"]
                details_label = {
                    "cancelled": "Cancellation details",
                    "interrupted": "Interruption details",
                    "tool_limit_reached": "Terminal state details",
                }.get(error_type)
                if details_label:
                    error_message["provider_details_label"] = details_label
                session.messages.append(error_message)
                try:
                    session.save()
                except Exception:
                    pass
                if append_interrupted and not self.ctx.ephemeral:
                    _append_interrupted(
                        session,
                        self.ctx.stream_id,
                        reason=error_type,
                        logger=self.ctx.logger,
                    )
            payload["session_id"] = getattr(session, "session_id", self.ctx.session_id)
            payload["old_session_id"] = old_session_id
            if include_session:
                payload["session"] = redact_session_data(
                    self.ctx.session_payload(session, tool_calls=session.tool_calls)
                )
            if continuation_session_id is not None:
                payload["new_session_id"] = continuation_session_id
                payload["continuation_session_id"] = continuation_session_id
        if tool_limit_reached:
            payload["terminal_state"] = "tool_limit_reached"
            payload["terminal_reason"] = "max_iterations"
        self.ctx.publish("apperror", payload)

    def _record_process_wakeup_pause(
        self,
        session,
        *,
        error_type: str,
        hint: str,
        payload: dict,
    ) -> str:
        if self.ctx.pending_source != "process_wakeup":
            return hint
        pause = record_process_wakeup_provider_unavailable_pause(
            session,
            classification=error_type,
            model=self.ctx.route_model,
            provider=self.ctx.route_provider,
        )
        if not pause:
            return hint
        suffix = (
            "Automatic retries for this conversation are paused until you send a "
            "message, switch the model/provider, or fix the credentials."
        )
        hint = f"{hint} {suffix}".strip()
        payload["hint"] = hint
        return hint

    def _cancel(self, *, lock_held: bool = False) -> None:
        session = self.ctx.session
        if session is not None:
            self.ctx.checkpoint.close()
            lock = (
                contextlib.nullcontext()
                if lock_held
                else self.ctx.session_lock or contextlib.nullcontext()
            )
            with lock:
                _finalize_cancelled_turn(session, ephemeral=self.ctx.ephemeral)
                if not self.ctx.ephemeral:
                    _append_interrupted(
                        session,
                        self.ctx.stream_id,
                        reason="cancelled",
                        logger=self.ctx.logger,
                    )
        self.ctx.publish("cancel", _cancel_event_payload("Cancelled by user"))

    def _outcome(self, handled, result, agent, tool_limit) -> LocalFailureOutcome:
        prepared = self.ctx.prepared_agent
        return LocalFailureOutcome(
            handled=handled,
            result=result,
            agent=agent,
            provider=prepared.provider,
            base_url=prepared.base_url,
            api_key=prepared.api_key,
            tool_limit_reached=tool_limit,
        )


def _terminal_error_description(classification, *, tool_limit_reached, raw_error):
    if tool_limit_reached:
        return (
            "Tool iteration limit reached",
            "tool_limit_reached",
            "The agent reached its configured tool iteration limit before producing "
            "a final answer. Start a narrower follow-up or increase agent.max_turns.",
            "The agent reached its configured tool iteration limit before producing "
            "a final answer.",
        )
    if classification["type"] == "auth_mismatch":
        return (
            "Authentication failed",
            "auth_mismatch",
            "The selected model may not be supported by your configured provider or "
            "your API key is invalid. Run `hermes model` in your terminal to update "
            "credentials, then restart the WebUI.",
            raw_error,
        )
    return (
        classification["label"],
        classification["type"],
        classification["hint"],
        raw_error,
    )


def _exception_error_description(classification):
    if classification["type"] in {
        "quota_exhausted",
        "credential_pool_empty",
        "rate_limit",
        "model_not_found",
        "cancelled",
        "interrupted",
        "compression_exhausted",
    }:
        return classification["label"], classification["type"], classification["hint"]
    if classification["type"] == "auth_mismatch":
        return (
            "Authentication error",
            "auth_mismatch",
            "The selected model may not be supported by your configured provider. "
            "Run `hermes model` in your terminal to switch providers, then restart the WebUI.",
        )
    return "Error", "error", ""


def _sanitize_provider_exception(message: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", message)
    return re.sub(r"\s+", " ", stripped).strip()


def _clear_pending_turn(session) -> None:
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None


def _append_interrupted(session, stream_id: str, *, reason: str, logger) -> None:
    try:
        append_turn_journal_event_for_stream(
            session.session_id,
            stream_id,
            {"event": "interrupted", "created_at": time.time(), "reason": reason},
        )
    except Exception:
        logger.debug("Failed to append interrupted turn journal event", exc_info=True)
