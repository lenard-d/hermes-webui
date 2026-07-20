"""Durable and live projections of a successful local agent turn."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from api.helpers import redact_session_data
from api.metering import meter
from api.session_state import PENDING_GOAL_CONTINUATION
from api.sessions.projects import title_from
from api.usage import prompt_cache_hit_percent

from .attachments import _attachment_name
from .diagnostics import _log_stream_writeback_timings, _stream_writeback_stage
from .gateway_routing_metadata import _extract_gateway_routing_metadata
from .local_context_window import ContextWindowProjection
from .runtime_resolution import _persistent_state_changes, _persistent_state_snapshot
from .thinking_content import _looks_invalid_generated_title, _split_thinking_from_content
from .title_generation import (
    _first_exchange_snippets,
    _is_provisional_title,
    _run_background_title_update,
)
from .tool_events import _extract_tool_calls_from_messages
from .transcript import _stamp_missing_message_timestamps


@dataclass
class LocalSuccessProjection:
    """Values shared by durable save, insights sync, and terminal events."""

    tool_calls: list
    context_window: ContextWindowProjection
    usage: dict
    should_refresh_title: bool
    title_user_snippet: str
    title_assistant_snippet: str

    @classmethod
    def apply(
        cls,
        session,
        *,
        agent,
        result: dict,
        route_model: str,
        resolved_model: str,
        resolved_provider: str | None,
        resolved_base_url: str | None,
        resolved_api_key: str | None,
        config: dict,
        previous_messages: list,
        reasoning_segments: dict,
        live_tool_calls: dict,
        attachments,
        message_text: str,
        turn_started_at: float,
        stream_id: str,
    ) -> "LocalSuccessProjection":
        _stamp_missing_message_timestamps(session.messages)
        if session.title in (None, "", "Untitled", "New Chat"):
            session.title = title_from(session.messages, session.title)
        invalid_title = _looks_invalid_generated_title(session.title)
        should_refresh_title = (
            (
                session.title in (None, "", "Untitled", "New Chat")
                or _is_provisional_title(session.title, session.messages)
                or invalid_title
            )
            and (not getattr(session, "llm_title_generated", False) or invalid_title)
        )
        user_snippet, assistant_snippet = (
            _first_exchange_snippets(session.messages)
            if should_refresh_title
            else ("", "")
        )

        input_tokens = int(getattr(agent, "session_prompt_tokens", 0) or 0)
        output_tokens = int(getattr(agent, "session_completion_tokens", 0) or 0)
        estimated_cost = getattr(agent, "session_estimated_cost_usd", None)
        cache_read_tokens = int(getattr(agent, "session_cache_read_tokens", 0) or 0)
        cache_write_tokens = int(getattr(agent, "session_cache_write_tokens", 0) or 0)
        turn_input = max(0, input_tokens - int(getattr(session, "input_tokens", 0) or 0))
        turn_cache_read = max(
            0,
            cache_read_tokens - int(getattr(session, "cache_read_tokens", 0) or 0),
        )
        if input_tokens:
            session.input_tokens = input_tokens
        if output_tokens:
            session.output_tokens = output_tokens
        if estimated_cost is not None:
            session.estimated_cost = estimated_cost
        if cache_read_tokens:
            session.cache_read_tokens = cache_read_tokens
        if cache_write_tokens:
            session.cache_write_tokens = cache_write_tokens

        tool_calls = _extract_tool_calls_from_messages(
            session.messages,
            live_tool_calls=live_tool_calls,
        )
        session.tool_calls = tool_calls
        _clear_pending_turn(session)
        _tag_turn_attachments(session, attachments, message_text)
        _attach_reasoning(session, previous_messages, reasoning_segments)

        try:
            duration = max(0.0, time.time() - float(turn_started_at))
        except Exception:
            duration = 0.0
        tps = round(output_tokens / duration, 1) if output_tokens and duration > 0 else None
        gateway_routing = _extract_gateway_routing_metadata(
            agent,
            result,
            requested_model=resolved_model or route_model,
            requested_provider=resolved_provider,
        )
        if gateway_routing:
            session.gateway_routing = gateway_routing
            history = list(getattr(session, "gateway_routing_history", None) or [])
            history.append(gateway_routing)
            session.gateway_routing_history = history[-50:]
        _annotate_latest_assistant(
            session,
            duration=duration,
            tps=tps,
            gateway_routing=gateway_routing,
            ttft_ms=meter().get_ttft_ms(stream_id),
        )

        context_window = ContextWindowProjection.from_agent(
            agent,
            resolved_model=resolved_model,
            resolved_provider=resolved_provider,
            resolved_base_url=resolved_base_url,
            resolved_api_key=resolved_api_key,
            config=config,
        )
        context_window.persist_on(session)
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost": estimated_cost,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_hit_percent": prompt_cache_hit_percent(cache_read_tokens, input_tokens),
            "turn_cache_hit_percent": prompt_cache_hit_percent(turn_cache_read, turn_input),
            "duration_seconds": round(duration, 3),
        }
        if tps is not None:
            usage["tps"] = tps
        if gateway_routing:
            usage["gateway_routing"] = gateway_routing
        ttft_ms = meter().get_ttft_ms(stream_id)
        if ttft_ms is not None:
            usage["ttft_ms"] = ttft_ms
        context_window.enrich_usage(usage, session=session)
        estimate = getattr(session, "post_compression_context_tokens_estimate", None)
        usage["post_compression_context_tokens_estimate"] = (
            estimate if isinstance(estimate, int) and estimate > 0 else None
        )
        return cls(
            tool_calls=tool_calls,
            context_window=context_window,
            usage=usage,
            should_refresh_title=should_refresh_title,
            title_user_snippet=user_snippet,
            title_assistant_snippet=assistant_snippet,
        )

    def publish_terminal(
        self,
        session,
        *,
        session_id: str,
        stream_id: str,
        agent,
        publish,
        payload_builder,
        tool_limit_reached: bool,
        maybe_schedule_title_refresh,
        writeback_timings: list,
        writeback_started: float,
        logger: logging.Logger,
    ) -> None:
        with _stream_writeback_stage(writeback_timings, "done_payload"):
            raw_session = payload_builder(session, tool_calls=self.tool_calls)
            done_payload = {
                "session": redact_session_data(raw_session),
                "usage": self.usage,
            }
            if tool_limit_reached:
                done_payload["terminal_state"] = "tool_limit_reached"
                done_payload["terminal_reason"] = "max_iterations"
            publish("done", done_payload)
            stats = meter().get_stats(stream_id)
            stats["session_id"] = session_id
            stats.setdefault("tps_available", False)
            stats.setdefault("estimated", False)
            publish("metering", stats)
        try:
            _log_stream_writeback_timings(
                getattr(session, "session_id", session_id),
                stream_id,
                writeback_timings,
                writeback_started,
            )
        except Exception:
            pass
        if (
            self.should_refresh_title
            and self.title_user_snippet
            and self.title_assistant_snippet
        ):
            threading.Thread(
                target=_run_background_title_update,
                args=(
                    session.session_id,
                    self.title_user_snippet,
                    self.title_assistant_snippet,
                    str(session.title or "").strip(),
                    publish,
                    agent,
                ),
                daemon=True,
            ).start()
        else:
            publish("stream_end", {"session_id": session_id})
            maybe_schedule_title_refresh(session, publish, agent)


def publish_persistent_state_changes(
    *,
    session,
    session_id: str,
    profile_home: str,
    before,
    publish,
    logger: logging.Logger,
) -> None:
    try:
        changes = _persistent_state_changes(
            before,
            _persistent_state_snapshot(profile_home),
        )
        if changes.get("memory_saved"):
            publish(
                "state_saved",
                {"session_id": session_id, "kind": "memory", "action": "saved"},
            )
        for skill in changes.get("skills") or []:
            publish(
                "state_saved",
                {
                    "session_id": session_id,
                    "kind": "skill",
                    "action": skill.get("action") or "updated",
                    "name": skill.get("name") or "",
                },
            )
    except Exception:
        logger.debug(
            "Persistent state change detection failed for session %s",
            session.session_id,
            exc_info=True,
        )


def sync_success_to_insights(session, *, agent, model: str, logger: logging.Logger) -> None:
    try:
        from api.config import load_settings

        if not load_settings().get("sync_to_insights"):
            return
        from api.state_sync import sync_session_usage

        sync_session_usage(
            session_id=session.session_id,
            input_tokens=session.input_tokens or 0,
            output_tokens=session.output_tokens or 0,
            estimated_cost=session.estimated_cost,
            model=model,
            title=session.title,
            message_count=len(session.messages),
            cache_read_tokens=session.cache_read_tokens or 0,
            cache_write_tokens=session.cache_write_tokens or 0,
            api_call_count=getattr(agent, "session_api_calls", None),
            profile=getattr(session, "profile", None),
        )
    except Exception:
        logger.debug("Failed to sync session to insights")


def publish_post_turn_controls(
    session,
    *,
    agent,
    session_id: str,
    profile_home: str,
    goal_related: bool,
    publish,
    logger: logging.Logger,
) -> None:
    try:
        drain = getattr(agent, "_drain_pending_steer", None)
        leftover = drain() if drain else None
        if leftover:
            publish(
                "pending_steer_leftover",
                {"session_id": session_id, "text": str(leftover)},
            )
    except Exception:
        logger.debug("Failed to drain pending steer for session %s", session_id)
    _publish_goal_continuation(
        session,
        session_id=session_id,
        profile_home=profile_home,
        goal_related=goal_related,
        publish=publish,
        logger=logger,
    )


def _publish_goal_continuation(
    session,
    *,
    session_id: str,
    profile_home: str,
    goal_related: bool,
    publish,
    logger: logging.Logger,
) -> None:
    try:
        from api.goals import evaluate_goal_after_turn, has_active_goal

        decision = {}
        if goal_related and has_active_goal(session_id, profile_home=profile_home):
            response = _latest_assistant_text(session.messages)
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
                response,
                user_initiated=True,
                profile_home=profile_home,
            ) or {}
        message = str(decision.get("message") or "").strip()
        if message:
            publish(
                "goal",
                {
                    "session_id": session_id,
                    "state": "continuing" if decision.get("should_continue") else "idle",
                    "message": message,
                    "message_key": decision.get("message_key") or "goal_continuing",
                    "message_args": decision.get("message_args") or [],
                    "decision": decision,
                },
            )
        prompt = str(decision.get("continuation_prompt") or "").strip()
        if decision.get("should_continue") and prompt:
            PENDING_GOAL_CONTINUATION.add(session_id)
            publish(
                "goal_continue",
                {
                    "session_id": session_id,
                    "continuation_prompt": prompt,
                    "text": prompt,
                    "message": message,
                    "message_key": decision.get("message_key") or "goal_continuing",
                    "message_args": decision.get("message_args") or [],
                    "decision": decision,
                },
            )
    except Exception as exc:
        logger.debug("Goal continuation hook failed for session %s: %s", session_id, exc)


def _clear_pending_turn(session) -> None:
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None


def _tag_turn_attachments(session, attachments, message_text: str) -> None:
    if not attachments:
        return
    display = [_attachment_name(item) for item in attachments if _attachment_name(item)]
    base = message_text.split("\n\n[Attached files:", 1)[0].strip()
    for message in reversed(session.messages):
        if message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if base[:60] in content or content[:60] in message_text:
            message["attachments"] = display
            return


def _attach_reasoning(session, previous_messages: list, segments: dict) -> None:
    previous_assistant_count = sum(
        1
        for message in previous_messages or []
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    assistant_index = 0
    for message in session.messages or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        turn_index = assistant_index
        assistant_index += 1
        if turn_index < previous_assistant_count:
            continue
        reasoning = (
            segments.get(turn_index - previous_assistant_count, "")
            or message.get("reasoning")
            or ""
        )
        content = message.get("content")
        if isinstance(content, str) and content:
            content, reasoning = _split_thinking_from_content(content, reasoning)
            message["content"] = content
        if reasoning:
            message["reasoning"] = reasoning


def _annotate_latest_assistant(
    session,
    *,
    duration: float,
    tps: float | None,
    gateway_routing,
    ttft_ms,
) -> None:
    for message in reversed(session.messages or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        message["_turnDuration"] = round(duration, 3)
        if tps is not None:
            message["_turnTps"] = tps
        if gateway_routing:
            message["_gatewayRouting"] = gateway_routing
        if ttft_ms is not None:
            message["_firstTokenMs"] = ttft_ms
        return


def _latest_assistant_text(messages: list) -> str:
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if not isinstance(content, list):
            return str(content or "")
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return ""
