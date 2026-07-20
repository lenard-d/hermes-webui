"""Attachment writes and session deletion share one per-session owner."""

from __future__ import annotations

import io
import threading
import zipfile

import api.config as config
import api.sessions.store as models
import api.upload as upload
import pytest
from api.sessions.store import Session
from api.sessions.repository import delete_session_state
from tests.test_raw_audio_upload import _FakeHandler, _multipart_body


class _ObservedOwnerLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.attempted = {
            "delete": threading.Event(),
            "upload": threading.Event(),
        }
        self.acquired = {
            "delete": threading.Event(),
            "upload": threading.Event(),
        }
        self._owner_ident = None

    def __enter__(self):
        role = threading.current_thread().name
        self.attempted[role].set()
        self._lock.acquire()
        self._owner_ident = threading.get_ident()
        self.acquired[role].set()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self._owner_ident = None
        self._lock.release()
        return False

    def held_by_current_thread(self) -> bool:
        return self._owner_ident == threading.get_ident()


def _zip_payload() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("inside.txt", b"archive payload")
    return payload.getvalue()


@pytest.fixture
def isolated_attachment_session(tmp_path, monkeypatch):
    prior_sessions = dict(models.SESSIONS)
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    attachment_root = tmp_path / "attachments"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(attachment_root))
    models.SESSIONS.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "attachment-race-session"
    Session(session_id=sid, messages=[{"role": "user", "content": "keep"}]).save()
    owner_lock = _ObservedOwnerLock()
    original_lock_for = config._get_session_agent_lock
    monkeypatch.setattr(
        config,
        "_get_session_agent_lock",
        lambda value: owner_lock if value == sid else original_lock_for(value),
    )
    original_visibility_check = upload._session_visible_to_active_profile

    def visibility_check(session):
        assert owner_lock.held_by_current_thread()
        return original_visibility_check(session)

    monkeypatch.setattr(upload, "_session_visible_to_active_profile", visibility_check)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    for target in (
        "api.turn_journal.delete_turn_journal",
        "api.runs.delete_run_journal",
        "api.background_process.forget_bg_task_completion_dedup",
        "api.terminal.close_terminal",
    ):
        monkeypatch.setattr(target, lambda _sid: None)

    yield sid, owner_lock, attachment_root

    models.SESSIONS.clear()
    models.SESSIONS.update(prior_sessions)
    config.SESSION_AGENT_LOCKS.clear()


def _handler_for(kind: str, sid: str) -> tuple[_FakeHandler, object]:
    if kind == "upload":
        filename, payload = "note.txt", b"plain attachment"
        callback = upload.handle_upload
    else:
        filename, payload = "bundle.zip", _zip_payload()
        callback = upload.handle_upload_extract
    body, content_type = _multipart_body(
        fields={"session_id": sid},
        files={"file": (filename, payload, "application/octet-stream")},
        boundary=b"attachment-race-boundary",
    )
    return _FakeHandler(body, content_type), callback


def _start(role: str, callback, errors: list[BaseException]) -> threading.Thread:
    def run():
        try:
            callback()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name=role)
    thread.start()
    return thread


def _join(thread: threading.Thread) -> None:
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize("kind", ["upload", "extract"])
def test_delete_owner_wins_before_attachment_write(
    kind,
    isolated_attachment_session,
    monkeypatch,
):
    sid, owner_lock, attachment_root = isolated_attachment_session
    delete_in_owner = threading.Event()
    finish_delete = threading.Event()
    errors = []

    def block_delete_in_owner(_sid, **_kwargs):
        assert owner_lock.held_by_current_thread()
        delete_in_owner.set()
        assert finish_delete.wait(timeout=2)
        return None

    monkeypatch.setattr(config, "blocking_runtime_stream", block_delete_in_owner)
    delete_thread = _start(
        "delete",
        lambda: delete_session_state(sid, messaging=False),
        errors,
    )
    assert delete_in_owner.wait(timeout=2)

    handler, callback = _handler_for(kind, sid)
    upload_thread = _start("upload", lambda: callback(handler), errors)
    assert owner_lock.attempted["upload"].wait(timeout=2)
    assert not owner_lock.acquired["upload"].is_set()

    finish_delete.set()
    _join(delete_thread)
    _join(upload_thread)

    assert errors == []
    assert handler.status == 404
    assert handler.payload() == {"error": "Session not found"}
    assert not (attachment_root / sid).exists()


@pytest.mark.parametrize("kind", ["upload", "extract"])
def test_attachment_write_owner_wins_then_delete_removes_every_artifact(
    kind,
    isolated_attachment_session,
    monkeypatch,
):
    sid, owner_lock, attachment_root = isolated_attachment_session
    operation_in_owner = threading.Event()
    finish_upload = threading.Event()
    errors = []

    if kind == "upload":
        original_operation = upload._upload_destination

        def block_operation(*args, **kwargs):
            assert owner_lock.held_by_current_thread()
            operation_in_owner.set()
            assert finish_upload.wait(timeout=2)
            return original_operation(*args, **kwargs)

        monkeypatch.setattr(upload, "_upload_destination", block_operation)
    else:
        original_operation = upload.extract_archive

        def block_operation(*args, **kwargs):
            assert owner_lock.held_by_current_thread()
            operation_in_owner.set()
            assert finish_upload.wait(timeout=2)
            return original_operation(*args, **kwargs)

        monkeypatch.setattr(upload, "extract_archive", block_operation)

    handler, callback = _handler_for(kind, sid)
    upload_thread = _start("upload", lambda: callback(handler), errors)
    assert operation_in_owner.wait(timeout=2)

    delete_thread = _start(
        "delete",
        lambda: delete_session_state(sid, messaging=False),
        errors,
    )
    assert owner_lock.attempted["delete"].wait(timeout=2)
    assert not owner_lock.acquired["delete"].is_set()

    finish_upload.set()
    _join(upload_thread)
    _join(delete_thread)

    assert errors == []
    assert handler.status == 200
    assert not (attachment_root / sid).exists()
