import sqlite3
from pathlib import Path
from types import SimpleNamespace

import api.routes as routes
import api.sessions.cleanup as session_cleanup
import api.sessions.materialization as session_materialization
import api.sessions.state_db as session_external
import api.sessions.records as session_records
from api.sessions import foreign_session_access, session_sidebar_projection
from api.sessions.records import SESSIONS, Session


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(session_records, "SESSION_DIR", session_dir)
    monkeypatch.setattr(
        session_records, "SESSION_INDEX_FILE", session_dir / "_index.json"
    )
    SESSIONS.clear()
    return session_dir


def _worktree_session(tmp_path, session_id):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / f"hermes-{session_id}"
    worktree.mkdir(parents=True)
    s = Session(
        session_id=session_id,
        title="Worktree session",
        workspace=str(worktree),
        worktree_path=str(worktree),
        worktree_branch=f"hermes/{session_id}",
        worktree_repo_root=str(repo),
    )
    s.save()
    return s, worktree


def _make_state_db(path, sid, *, source="telegram"):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            message_count INTEGER DEFAULT 0,
            started_at REAL,
            title TEXT,
            cwd TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, model, message_count, started_at, title, cwd) "
        "VALUES (?, ?, 'MiniMax-M3', 2, 1781024055.0, 'Telegram chat', ?)",
        (sid, source, str(path.parent)),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', 1781024055.0)",
        (sid,),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', 'hello', 1781024056.0)",
        (sid,),
    )
    conn.commit()
    conn.close()


def test_delete_worktree_session_reports_retained_worktree_without_cleanup(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session, worktree = _worktree_session(tmp_path, "wtdelete1")
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(session_cleanup, "delete_cli_session", lambda sid: True)
    assert (session_dir / f"{session.session_id}.json").exists()

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert captured["payload"]["worktree_retained"] is True
    assert captured["payload"]["worktree_path"] == str(worktree.resolve())
    assert captured["payload"]["worktree_branch"] == "hermes/wtdelete1"
    assert not (session_dir / "wtdelete1.json").exists()
    assert worktree.exists(), "session delete must not remove the git worktree directory"


def test_delete_session_records_tombstone_when_state_db_delete_fails(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "dbfaildelete1"
    session = Session(
        session_id=sid,
        title="Delete failure",
        messages=[{"role": "user", "content": "keep deleted"}],
    )
    session.save()
    (session_dir / f"{sid}.json.bak").write_text("backup", encoding="utf-8")
    assert (session_dir / f"{sid}.json").exists()
    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)

    def fail_delete(value):
        raise RuntimeError("state.db locked")

    real_unlink = Path.unlink

    def fail_backup_unlink(path, *args, **kwargs):
        if path.name == f"{sid}.json.bak":
            raise PermissionError("backup locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(session_cleanup, "delete_cli_session", fail_delete)
    monkeypatch.setattr(Path, "unlink", fail_backup_unlink)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is True
    assert not (session_dir / f"{sid}.json").exists()
    assert sid in session_records._load_webui_deleted_session_tombstone()


def test_delete_messaging_session_reopens_read_only_without_deleted_webui_tombstone(
    tmp_path, monkeypatch
):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "telegramdelete1"
    state_db = tmp_path / "state.db"
    _make_state_db(state_db, sid)
    monkeypatch.setattr(session_external, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(
        session_materialization, "_active_state_db_path", lambda: state_db
    )
    session = Session(session_id=sid, title="Telegram chat")
    session.save()
    assert (session_dir / f"{sid}.json").exists()
    captured = _capture_post(monkeypatch, {"session_id": sid})
    cli_meta = {
        "session_id": sid,
        "source_tag": "telegram",
        "raw_source": "telegram",
        "session_source": "messaging",
    }
    monkeypatch.setattr(
        foreign_session_access,
        "metadata",
        lambda value: cli_meta,
    )
    monkeypatch.setattr(
        session_sidebar_projection,
        "is_messaging_session",
        lambda value: True,
    )
    delete_calls = []
    monkeypatch.setattr(
        session_cleanup,
        "delete_cli_session",
        lambda value: delete_calls.append(value),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    sess, reason = foreign_session_access.claim(sid)

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert not (session_dir / f"{sid}.json").exists()
    assert sid not in session_records._load_webui_deleted_session_tombstone()
    assert delete_calls == []
    assert reason == "not_claimable"
    assert sess is not None
    assert sess.read_only is True
    assert sess.session_source == "messaging"


def test_delete_active_session_fails_closed_with_conflict(tmp_path, monkeypatch):
    from api.sessions.repository import SessionActiveError

    _isolate_session_store(tmp_path, monkeypatch)
    sid = "activedeleteconflict1"
    Session(session_id=sid, title="Still running").save()
    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: captured.update(
            payload={"error": message},
            status=status,
        )
        or True,
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(
        routes,
        "delete_session_state",
        lambda value, *, messaging: (_ for _ in ()).throw(
            SessionActiveError(value, "stream-1")
        ),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 409
    assert captured["payload"]["error"] == (
        "Stop the active response before deleting this session"
    )


def test_archive_worktree_session_reports_retained_worktree_without_cleanup(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    session, worktree = _worktree_session(tmp_path, "wtarchive1")
    captured = _capture_post(
        monkeypatch,
        {"session_id": session.session_id, "archived": True},
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["session"]["archived"] is True
    assert captured["payload"]["worktree_retained"] is True
    assert captured["payload"]["worktree_path"] == str(worktree.resolve())
    assert worktree.exists(), "session archive must not remove the git worktree directory"
    assert Session.load("wtarchive1").archived is True
