"""Legacy synchronous chat execution behind one run-domain interface.

The browser uses streaming turns; this owner preserves the fallback `/api/chat`
lifecycle without leaking transcript, replay, provider, or environment details
back into the HTTP route.
"""

from __future__ import annotations

import logging
import os

from api.agent_runtime import require_ai_agent_class
from api.config import (
    CHAT_LOCK,
    environment_mutation_lock,
    get_config,
    load_settings,
    model_with_provider_context,
    resolve_custom_provider_connection,
    resolve_model_provider,
)
from api.session_state import session_agent_lock
from api.sessions import title_from
from api.workspace_context import _workspace_context_prefix

from .context_replay import _dedupe_replayed_context_messages
from .message_sanitization import (
    _assign_stable_message_ids,
    _sanitize_messages_for_api,
)
from .post_compression_context import (
    _restore_display_reasoning_metadata,
    _restore_reasoning_metadata,
)
from .prompts import _WEBUI_PROGRESS_PROMPT
from .transcript import _merge_display_messages_after_agent_result
from .turn_context import _context_messages_for_new_turn


logger = logging.getLogger(__name__)


def _runtime_provider_connection(provider, base_url):
    api_key = None
    try:
        from api.auth import resolve_runtime_provider_with_anthropic_env_lock
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider_with_anthropic_env_lock(
            resolve_runtime_provider,
            requested=provider,
        )
        api_key = runtime.get("api_key")
        if not provider:
            provider = runtime.get("provider")
        if not base_url:
            base_url = runtime.get("base_url")
    except Exception as exc:
        logger.warning("resolve_runtime_provider failed: %s", exc)
    if isinstance(provider, str) and provider.startswith("custom:"):
        custom_key, custom_base = resolve_custom_provider_connection(provider)
        if not api_key and custom_key:
            api_key = custom_key
        if not base_url and custom_base:
            base_url = custom_base
    return provider, base_url, api_key


def _system_message(workspace: str) -> str:
    return (
        f"Active workspace at session start: {workspace}\n"
        "Every user message is prefixed with [Workspace::v1: /absolute/path] "
        "indicating the workspace the user has selected in the web UI at the "
        "time they sent that message. This tag is the single authoritative "
        "source of the active workspace and updates with every message. It "
        "overrides any prior workspace mentioned in this system prompt, memory, "
        "or conversation history. Always use the value from the most recent "
        "[Workspace::v1: ...] tag as your default working directory for ALL file "
        "operations: write_file, read_file, search_files, terminal workdir, and "
        "patch. Never fall back to a hardcoded path when this tag is present.\n\n"
        f"{_WEBUI_PROGRESS_PROMPT}\n\n"
        "WebUI external-notes/durable-memory policy: Do not copy or dump this "
        "browser transcript into external notes or durable memory by default. "
        "Write or update durable notes only for explicit captures, durable "
        "preferences, decisions, blockers/open issues, runbook-worthy workflows, "
        "or other clearly reusable signals; otherwise leave external notes and "
        "durable memory unchanged. When you do write or update a durable note, "
        "briefly tell the user what note or section changed so the write is "
        "reviewable."
    )


def _run_agent(session, message: str, *, enabled_toolsets):
    agent_class = require_ai_agent_class()
    with CHAT_LOCK:
        model, provider, base_url = resolve_model_provider(
            model_with_provider_context(
                session.model,
                getattr(session, "model_provider", None),
            )
        )
        provider, base_url, api_key = _runtime_provider_connection(
            provider,
            base_url,
        )
        agent = agent_class(
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            platform="webui",
            quiet_mode=True,
            enabled_toolsets=enabled_toolsets,
            session_id=session.session_id,
        )
        previous_messages = list(session.messages or [])
        previous_context_messages = list(
            _context_messages_for_new_turn(session, message)
        )
        result = agent.run_conversation(
            user_message=_workspace_context_prefix(str(session.workspace)) + message,
            system_message=_system_message(str(session.workspace)),
            conversation_history=_sanitize_messages_for_api(
                previous_context_messages,
                cfg=get_config(),
                effective_model=model,
                effective_provider=provider,
                effective_base_url=base_url,
            ),
            task_id=session.session_id,
            persist_user_message=message,
        )
    return result, previous_messages, previous_context_messages


def _with_session_environment(session, message: str, *, enabled_toolsets):
    with environment_mutation_lock:
        previous = {
            "TERMINAL_CWD": os.environ.get("TERMINAL_CWD"),
            "HERMES_EXEC_ASK": os.environ.get("HERMES_EXEC_ASK"),
            "HERMES_SESSION_KEY": os.environ.get("HERMES_SESSION_KEY"),
        }
        os.environ["TERMINAL_CWD"] = str(session.workspace)
        os.environ["HERMES_EXEC_ASK"] = "1"
        os.environ["HERMES_SESSION_KEY"] = session.session_id
    try:
        return _run_agent(session, message, enabled_toolsets=enabled_toolsets)
    finally:
        with environment_mutation_lock:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _persist_result(
    session,
    message: str,
    result: dict,
    previous_messages: list,
    previous_context_messages: list,
) -> None:
    with session_agent_lock(session.session_id):
        result_messages = result.get("messages") or previous_context_messages
        next_context_messages = _restore_reasoning_metadata(
            previous_context_messages,
            result_messages,
        )
        _assign_stable_message_ids(
            result_messages,
            previous_messages,
            previous_context_messages,
        )
        session.context_messages = _dedupe_replayed_context_messages(
            previous_context_messages,
            next_context_messages,
            message,
        )
        session.messages = _merge_display_messages_after_agent_result(
            previous_messages,
            previous_context_messages,
            _restore_display_reasoning_metadata(previous_messages, result_messages),
            message,
            source=getattr(session, "pending_user_source", None) or "webui",
        )
        if session.title == "Untitled":
            session.title = title_from(session.messages, session.title)
        session.save()


def _sync_insights(session) -> None:
    try:
        if not load_settings().get("sync_to_insights"):
            return
        from api.state_sync import sync_session_usage

        sync_session_usage(
            session_id=session.session_id,
            input_tokens=session.input_tokens or 0,
            output_tokens=session.output_tokens or 0,
            estimated_cost=session.estimated_cost,
            model=session.model,
            title=session.title,
            message_count=len(session.messages),
            cache_read_tokens=session.cache_read_tokens or 0,
            cache_write_tokens=session.cache_write_tokens or 0,
            profile=getattr(session, "profile", None),
        )
    except Exception:
        logger.debug("Failed to update session cost tracking", exc_info=True)


def run_synchronous_chat(session, message: str, *, enabled_toolsets) -> dict:
    """Execute, persist, and project one legacy synchronous chat turn."""
    result, previous_messages, previous_context_messages = _with_session_environment(
        session,
        message,
        enabled_toolsets=enabled_toolsets,
    )
    _persist_result(
        session,
        message,
        result,
        previous_messages,
        previous_context_messages,
    )
    _sync_insights(session)
    return {
        "answer": result.get("final_response") or "",
        "status": "done" if result.get("completed", True) else "partial",
        "session": session.compact() | {"messages": session.messages},
        "result": {key: value for key, value in result.items() if key != "messages"},
    }
