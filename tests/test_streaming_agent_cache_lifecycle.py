from unittest.mock import MagicMock


def test_evicted_agent_lifecycle_commits_unregisters_and_shutdowns(monkeypatch):
    from api.runs import agent_cache

    events = []

    def fake_commit(session_id, *, agent=None, wait=False):
        events.append(("commit", session_id, agent, wait))
        return True

    def fake_has_uncommitted_work(session_id):
        events.append(("has_uncommitted", session_id))
        return False

    def fake_unregister(session_id):
        events.append(("unregister", session_id))

    monkeypatch.setattr(agent_cache, "_lifecycle_commit_session_memory", fake_commit)
    monkeypatch.setattr(agent_cache, "_lifecycle_has_uncommitted_work", fake_has_uncommitted_work)
    monkeypatch.setattr(agent_cache, "_lifecycle_unregister_agent", fake_unregister)
    monkeypatch.setattr(agent_cache, "_lifecycle_discard_session", lambda _session_id: True)

    session_db = MagicMock()
    agent = MagicMock()
    agent._session_db = session_db
    agent._session_messages = [{"role": "user", "content": "hello"}]

    agent_cache._close_evicted_agent_at_session_boundary("old-session", agent)

    assert ("commit", "old-session", agent, True) in events
    assert ("has_uncommitted", "old-session") in events
    assert ("unregister", "old-session") in events
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    session_db.close.assert_called_once()


def test_evicted_agent_lifecycle_shutdown_uses_empty_messages_when_missing(monkeypatch):
    from api.runs import agent_cache

    monkeypatch.setattr(agent_cache, "_lifecycle_commit_session_memory", lambda *a, **kw: True)
    monkeypatch.setattr(agent_cache, "_lifecycle_has_uncommitted_work", lambda session_id: False)
    monkeypatch.setattr(agent_cache, "_lifecycle_unregister_agent", MagicMock())
    monkeypatch.setattr(agent_cache, "_lifecycle_discard_session", lambda _session_id: True)

    agent = MagicMock()
    agent._session_db = MagicMock()

    agent_cache._close_evicted_agent_at_session_boundary("old-session", agent)

    agent.shutdown_memory_provider.assert_called_once_with([])
    agent._session_db.close.assert_called_once()


def test_cached_agent_entry_lifecycle_extracts_agent_from_cache_tuple(monkeypatch):
    from api.runs import agent_cache

    closed = []
    monkeypatch.setattr(
        agent_cache,
        "_close_evicted_agent_at_session_boundary",
        lambda session_id, agent: closed.append((session_id, agent)) or True,
    )

    agent = MagicMock()

    assert agent_cache._close_cached_agent_entry_at_session_boundary("old-session", (agent, "sig")) is True
    assert closed == [("old-session", agent)]


def test_evicted_agent_lifecycle_keeps_provider_alive_when_commit_still_dirty(monkeypatch):
    from api.runs import agent_cache

    def fake_commit(session_id, *, agent=None, wait=False):
        return True

    def fake_has_uncommitted_work(session_id):
        return True

    monkeypatch.setattr(agent_cache, "_lifecycle_commit_session_memory", fake_commit)
    monkeypatch.setattr(agent_cache, "_lifecycle_has_uncommitted_work", fake_has_uncommitted_work)
    monkeypatch.setattr(agent_cache, "_lifecycle_unregister_agent", MagicMock())

    agent = MagicMock()
    agent._session_db = MagicMock()

    agent_cache._close_evicted_agent_at_session_boundary("dirty-session", agent)

    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()


def test_identity_mismatch_cache_eviction_closes_outside_cache_lock(monkeypatch):
    import threading

    import api.config as config
    from api.runs import local_agent_cache

    cache = config.SESSION_AGENT_CACHE.__class__()
    cache_lock = threading.Lock()
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", cache)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE_LOCK", cache_lock)
    monkeypatch.setattr(local_agent_cache, "_register_agent", lambda *_args: None)
    monkeypatch.setattr(local_agent_cache, "_active_session_ids", lambda: set())

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")

    def acquire():
        return local_agent_cache.acquire_local_agent(
            agent_class=FakeAgent,
            session_id="cache-owner-session",
            ephemeral=False,
            kwargs={"session_id": "cache-owner-session"},
            session_db=None,
            model="model",
            provider="provider",
            base_url=None,
            api_key=None,
            runtime={},
            max_iterations=None,
            max_tokens=None,
            fallback_models=None,
            toolsets=None,
            reasoning=None,
            request_overrides=None,
            prefill_status=None,
            profile_home=None,
        )

    created = acquire()
    with cache_lock:
        _, signature = cache["cache-owner-session"]
        mismatched = FakeAgent(session_id="another-session")
        cache["cache-owner-session"] = (mismatched, signature)

    closed = []

    def close_entry(session_id, entry):
        assert not cache_lock.locked()
        closed.append((session_id, entry))
        return True

    monkeypatch.setattr(
        local_agent_cache,
        "_close_cached_agent_entry_at_session_boundary",
        close_entry,
    )

    replacement = acquire()

    assert replacement.agent is not created.agent
    assert closed == [("cache-owner-session", (mismatched, signature))]
