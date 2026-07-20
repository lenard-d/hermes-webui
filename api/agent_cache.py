"""Temporary owner for cached-agent eviction at a session boundary.

The cache storage remains on :mod:`api.config` while legacy route and test
callers still import it there. Once those callers move to a dedicated cache
interface, this operation and its storage can move together under ``api.runs``.
"""

import logging


logger = logging.getLogger(__name__)


def evict_session_agent(session_id: str) -> None:
    """Drop one cached agent without closing a live worker's SessionDB."""
    from api import config as config_api

    agent = None
    with config_api.SESSION_AGENT_CACHE_LOCK:
        entry = config_api.SESSION_AGENT_CACHE.pop(session_id, None)
        if entry is not None:
            agent = entry[0] if isinstance(entry, tuple) else None
    if agent is None:
        return

    # The worker keeps its own agent reference, but it still needs the attached
    # SessionDB until the active run finishes.
    run_active = False
    try:
        with config_api.ACTIVE_RUNS_LOCK:
            run_active = any(
                (entry or {}).get("session_id") == session_id
                for entry in (config_api.ACTIVE_RUNS or {}).values()
            )
    except Exception:
        run_active = False
    if run_active:
        return

    should_close = True
    try:
        from api.sessions import commit_session_memory
        from api.sessions.lifecycle import (
            discard_session,
            has_uncommitted_work,
            unregister_agent,
        )

        if has_uncommitted_work(session_id):
            commit_session_memory(session_id, agent=agent, wait=True)
        if not has_uncommitted_work(session_id):
            unregister_agent(session_id)
            discard_session(session_id)
        else:
            should_close = False
    except Exception:
        should_close = False
        logger.debug(
            "Lifecycle commit on eviction failed for %s",
            session_id,
            exc_info=True,
        )

    if should_close and getattr(agent, "_session_db", None) is not None:
        try:
            agent._session_db.close()
        except Exception:
            logger.debug(
                "Failed to close _session_db on eviction for %s",
                session_id,
                exc_info=True,
            )


__all__ = ["evict_session_agent"]
