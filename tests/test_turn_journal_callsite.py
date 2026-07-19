"""Behavioral contract for the local turn-admission journal seam."""

from __future__ import annotations

import threading

from api import turn_admission


class _Session:
    session_id = "turn-journal-session"
    profile = None
    title = "Existing title"
    active_stream_id = None
    pending_user_message = None
    pending_started_at = None
    messages = []
    worktree_path = None

    def __init__(self, order):
        self.order = order

    def save(self, *args, **kwargs):
        self.order.append("persisted")


def _request():
    return turn_admission.LocalTurnRequest(
        message="write the journal first",
        attachments=[],
        workspace="/tmp/workspace",
        model="test-model",
    )


def test_local_turn_is_persisted_and_journaled_before_publication_and_worker(monkeypatch):
    order = []
    worker_finished = threading.Event()
    session = _Session(order)

    def append(session_id, event):
        assert session_id == session.session_id
        assert event["event"] == "submitted"
        assert event["role"] == "user"
        order.append("journaled")
        return {"turn_id": "turn-1"}

    def register(stream_id, session_id, channel, *, goal_related=False):
        assert session_id == session.session_id
        order.append("published")

    def worker(*args, **kwargs):
        order.append("worker")
        worker_finished.set()

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "register_runtime_stream", register)
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)

    result = turn_admission.start_local_turn(
        session,
        _request(),
        worker_target=worker,
        clear_stale_stream=lambda _session: False,
    )

    assert worker_finished.wait(2)
    assert order == ["persisted", "journaled", "published", "worker"]
    assert result["turn_id"] == "turn-1"


def test_journal_failure_does_not_publish_before_persistence_or_prevent_worker(monkeypatch):
    order = []
    worker_finished = threading.Event()
    session = _Session(order)

    def fail_journal(*_args, **_kwargs):
        order.append("journal_failed")
        raise OSError("journal unavailable")

    def register(*_args, **_kwargs):
        order.append("published")

    def worker(*_args, **_kwargs):
        order.append("worker")
        worker_finished.set()

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", fail_journal)
    monkeypatch.setattr(turn_admission, "register_runtime_stream", register)
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)

    result = turn_admission.start_local_turn(
        session,
        _request(),
        worker_target=worker,
        clear_stale_stream=lambda _session: False,
    )

    assert worker_finished.wait(2)
    assert order == ["persisted", "journal_failed", "published", "worker"]
    assert result["turn_id"] is None


def test_local_turn_refuses_a_session_deleted_while_waiting_for_its_lock(monkeypatch):
    order = []
    session = _Session(order)
    monkeypatch.setattr(
        turn_admission,
        "session_deleted_for_write",
        lambda sid: sid == session.session_id,
        raising=False,
    )
    monkeypatch.setattr(
        turn_admission,
        "register_runtime_stream",
        lambda *_args, **_kwargs: order.append("published"),
    )

    result = turn_admission.start_local_turn(
        session,
        _request(),
        worker_target=lambda *_args, **_kwargs: order.append("worker"),
        clear_stale_stream=lambda _session: False,
    )

    assert result == {"error": "Session not found", "_status": 404}
    assert order == []
