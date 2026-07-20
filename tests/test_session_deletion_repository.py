import gc
import threading
import weakref

import api.config as config
import api.sessions.store as models
import api.sessions.cache as session_cache
import api.sessions.cleanup as session_cleanup
import api.sessions.records as session_records
import pytest
from api.sessions.store import Session


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(session_cache, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_cache, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(session_records, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_records, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    return session_dir


def _patch_cleanup_collaborators(monkeypatch, tmp_path):
    calls = []
    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir()
    monkeypatch.setattr(
        "api.media.cleanup.session_attachment_dir",
        lambda sid: attachment_dir,
    )
    monkeypatch.setattr(
        "api.turn_journal.delete_turn_journal",
        lambda sid: calls.append(("turn_journal", sid)),
    )
    monkeypatch.setattr(
        "api.runs.delete_run_journal",
        lambda sid: calls.append(("run_journal", sid)),
    )
    monkeypatch.setattr(
        "api.background_process.forget_bg_task_completion_dedup",
        lambda sid: calls.append(("completion_dedup", sid)),
    )
    monkeypatch.setattr(
        "api.terminal.close_terminal",
        lambda sid: calls.append(("terminal", sid)),
    )
    monkeypatch.setattr(
        config,
        "_evict_session_agent",
        lambda sid: calls.append(("agent", sid)),
    )
    return calls, attachment_dir


def test_delete_session_state_owns_all_persisted_and_runtime_cleanup(tmp_path, monkeypatch):
    from api.sessions.repository import delete_session_state

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "owneddelete1"
    session = Session(
        session_id=sid,
        messages=[{"role": "user", "content": "delete me"}],
    )
    session.save()
    session.path.with_suffix(".json.bak").write_text("backup", encoding="utf-8")
    calls, attachment_dir = _patch_cleanup_collaborators(monkeypatch, tmp_path)
    monkeypatch.setattr(session_cleanup, "delete_cli_session", lambda value: False)
    config._get_session_agent_lock(sid)

    result = delete_session_state(sid, messaging=False)

    assert result.sidecar_deleted is True
    assert result.state_db_cleanup_failed is True
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / f"{sid}.json.bak").exists()
    assert not attachment_dir.exists()
    assert sid not in models.SESSIONS
    assert sid not in config.SESSION_AGENT_LOCKS
    assert sid in models._load_webui_deleted_session_tombstone()
    assert calls == [
        ("agent", sid),
        ("turn_journal", sid),
        ("run_journal", sid),
        ("completion_dedup", sid),
        ("terminal", sid),
    ]


def test_delete_messaging_session_preserves_state_db_and_has_no_webui_tombstone(
    tmp_path, monkeypatch
):
    from api.sessions.repository import delete_session_state

    _isolate_session_store(tmp_path, monkeypatch)
    sid = "messagingdelete1"
    Session(session_id=sid, messages=[]).save()
    _patch_cleanup_collaborators(monkeypatch, tmp_path)
    state_db_calls = []
    monkeypatch.setattr(
        session_cleanup,
        "delete_cli_session",
        lambda value: state_db_calls.append(value) or True,
    )

    result = delete_session_state(sid, messaging=True)

    assert result.sidecar_deleted is True
    assert result.state_db_cleanup_failed is False
    assert state_db_calls == []
    assert sid not in models._load_webui_deleted_session_tombstone()


def test_delete_session_state_waits_for_the_session_owner_lock(tmp_path, monkeypatch):
    from api.sessions.repository import delete_session_state

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "serializeddelete1"
    Session(
        session_id=sid,
        messages=[{"role": "user", "content": "delete after writer"}],
    ).save()
    _patch_cleanup_collaborators(monkeypatch, tmp_path)
    monkeypatch.setattr(session_cleanup, "delete_cli_session", lambda value: True)
    owner_lock = config._get_session_agent_lock(sid)
    started = threading.Event()
    finished = threading.Event()

    def delete():
        started.set()
        delete_session_state(sid, messaging=False)
        finished.set()

    with owner_lock:
        thread = threading.Thread(target=delete)
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.05)
        assert (session_dir / f"{sid}.json").exists()

    thread.join(timeout=2)
    assert finished.is_set()
    assert not (session_dir / f"{sid}.json").exists()


def test_delete_preserves_lock_identity_for_existing_waiters(tmp_path, monkeypatch):
    from api.sessions.repository import delete_session_state

    _isolate_session_store(tmp_path, monkeypatch)
    sid = "deletewaiterlock1"
    Session(
        session_id=sid,
        messages=[{"role": "user", "content": "delete safely"}],
    ).save()
    _patch_cleanup_collaborators(monkeypatch, tmp_path)
    monkeypatch.setattr(session_cleanup, "delete_cli_session", lambda value: True)
    waiter_lock = config._get_session_agent_lock(sid)

    delete_session_state(sid, messaging=False)

    assert config._get_session_agent_lock(sid) is waiter_lock


def test_session_lock_alias_rejects_a_different_new_session_owner():
    old_sid = "compression-old-lock"
    new_sid = "compression-new-lock"
    old_lock = config._get_session_agent_lock(old_sid)
    new_lock = config._get_session_agent_lock(new_sid)

    with pytest.raises(RuntimeError, match="another lock owner"):
        config.alias_session_agent_lock(old_sid, new_sid, old_lock)

    assert config._get_session_agent_lock(old_sid) is old_lock
    assert config._get_session_agent_lock(new_sid) is new_lock


def test_session_lock_registry_releases_unreferenced_lock():
    sid = "weak-session-lock"
    lock = config._get_session_agent_lock(sid)
    reference = weakref.ref(lock)

    del lock
    gc.collect()

    assert reference() is None
    assert sid not in config.SESSION_AGENT_LOCKS


def test_delete_session_state_refuses_a_live_worker(tmp_path, monkeypatch):
    from api.sessions.repository import SessionActiveError, delete_session_state

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "activedelete1"
    Session(
        session_id=sid,
        active_stream_id="stream-1",
        pending_user_message="still running",
        pending_started_at=1,
        messages=[{"role": "user", "content": "do not resurrect me"}],
    ).save()
    monkeypatch.setattr(
        config,
        "blocking_runtime_stream",
        lambda session_id, **kwargs: "stream-1",
    )
    owner_lock = config._get_session_agent_lock(sid)

    with pytest.raises(SessionActiveError, match="stream-1"):
        delete_session_state(sid, messaging=False)

    assert (session_dir / f"{sid}.json").exists()
    assert config._get_session_agent_lock(sid) is owner_lock
    assert sid not in models._load_webui_deleted_session_tombstone()
