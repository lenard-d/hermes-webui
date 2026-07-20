"""Canonical process-local cache for reusable Hermes Agent instances.

The cache spans the HTTP/session and local-run packages, so its storage and
eviction transaction live at this neutral seam.  ``api.config`` re-exports the
same objects for compatibility; it no longer owns their lifecycle.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import sys
import threading


logger = logging.getLogger(__name__)


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


SESSION_AGENT_CACHE: collections.OrderedDict = collections.OrderedDict()
SESSION_AGENT_CACHE_MAX = _positive_env_int("HERMES_WEBUI_AGENT_CACHE_MAX", 25)
SESSION_AGENT_CACHE_LOCK = threading.Lock()


def compatibility_cache_state() -> tuple[collections.OrderedDict, object]:
    """Return cache objects, honoring explicit legacy-facade replacements.

    Mutable object identity normally makes the owner and facade identical.
    A small number of integrations historically replaced the facade objects
    wholesale.  Resolve those replacements at operation time until that public
    monkeypatch contract can be retired deliberately.
    """
    config_api = sys.modules.get("api.config")
    if config_api is None:
        return SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
    return (
        getattr(config_api, "SESSION_AGENT_CACHE", SESSION_AGENT_CACHE),
        getattr(config_api, "SESSION_AGENT_CACHE_LOCK", SESSION_AGENT_CACHE_LOCK),
    )


def agent_cache_max() -> int:
    """Return the live cache cap, including compatibility-facade overrides."""
    config_api = sys.modules.get("api.config")
    candidate = (
        getattr(config_api, "SESSION_AGENT_CACHE_MAX", SESSION_AGENT_CACHE_MAX)
        if config_api is not None
        else SESSION_AGENT_CACHE_MAX
    )
    return candidate if isinstance(candidate, int) and candidate >= 1 else SESSION_AGENT_CACHE_MAX


@contextlib.contextmanager
def locked_agent_cache():
    """Yield the live cache while holding its matching compatibility lock."""
    cache, cache_lock = compatibility_cache_state()
    with cache_lock:
        yield cache


def evict_session_agent(session_id: str) -> None:
    """Drop one cached agent without closing a live worker's SessionDB."""
    agent = None
    with locked_agent_cache() as cache:
        entry = cache.pop(session_id, None)
        if entry is not None:
            agent = entry[0] if isinstance(entry, tuple) else None
    if agent is None:
        return

    # The worker keeps its own agent reference, but it still needs the attached
    # SessionDB until the active run finishes.
    run_active = False
    try:
        from api.runtime_state import ACTIVE_RUNS, ACTIVE_RUNS_LOCK

        with ACTIVE_RUNS_LOCK:
            run_active = any(
                (entry or {}).get("session_id") == session_id
                for entry in (ACTIVE_RUNS or {}).values()
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


__all__ = [
    "SESSION_AGENT_CACHE",
    "SESSION_AGENT_CACHE_LOCK",
    "SESSION_AGENT_CACHE_MAX",
    "agent_cache_max",
    "compatibility_cache_state",
    "evict_session_agent",
    "locked_agent_cache",
]
