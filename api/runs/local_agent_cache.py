"""Session-scoped Hermes Agent cache ownership for local runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class CachedLocalAgent:
    agent: object
    signature: str | None
    session_db: object


def _register_agent(api: ModuleType, session_id: str, agent) -> None:
    try:
        from api.sessions import register_agent

        register_agent(session_id, agent)
    except Exception:
        api.logger.debug(
            "Lifecycle register_agent failed for session %s",
            session_id,
            exc_info=True,
        )


def _active_session_ids() -> set[str]:
    try:
        from api.config import ACTIVE_RUNS, ACTIVE_RUNS_LOCK

        with ACTIVE_RUNS_LOCK:
            return {
                session_id
                for entry in (ACTIVE_RUNS or {}).values()
                if (session_id := (entry or {}).get("session_id"))
            }
    except Exception:
        return set()


def _cache_signature(api: ModuleType, *, identity: list) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[
        :16
    ]


def acquire_local_agent(
    api: ModuleType,
    *,
    agent_class,
    session_id: str,
    ephemeral: bool,
    kwargs: dict,
    session_db,
    model,
    provider,
    base_url,
    api_key,
    runtime: dict,
    max_iterations,
    max_tokens,
    fallback_models,
    toolsets,
    reasoning,
    request_overrides,
    prefill_status,
    profile_home,
) -> CachedLocalAgent:
    """Reuse or create one agent without evicting an active worker."""
    if ephemeral:
        agent = agent_class(**kwargs)
        api.logger.debug("[webui] Created ephemeral agent for session %s", session_id)
        return CachedLocalAgent(agent, None, session_db)

    from api.config import (
        SESSION_AGENT_CACHE,
        SESSION_AGENT_CACHE_LOCK,
        SESSION_AGENT_CACHE_MAX,
    )

    credential_pool = runtime.get("credential_pool")
    signature = _cache_signature(
        api,
        identity=[
            model or "",
            api._agent_cache_api_key_sig(api_key, credential_pool),
            base_url or "",
            provider or "",
            runtime.get("api_mode") or "",
            runtime.get("command") or "",
            runtime.get("args") or [],
            bool(credential_pool),
            max_iterations or "",
            max_tokens or "",
            fallback_models or {},
            sorted(toolsets) if toolsets else [],
            reasoning or {},
            request_overrides or {},
            prefill_status,
            profile_home or "",
        ],
    )

    agent = None
    identity_mismatch = None
    with SESSION_AGENT_CACHE_LOCK:
        cached = SESSION_AGENT_CACHE.get(session_id)
        if cached and cached[1] == signature:
            candidate = cached[0]
            if api._cached_agent_matches_session(candidate, session_id):
                agent = candidate
                SESSION_AGENT_CACHE.move_to_end(session_id)
            else:
                identity_mismatch = SESSION_AGENT_CACHE.pop(session_id, None)
                api.logger.warning(
                    "[webui] Evicted cached agent with mismatched session identity: "
                    "cache_key=%s agent_session_id=%s",
                    session_id,
                    api._cached_agent_session_identity(candidate),
                )
    if identity_mismatch is not None:
        try:
            api._close_cached_agent_entry_at_session_boundary(
                session_id, identity_mismatch
            )
        except Exception:
            api.logger.debug(
                "Failed to close identity-mismatched cached agent for %s",
                session_id,
                exc_info=True,
            )

    if agent is not None and not api._refresh_cached_agent_runtime(agent, kwargs):
        with SESSION_AGENT_CACHE_LOCK:
            stale = SESSION_AGENT_CACHE.pop(session_id, None)
        if stale is not None:
            try:
                api._close_cached_agent_entry_at_session_boundary(session_id, stale)
            except Exception:
                api.logger.debug(
                    "Failed to close stale-runtime cached agent for %s",
                    session_id,
                    exc_info=True,
                )
        agent = None

    if agent is not None:
        _register_agent(api, session_id, agent)
        for attribute, key in (
            ("stream_delta_callback", "stream_delta_callback"),
            ("tool_progress_callback", "tool_progress_callback"),
            ("tool_start_callback", "tool_start_callback"),
            ("tool_complete_callback", "tool_complete_callback"),
            ("status_callback", "status_callback"),
            ("interim_assistant_callback", "interim_assistant_callback"),
            ("reasoning_callback", "reasoning_callback"),
            ("clarify_callback", "clarify_callback"),
        ):
            if hasattr(agent, attribute):
                setattr(agent, attribute, kwargs.get(key))
        if "prefill_messages" in kwargs and hasattr(agent, "prefill_messages"):
            agent.prefill_messages = list(kwargs.get("prefill_messages") or [])
        if session_db is not None:
            session_db = api._adopt_session_db_for_cached_agent(agent, session_db)
            agent._session_db = session_db
        if hasattr(agent, "_api_call_count"):
            agent._api_call_count = 0
        if hasattr(agent, "_interrupted"):
            agent._interrupted = False
        if hasattr(agent, "_interrupt_message"):
            agent._interrupt_message = None
        api.logger.debug("[webui] Reusing cached agent for session %s", session_id)
        return CachedLocalAgent(agent, signature, session_db)

    agent = agent_class(**kwargs)
    _register_agent(api, session_id, agent)
    active_sessions = _active_session_ids()
    evicted = []
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE[session_id] = (agent, signature)
        SESSION_AGENT_CACHE.move_to_end(session_id)
        while len(SESSION_AGENT_CACHE) > SESSION_AGENT_CACHE_MAX:
            evictable = next(
                (sid for sid in SESSION_AGENT_CACHE if sid not in active_sessions),
                None,
            )
            if evictable is None:
                break
            evicted.append((evictable, SESSION_AGENT_CACHE.pop(evictable)))
    for evicted_session_id, entry in evicted:
        try:
            evicted_agent = entry[0] if isinstance(entry, tuple) else None
            api._close_evicted_agent_at_session_boundary(
                evicted_session_id, evicted_agent
            )
        except Exception:
            api.logger.debug(
                "Failed to close evicted agent for session %s",
                evicted_session_id,
                exc_info=True,
            )
    api.logger.debug("[webui] Created new agent for session %s", session_id)
    return CachedLocalAgent(agent, signature, session_db)
