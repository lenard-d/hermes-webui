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
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_sessions_split_support import SESSIONS_SOURCE

ROUTES_SRC = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")


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
    from api.session_repository import SessionBusyError, SessionRepository

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
    from api.session_repository import SessionBusyError

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
    from api.session_repository import SessionRepository

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


# ── B) Structural: project delete skips streaming sessions + guards each save ──

def _delete_block():
    idx = ROUTES_SRC.find('"/api/projects/delete"')
    assert idx > 0, "projects/delete handler not found"
    end = ROUTES_SRC.find('"/api/session/import"', idx)
    return ROUTES_SRC[idx:end]


def test_delete_clears_project_id_on_streaming_sessions_in_cache():
    block = _delete_block()
    assert "_active_stream_ids()" in block, (
        "projects/delete must compute the active stream set to special-case streaming sessions (#3746)"
    )
    assert 'entry.get("active_stream_id") in active_ids' in block, (
        "projects/delete must detect sessions whose active_stream_id is currently streaming (#3746)"
    )
    # The streaming session's project_id must be cleared on the LIVE CACHED object
    # (so the streaming thread persists it) — NOT left dangling, and NOT given a
    # competing s.save() that races the streaming writer.
    assert "cached.project_id = None" in block, (
        "projects/delete must clear project_id on the live cached streaming session so the "
        "streaming thread persists the unlink — not leave a dangling pointer to a deleted project (#3746)"
    )
    assert "with LOCK:" in block, (
        "the cached-object mutation must happen under the session cache LOCK (#3746)"
    )


def test_delete_guards_each_session_save():
    block = _delete_block()
    # Each per-session update stays wrapped in try/except so one slow/failing
    # session can't abort the whole delete.
    assert "try:" in block and "except Exception:" in block, (
        "projects/delete must guard each per-session update (#3746)"
    )
    # The active-profile ownership guard (#1614) must remain intact.
    assert '_profiles_match(proj.get("profile"), active_profile)' in block, (
        "projects/delete must keep its cross-profile ownership guard (#1614)"
    )


# ── Frontend: the '+ New project and move' shortcut guards the new 503 ──

SESSIONS_JS = SESSIONS_SOURCE


def test_new_project_and_move_shortcut_guards_503():
    """The '+ New project and move' shortcut must catch a failed move (e.g. 503
    when the session is streaming) instead of leaving an unhandled rejection (#3746)."""
    idx = SESSIONS_JS.find("Guard the move so a 503")
    assert idx > 0, "the new-project-and-move shortcut guard not found"
    block = SESSIONS_JS[idx:idx + 700]
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
