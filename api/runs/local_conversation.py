"""Conversation input, history, and provider invocation for a local run."""

from __future__ import annotations

import time
from dataclasses import dataclass

from api.metering import meter
from api.sessions.reconciliation_projection import reconciled_state_db_messages_for_session
from api.sessions.state_db_messages import get_state_db_session_messages
from api.workspace_context import _workspace_context_prefix

from .attachments import _build_native_multimodal_message
from .message_sanitization import (
    _deduplicate_context_messages,
    _sanitize_messages_for_api,
)
from .process_notifications import (
    _accept_pending_async_delegations,
    _drain_webui_process_notifications,
)
from .prompts import _webui_ephemeral_system_prompt
from .runtime_resolution import _persistent_state_snapshot
from .turn_context import _new_turn_context_from_messages


@dataclass
class LocalConversation:
    """Prepared input and immutable pre-turn observations for one invocation."""

    previous_messages: list
    previous_context_messages: list
    user_message: object
    system_message: str
    started_at: float
    pre_compression_count: int
    persistent_state_before: object
    kwargs: dict

    @classmethod
    def prepare(
        cls,
        *,
        session,
        session_id: str,
        stream_id: str,
        message_text: str,
        attachments,
        workspace,
        agent,
        config: dict,
        resolved_model: str,
        resolved_provider: str | None,
        resolved_base_url: str | None,
        profile_home: str,
        checkpoint,
        checkpoint_activity,
        session_lock,
        moa_config,
    ) -> "LocalConversation":
        workspace_context = _workspace_context_prefix(str(session.workspace))
        system_message = _workspace_system_message(str(session.workspace))
        agent.ephemeral_system_prompt = _webui_ephemeral_system_prompt(
            _personality_prompt(session, config),
            surface_context={
                "source": "webui",
                "session_id": session_id,
                "profile": getattr(session, "profile", None),
                "workspace": session.workspace,
            },
            config_data=config,
        )

        pending_started_at = getattr(session, "pending_started_at", None)
        meter().set_pending_started_at(stream_id, pending_started_at)
        started_at = pending_started_at or time.time()
        state_messages = get_state_db_session_messages(
            getattr(session, "session_id", None)
        )
        previous_messages = list(
            reconciled_state_db_messages_for_session(
                session,
                state_messages=state_messages,
            )
            or []
        )
        previous_context_messages = _new_turn_context_from_messages(
            reconciled_state_db_messages_for_session(
                session,
                prefer_context=True,
                state_messages=state_messages,
            ),
            message_text,
        )
        previous_context_messages = _deduplicate_context_messages(
            previous_context_messages
        )
        pre_compression_count = int(
            getattr(getattr(agent, "context_compressor", None), "compression_count", 0)
            or 0
        )
        checkpoint.start(
            session,
            session_lock=session_lock,
            activity=checkpoint_activity,
        )

        pending_acceptances: list = []
        notifications = _drain_webui_process_notifications(
            session_id,
            pending_async_acceptances=pending_acceptances,
        )
        agent_message_text = _message_with_notifications(
            message_text,
            notifications,
        )
        user_message = _build_native_multimodal_message(
            workspace_context,
            agent_message_text,
            attachments,
            workspace,
            cfg=config,
        )
        kwargs = {
            "user_message": user_message,
            "system_message": system_message,
            "conversation_history": _sanitize_messages_for_api(
                previous_context_messages,
                cfg=config,
                effective_model=resolved_model,
                effective_provider=resolved_provider,
                effective_base_url=resolved_base_url,
            ),
            "task_id": session_id,
            "persist_user_message": message_text,
        }
        if moa_config is not None:
            kwargs["moa_config"] = moa_config

        rejected = _accept_pending_async_delegations(
            pending_acceptances,
            session_id=session_id,
        )
        if rejected:
            notifications = [item for item in notifications if item not in rejected]
            agent_message_text = _message_with_notifications(
                message_text,
                notifications,
            )
            user_message = _build_native_multimodal_message(
                workspace_context,
                agent_message_text,
                attachments,
                workspace,
                cfg=config,
            )
            kwargs["user_message"] = user_message

        return cls(
            previous_messages=previous_messages,
            previous_context_messages=previous_context_messages,
            user_message=user_message,
            system_message=system_message,
            started_at=started_at,
            pre_compression_count=pre_compression_count,
            persistent_state_before=_persistent_state_snapshot(profile_home),
            kwargs=kwargs,
        )

    def run(self, agent):
        return agent.run_conversation(**self.kwargs)


def _message_with_notifications(message_text: str, notifications: list) -> str:
    if not notifications:
        return message_text
    return "\n\n".join([*notifications, message_text]).strip()


def _workspace_system_message(workspace: str) -> str:
    return (
        f"Active workspace at session start: {workspace}\n"
        "Every user message is prefixed with [Workspace::v1: /absolute/path] indicating the "
        "workspace the user has selected in the web UI at the time they sent that message. "
        "This tag is the single authoritative source of the active workspace and updates "
        "with every message. It overrides any prior workspace mentioned in this system "
        "prompt, memory, or conversation history. Always use the value from the most recent "
        "[Workspace::v1: ...] tag as your default working directory for ALL file operations: "
        "write_file, read_file, search_files, terminal workdir, and patch. "
        "Never fall back to a hardcoded path when this tag is present."
    )


def _personality_prompt(session, config: dict) -> str | None:
    name = getattr(session, "personality", None)
    personalities = (config.get("agent", {}) or {}).get("personalities", {})
    if not name or not isinstance(personalities, dict) or name not in personalities:
        return None
    value = personalities[name]
    if not isinstance(value, dict):
        return str(value)
    parts = [value.get("system_prompt", "") or value.get("prompt", "")]
    if value.get("tone"):
        parts.append(f"Tone: {value['tone']}")
    if value.get("style"):
        parts.append(f"Style: {value['style']}")
    return "\n".join(part for part in parts if part)
