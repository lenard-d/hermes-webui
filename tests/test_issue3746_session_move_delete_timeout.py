"""Regression tests for #3746 — timeouts on session move / project delete
during active streaming.

Two distinct root causes, two fixes:

A) /api/session/move acquired the per-session agent lock with a bare, unbounded
   `with _get_session_agent_lock(sid):`. The streaming thread holds that same
   lock during checkpoint saves, so on slow file I/O (WSL/DrvFs) the move could
   block past the client's 30s abort and surface as a silent "Request timed out"
   toast. Fix: bounded `acquire(timeout=5)` → HTTP 503 on contention.

B) /api/projects/delete unlinked every assigned session with a full
   get_session() + s.save() (O(N) full-messages reserialize). For a project with
   many messageful sessions that throughput alone blows past 30s. Fix: skip
   actively-streaming sessions (largest arrays + race the writer) and guard each
   per-session save so one slow/failing session can't abort the whole request.
"""
import json
import threading
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest
from tests.test_sessions_split_support import SESSIONS_SOURCE


# ── A) Behavioral: the bounded lock acquire actually returns instead of blocking ──

class TestBoundedLockAcquire:
    """The fix relies on threading.Lock.acquire(timeout=...) returning False
    promptly when the lock is held, instead of blocking forever. Pin that
    primitive behavior so the move handler's 503 path is reachable."""

    def test_acquire_timeout_returns_false_when_held(self):
        lock = threading.Lock()
        lock.acquire()  # simulate the streaming thread holding it
        try:
            import time
            t0 = time.monotonic()
            acquired = lock.acquire(timeout=0.2)
            elapsed = time.monotonic() - t0
            assert acquired is False, "bounded acquire must give up, not block, when the lock is held"
            assert elapsed < 1.0, f"bounded acquire must return near its timeout, took {elapsed:.2f}s"
        finally:
            lock.release()

    def test_acquire_succeeds_when_free(self):
        lock = threading.Lock()
        acquired = lock.acquire(timeout=0.2)
        assert acquired is True
        lock.release()


# ── A) Observable move/repository behavior ──


def test_move_uses_bounded_lock_acquire():
    from api.sessions.repository import SessionBusyError, SessionRepository

    lock = threading.Lock()
    lock.acquire()
    repository = SessionRepository(
        load=lambda _sid: SimpleNamespace(session_id="move-busy"),
        load_full=lambda _sid: None,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )
    try:
        with pytest.raises(SessionBusyError):
            with repository.edit("move-busy", lock_timeout=0.01):
                pass
    finally:
        lock.release()


def test_move_returns_503_on_lock_contention(monkeypatch):
    import api.routes as routes
    from api.sessions.repository import SessionBusyError

    session = SimpleNamespace(
        session_id="move-busy-route",
        project_id=None,
        profile=None,
        workspace="/tmp",
        compact=lambda: {"session_id": "move-busy-route"},
    )

    @contextmanager
    def busy_edit(*_args, **_kwargs):
        raise SessionBusyError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid: session)
    monkeypatch.setattr(routes, "edit_session", busy_edit)
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.update(
            payload=payload, status=status
        ),
    )
    body = json.dumps({"session_id": session.session_id}).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        rfile=BytesIO(body),
    )

    routes.handle_post(handler, SimpleNamespace(path="/api/session/move"))

    assert captured["status"] == 503
    assert "busy" in captured["payload"]["error"].lower()


def test_move_releases_lock_in_finally():
    from api.sessions.repository import SessionRepository

    lock = threading.Lock()
    session = SimpleNamespace(session_id="move-release", save=lambda **_kwargs: None)
    repository = SessionRepository(
        load=lambda _sid: session,
        load_full=lambda _sid: None,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    with pytest.raises(RuntimeError, match="mutation failed"):
        with repository.edit("move-release", lock_timeout=0.1):
            raise RuntimeError("mutation failed")

    assert lock.acquire(blocking=False) is True
    lock.release()


# ── B) Observable project-delete behavior ──


def _project_delete_context(index_file, **overrides):
    import api.routes as route_facade

    ctx = dict(vars(route_facade))
    ctx.update(
        SESSION_INDEX_FILE=index_file,
        LOCK=threading.Lock(),
        SESSIONS={},
        _active_stream_ids=lambda: set(),
        load_projects=lambda: [
            {"project_id": "project-1", "profile": "default", "name": "Project"}
        ],
        save_projects=lambda _projects: None,
        get_active_profile_name=lambda: "default",
        _profiles_match=lambda left, right: left == right,
        j=lambda _handler, payload, status=200, **_kwargs: {
            "payload": payload,
            "status": status,
        },
    )
    ctx.update(overrides)
    return ctx


def _delete_project(ctx):
    from api.http.routes import session_organization_mutations

    return session_organization_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/projects/delete"),
        {"project_id": "project-1"},
        None,
        ctx,
    )


def test_delete_clears_project_id_on_streaming_sessions_in_cache(tmp_path):
    index_file = tmp_path / "_index.json"
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": "streaming-session",
                    "project_id": "project-1",
                    "active_stream_id": "stream-1",
                }
            ]
        ),
        encoding="utf-8",
    )
    cached = SimpleNamespace(project_id="project-1")
    saved_projects = []
    loaded_sessions = []

    def unexpected_load(session_id):
        loaded_sessions.append(session_id)
        raise AssertionError("a cached streaming session must not be loaded and saved separately")

    ctx = _project_delete_context(
        index_file,
        SESSIONS={"streaming-session": cached},
        _active_stream_ids=lambda: {"stream-1"},
        get_session=unexpected_load,
        save_projects=lambda projects: saved_projects.append(projects),
    )

    response = _delete_project(ctx)

    assert response == {"payload": {"ok": True}, "status": 200}
    assert cached.project_id is None
    assert loaded_sessions == []
    assert saved_projects == [[]]


def test_delete_continues_after_one_session_save_fails(tmp_path):
    index_file = tmp_path / "_index.json"
    index_file.write_text(
        json.dumps(
            [
                {"session_id": "save-fails", "project_id": "project-1"},
                {"session_id": "save-succeeds", "project_id": "project-1"},
            ]
        ),
        encoding="utf-8",
    )
    attempts = []

    class SessionStub:
        def __init__(self, session_id, *, fail=False):
            self.session_id = session_id
            self.project_id = "project-1"
            self.fail = fail

        def save(self):
            attempts.append((self.session_id, self.project_id))
            if self.fail:
                raise OSError("simulated slow-storage failure")

    sessions = {
        "save-fails": SessionStub("save-fails", fail=True),
        "save-succeeds": SessionStub("save-succeeds"),
    }
    ctx = _project_delete_context(
        index_file,
        get_session=lambda session_id: sessions[session_id],
    )

    response = _delete_project(ctx)

    assert response == {"payload": {"ok": True}, "status": 200}
    assert attempts == [("save-fails", None), ("save-succeeds", None)]


def test_delete_rejects_a_project_owned_by_another_profile(tmp_path):
    index_file = tmp_path / "_index.json"
    index_file.write_text("[]", encoding="utf-8")
    mutations = []
    ctx = _project_delete_context(
        index_file,
        load_projects=lambda: [
            {"project_id": "project-1", "profile": "profile-a", "name": "Project"}
        ],
        get_active_profile_name=lambda: "profile-b",
        save_projects=lambda projects: mutations.append(projects),
        bad=lambda _handler, message, status=400: {
            "payload": {"error": message},
            "status": status,
        },
    )

    response = _delete_project(ctx)

    assert response == {"payload": {"error": "Project not found"}, "status": 404}
    assert mutations == []


# ── Frontend: the '+ New project and move' shortcut guards the new 503 ──

SESSIONS_JS = SESSIONS_SOURCE


def test_new_project_and_move_shortcut_guards_503():
    """The '+ New project and move' shortcut must catch a failed move (e.g. 503
    when the session is streaming) instead of leaving an unhandled rejection (#3746)."""
    idx = SESSIONS_JS.find("createItem.onclick=async()=>{")
    assert idx > 0, "the new-project-and-move shortcut not found"
    block = SESSIONS_JS[idx:idx + 1200]
    assert "try{" in block and "}catch(e){" in block, (
        "the new-project-and-move move call must be wrapped in try/catch (#3746)"
    )
    assert "move failed" in block.lower(), (
        "a failed move must surface an actionable toast (#3746)"
    )
    # The authoritative refetch (#2551) must remain in the success path.
    assert "await renderSessionList()" in block, (
        "the shortcut must keep its authoritative /api/sessions refetch (#2551)"
    )
