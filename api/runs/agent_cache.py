"""Cached-agent, SessionDB, credential refresh, and teardown ownership."""

from __future__ import annotations

import contextlib
import logging
import random
import sqlite3
import time

from api.config import CANCEL_FLAGS
from api.sessions.lifecycle import (
    commit_session_memory,
    discard_session,
    has_uncommitted_work,
    unregister_agent,
)
from api.sessions.pending_recovery import (
    _apply_core_sync_or_error_marker as apply_core_sync_or_error_marker,
)
from api.sessions.process_wakeup import _get_profile_home as get_profile_home


logger = logging.getLogger(__name__)


def _last_resort_sync_from_core(session, stream_id, agent_lock):
    """Final-exit guard: if the stream exits with pending_user_message still set,
    sync messages from the core transcript or add an error marker.
    Called from the outer finally block of _run_agent_streaming.
    Must never raise.
    """
    try:
        # Guard: if a cancel was already requested, bail out — cancel_stream() has
        # already saved partial content and we must not double-append error markers.
        if stream_id in CANCEL_FLAGS and CANCEL_FLAGS[stream_id].is_set():
            return

        profile_home = get_profile_home(session.profile)
        core_path = profile_home / 'sessions' / f'session_{session.session_id}.json'

        _lock_ctx = agent_lock if agent_lock is not None else contextlib.nullcontext()
        with _lock_ctx:
            apply_core_sync_or_error_marker(
                session,
                core_path,
                stream_id_for_recheck=stream_id,
                require_stream_dead=False,
            )
    except Exception:
        logger.exception(
            "_last_resort_sync_from_core failed for session %s",
            getattr(session, 'session_id', '?'),
        )

def _session_db_is_open(session_db) -> bool:
    """True when *session_db* still has a live sqlite connection.

    SessionDB.close() sets ``_conn = None``. Subagents capture the parent's
    SessionDB object by reference at spawn time (delegate_tool), so closing
    that object mid-parent-turn makes every subsequent child
    ``append_message`` fail with
    ``'NoneType' object has no attribute 'execute'``.
    """
    if session_db is None:
        return False
    return getattr(session_db, "_conn", None) is not None

def _adopt_session_db_for_cached_agent(agent, new_session_db):
    """Attach a SessionDB to a reused cached agent without breaking subagents.

    Historical behaviour (PR #1421 FD-leak fix): create a fresh SessionDB every
    stream request and close the previous handle before replacing
    ``agent._session_db``. That stops EMFILE growth, but a server-side wakeup
    / new turn for the same parent session will close the shared handle while
    background subagents are still writing into it.

    Policy now:
    - If the cached agent already holds an *open* SessionDB, keep it and close
      the unused new handle (no FD leak; live subagents keep working).
    - If the existing handle is missing or already closed, adopt *new_session_db*.
    - If *new_session_db* is None, leave the existing handle alone.
    """
    if agent is None:
        return new_session_db
    existing = getattr(agent, "_session_db", None)
    if new_session_db is None:
        return existing
    if existing is new_session_db:
        return existing
    if _session_db_is_open(existing):
        try:
            new_session_db.close()
        except Exception:
            # Same observability as _replace_session_db_in_kwargs: a failed
            # close here reintroduces the EMFILE pressure PR #1421 fixed.
            logger.debug(
                "Failed to close unused session_db handle in adopt helper",
                exc_info=True,
            )
        return existing
    if existing is not None:
        try:
            existing.close()
        except Exception:
            logger.debug(
                "Failed to close previous session_db handle in adopt helper",
                exc_info=True,
            )
    agent._session_db = new_session_db
    return new_session_db

def _build_session_db_for_stream(state_db_path):
    """Build a per-request SessionDB handle for WebUI session search.

    Returns ``None`` if the helper module or constructor fails so callers can
    continue without session_search rather than propagating a hard failure.
    """
    try:
        from hermes_state import SessionDB
        _attempts = 3
        _last_error = None
        for _attempt in range(_attempts):
            try:
                return SessionDB(db_path=state_db_path)
            except sqlite3.OperationalError as _db_err:
                _db_err_text = str(_db_err).lower()
                if not (
                    "locked" in _db_err_text or "busy" in _db_err_text
                ):
                    raise
                _last_error = _db_err
                if _attempt < _attempts - 1:
                    print(
                        f"[webui] WARNING: SessionDB init attempt {_attempt + 1}/{_attempts} failed, retrying: {_db_err}",
                        flush=True,
                    )
                    time.sleep(0.05 * (2 ** _attempt) + random.uniform(0, 0.05))
        raise _last_error or RuntimeError("SessionDB construction exhausted all attempts")
    except Exception as _db_err:
        print(f"[webui] WARNING: SessionDB init failed - session_search will be unavailable: {_db_err}", flush=True)
        return None

def _replace_session_db_in_kwargs(agent_kwargs, state_db_path):
    """Build a fresh SessionDB and replace ``agent_kwargs['session_db']`` safely.

    Does not close an existing open handle that may still be shared with live
    subagents; only replaces when the prior handle is missing or already closed.
    """
    if not isinstance(agent_kwargs, dict):
        return None

    _old_session_db = agent_kwargs.get("session_db")
    _next_session_db = _build_session_db_for_stream(state_db_path)
    if _next_session_db is None:
        # Replacement construction failed. Keep the prior handle only if it is
        # still open (live subagents may hold it by reference); otherwise
        # degrade cleanly to None — as master did — so the rebuilt agent lazily
        # reinitialises its SessionDB instead of reusing a closed handle and
        # failing every persist/search with
        # "'NoneType' object has no attribute 'execute'".
        if _session_db_is_open(_old_session_db):
            return _old_session_db
        agent_kwargs["session_db"] = None
        return None
    if _session_db_is_open(_old_session_db):
        # Keep the live handle; discard the unused new one.
        try:
            if _next_session_db is not _old_session_db:
                _next_session_db.close()
        except Exception:
            logger.debug("Failed to close unused session_db handle during self-heal")
        agent_kwargs["session_db"] = _old_session_db
        return _old_session_db
    if _old_session_db is not None and _old_session_db is not _next_session_db:
        try:
            _old_session_db.close()
        except Exception:
            logger.debug("Failed to close previous session_db handle during self-heal")
    agent_kwargs["session_db"] = _next_session_db
    return _next_session_db

def _attempt_credential_self_heal(
    provider_id, session_id, _agent_lock_ref, *, target_model=None,
):
    """Try to silently refresh credentials after a 401/auth error (#1401).

    Returns a new ``(agent, rt_dict)`` tuple on success so the caller can
    retry the conversation.  Returns ``None`` when self-heal is not
    applicable (e.g. auth.json unchanged, provider unresolvable).

    Steps:
    1. Re-read ``~/.hermes/auth.json`` to pick up fresh credentials that
       may have been written by a concurrent ``hermes model`` CLI invocation.
    2. Evict the session's cached agent so it is rebuilt with fresh keys.
    3. Evict the provider's credential-pool cache entry.
    4. Re-resolve the runtime provider.
    5. Return a new agent + resolved-provider dict (the caller must
       re-invoke ``run_conversation`` with these).
    """
    try:
        from api.auth import (
            read_auth_json,
            resolve_runtime_provider_with_anthropic_env_lock,
        )
        from api.config import (
            SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK,
            invalidate_credential_pool_cache,
        )
        from hermes_cli.runtime_provider import resolve_runtime_provider

        # 1. Re-read auth.json (triggers a fresh credential scan)
        _fresh_auth = read_auth_json()
        if not _fresh_auth:
            logger.debug('[webui] self-heal: auth.json empty or missing, skipping')
            return None

        # 2. Evict the cached agent for this session
        _evicted_entry = None
        with SESSION_AGENT_CACHE_LOCK:
            _evicted_entry = SESSION_AGENT_CACHE.pop(session_id, None)
        if _evicted_entry is not None:
            _close_cached_agent_entry_at_session_boundary(session_id, _evicted_entry)

        # 3. Invalidate the credential pool for this provider
        invalidate_credential_pool_cache(provider_id)

        # 4. Re-resolve runtime provider with fresh credentials
        _new_rt = resolve_runtime_provider_with_anthropic_env_lock(
            resolve_runtime_provider,
            requested=provider_id,
            target_model=target_model,
        )

        logger.info(
            '[webui] self-heal: credential refresh succeeded for provider=%s session=%s',
            provider_id, session_id,
        )
        return _new_rt
    except Exception as _heal_err:
        logger.warning(
            '[webui] self-heal: failed for provider=%s session=%s: %s',
            provider_id, session_id, _heal_err,
        )
        return None

def _agent_cache_api_key_sig(resolved_api_key, credential_pool) -> str:
    """Return the cache-signature component for runtime credentials.

    Credential-pool providers can legitimately hand WebUI a different runtime
    token on each request (round-robin pools, OAuth refresh, auth self-heal).
    The AIAgent object is also where cross-turn memory-provider state lives, so
    using the volatile token itself in the cache signature silently defeats the
    per-session agent cache and drops warmed Hindsight prefetch results.
    """
    if credential_pool is not None:
        return 'credential-pool'
    import hashlib as _hashlib
    return _hashlib.sha256((resolved_api_key or '').encode()).hexdigest()[:16]

def _lifecycle_commit_session_memory(session_id: str, *, agent=None, wait: bool = False) -> bool:
    return commit_session_memory(session_id, agent=agent, wait=wait)

def _lifecycle_has_uncommitted_work(session_id: str) -> bool:
    return has_uncommitted_work(session_id)

def _lifecycle_unregister_agent(session_id: str) -> None:
    unregister_agent(session_id)

def _lifecycle_discard_session(session_id: str) -> bool:
    return discard_session(session_id)

def _close_evicted_agent_at_session_boundary(session_id: str, agent) -> bool:
    """Commit and tear down an evicted cached agent at a WebUI session boundary.

    WebUI keeps AIAgent instances in an LRU cache so memory providers can carry
    state across turns. When an agent is evicted, commit pending memory first;
    if the lifecycle entry is clean afterwards, unregister and call
    shutdown_memory_provider(messages) so provider-owned clients such as
    Hindsight's aiohttp session are closed instead of being garbage-collected
    later. Passing the cached transcript mirrors gateway cleanup semantics for
    providers that use on_session_end(messages) during shutdown.
    """
    if agent is None:
        return True

    should_close_evicted_agent = True
    try:
        _lifecycle_commit_session_memory(session_id, agent=agent, wait=True)
        if not _lifecycle_has_uncommitted_work(session_id):
            _lifecycle_unregister_agent(session_id)
            # Drop the lifecycle dict entry now that the LRU-evicted agent is
            # gone and no uncommitted work remains, so the dict tracks only live
            # sessions instead of growing unbounded (issue #3506).
            _lifecycle_discard_session(session_id)
        else:
            should_close_evicted_agent = False
    except Exception:
        should_close_evicted_agent = False
        logger.debug("Lifecycle commit on eviction failed for %s", session_id, exc_info=True)

    if not should_close_evicted_agent:
        return False

    try:
        shutdown_memory_provider = getattr(agent, 'shutdown_memory_provider', None)
        if callable(shutdown_memory_provider):
            session_messages = vars(agent).get('_session_messages', [])
            shutdown_memory_provider(session_messages)
    except Exception:
        logger.debug("Failed to shut down evicted agent memory provider for session %s", session_id, exc_info=True)

    try:
        session_db = getattr(agent, '_session_db', None)
        if session_db is not None:
            session_db.close()
    except Exception:
        logger.debug("Failed to close evicted agent session DB for session %s", session_id, exc_info=True)
    return True

def _close_cached_agent_entry_at_session_boundary(session_id: str, cache_entry) -> bool:
    """Commit and tear down a popped SESSION_AGENT_CACHE entry outside the cache lock."""
    agent = cache_entry[0] if isinstance(cache_entry, tuple) else None
    return _close_evicted_agent_at_session_boundary(session_id, agent)

def _refresh_cached_agent_runtime(agent, agent_kwargs: dict) -> bool:
    """Refresh volatile runtime credentials on a reused cached AIAgent.

    The cache key intentionally ignores credential-pool token churn, but the
    cached agent's LLM client still needs the latest selected/refreshed runtime
    key. Keep long-lived provider/session state (memory prefetch, turn counters,
    tool state) while swapping only the runtime credential/client.
    """
    if agent is None or not isinstance(agent_kwargs, dict):
        return False

    new_pool = agent_kwargs.get('credential_pool')
    if new_pool is not None:
        try:
            agent._credential_pool = new_pool
        except Exception:
            pass

    new_key = agent_kwargs.get('api_key') or ''
    if not new_key:
        return True

    new_base = agent_kwargs.get('base_url') or getattr(agent, 'base_url', '') or ''
    if getattr(agent, '_fallback_activated', False):
        # Avoid mixing a refreshed primary credential into a live fallback
        # runtime. Rebuilding is safer than mutating a fallback-active agent
        # whose restore/cooldown state has not run yet for this turn.
        return False

    if new_key == (getattr(agent, 'api_key', '') or ''):
        _refresh_cached_agent_primary_runtime_snapshot(agent)
        return True

    try:
        if getattr(agent, 'api_mode', None) == 'anthropic_messages':
            # Native Anthropic-style clients have their own construction path;
            # switch_model() already handles token/client refresh there.
            if hasattr(agent, 'switch_model'):
                agent.switch_model(
                    agent_kwargs.get('model') or getattr(agent, 'model', None),
                    agent_kwargs.get('provider') or getattr(agent, 'provider', None),
                    api_key=new_key,
                    base_url=new_base,
                    api_mode=agent_kwargs.get('api_mode') or getattr(agent, 'api_mode', ''),
                )
                return True
            return False

        if not hasattr(agent, '_client_kwargs') or not hasattr(agent, '_replace_primary_openai_client'):
            # Test/fake-agent fallback: keep metadata accurate even if no real
            # OpenAI client exists to rebuild.
            agent.api_key = new_key
            if new_base:
                agent.base_url = new_base
            _refresh_cached_agent_primary_runtime_snapshot(agent)
            return True

        client_kwargs = dict(getattr(agent, '_client_kwargs', {}) or {})
        client_kwargs['api_key'] = new_key
        if new_base:
            client_kwargs['base_url'] = new_base
        agent._client_kwargs = client_kwargs
        agent.api_key = new_key
        if new_base:
            agent.base_url = new_base
        if hasattr(agent, '_apply_client_headers_for_base_url'):
            agent._apply_client_headers_for_base_url(agent.base_url)
        rebuilt = bool(agent._replace_primary_openai_client(reason='webui_credential_refresh'))
        if rebuilt:
            _refresh_cached_agent_primary_runtime_snapshot(agent)
        return rebuilt
    except Exception:
        logger.debug('[webui] Failed to refresh cached agent runtime credentials', exc_info=True)
        return False

def _cached_agent_session_identity(agent) -> str | None:
    """Best-effort session id carried by a cached AIAgent.

    The cache key is only safe when it agrees with the object's own session
    identity. Some old/fake agents may not expose an identity; keep those
    backwards-compatible and treat them as unverifiable rather than mismatched.
    """
    if agent is None:
        return None
    for attr in ('session_id', '_session_id'):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value:
            return value
    session_db = getattr(agent, '_session_db', None)
    if session_db is not None:
        for attr in ('session_id', '_session_id'):
            value = getattr(session_db, attr, None)
            if isinstance(value, str) and value:
                return value
    return None

def _cached_agent_matches_session(agent, session_id: str) -> bool:
    identity = _cached_agent_session_identity(agent)
    return identity is None or identity == str(session_id)

def _refresh_cached_agent_primary_runtime_snapshot(agent) -> None:
    """Keep AIAgent's primary-runtime snapshot aligned with refreshed creds.

    Long-lived AIAgent instances use `_primary_runtime` to restore the preferred
    provider after fallback/transport recovery. If WebUI refreshes a cached
    agent's runtime token but leaves that snapshot stale, a later restore can
    resurrect the old credential and undo the refresh.
    """
    rt = getattr(agent, '_primary_runtime', None)
    if not isinstance(rt, dict):
        return

    base_url = getattr(agent, 'base_url', rt.get('base_url'))
    api_key = getattr(agent, 'api_key', rt.get('api_key', ''))
    client_kwargs = dict(getattr(agent, '_client_kwargs', None) or rt.get('client_kwargs', {}) or {})

    rt['base_url'] = base_url
    rt['api_key'] = api_key
    rt['client_kwargs'] = client_kwargs

    # The default context compressor usually tracks the primary runtime too;
    # keep both the live compressor fields and the fallback-restoration
    # snapshot aligned when those attributes exist.
    cc = getattr(agent, 'context_compressor', None)
    if cc is not None:
        if hasattr(cc, 'base_url'):
            cc.base_url = base_url
        if hasattr(cc, 'api_key'):
            cc.api_key = api_key
        if 'compressor_base_url' in rt:
            rt['compressor_base_url'] = getattr(cc, 'base_url', base_url)
        if 'compressor_api_key' in rt:
            rt['compressor_api_key'] = getattr(cc, 'api_key', api_key)
    else:
        if 'compressor_base_url' in rt:
            rt['compressor_base_url'] = base_url
        if 'compressor_api_key' in rt:
            rt['compressor_api_key'] = api_key

    if getattr(agent, 'api_mode', None) == 'anthropic_messages':
        if hasattr(agent, '_anthropic_api_key'):
            rt['anthropic_api_key'] = agent._anthropic_api_key
        if hasattr(agent, '_anthropic_base_url'):
            rt['anthropic_base_url'] = agent._anthropic_base_url
        if hasattr(agent, '_is_anthropic_oauth'):
            rt['is_anthropic_oauth'] = agent._is_anthropic_oauth
