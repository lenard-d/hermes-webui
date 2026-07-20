"""Dependency assembly for the local browser-turn runner.

The local runner owns execution.  HTTP routing calls this narrow entrypoint;
SSE transport only carries the events that the run publishes.
"""

from __future__ import annotations

from api.config import _get_session_agent_lock, resolve_model_provider
from api.sessions.store import get_session

from .agent_cache import _attempt_credential_self_heal, _build_session_db_for_stream
from .agent_loader import _get_ai_agent
from .local import LocalRunDependencies
from . import local as _local_run
from .payloads import _session_payload_with_full_messages
from .provider_errors import _classify_provider_error
from .title_generation import _maybe_schedule_title_refresh
from .webui_prefill import (
    _load_webui_prefill_context,
    _normalize_prefill_messages_before_user_turn,
    _prefill_messages_with_webui_context,
)


def run_agent_streaming(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    ephemeral=False,
    model_provider=None,
    goal_related=False,
    moa_config=None,
):
    """Run one complete local-agent lifecycle for an admitted browser turn."""
    return _local_run.run_agent_streaming(
        session_id,
        msg_text,
        model,
        workspace,
        stream_id,
        attachments,
        ephemeral=ephemeral,
        model_provider=model_provider,
        goal_related=goal_related,
        moa_config=moa_config,
        dependencies=LocalRunDependencies(
            get_session=get_session,
            get_ai_agent=_get_ai_agent,
            resolve_model_provider=resolve_model_provider,
            get_session_agent_lock=_get_session_agent_lock,
            build_session_db_for_stream=_build_session_db_for_stream,
            attempt_credential_self_heal=_attempt_credential_self_heal,
            load_webui_prefill_context=_load_webui_prefill_context,
            prefill_messages_with_webui_context=_prefill_messages_with_webui_context,
            normalize_prefill_messages_before_user_turn=(
                _normalize_prefill_messages_before_user_turn
            ),
            classify_provider_error=_classify_provider_error,
            session_payload_with_full_messages=_session_payload_with_full_messages,
            maybe_schedule_title_refresh=_maybe_schedule_title_refresh,
        ),
    )
