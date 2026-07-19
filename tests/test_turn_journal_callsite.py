"""Behavioral contract for the local turn-admission journal seam."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from api import config, turn_admission


@pytest.fixture(autouse=True)
def _use_fake_session_repository(monkeypatch):
    active_session = [None]

    @contextmanager
    def write_owner(_sid, *, session=None):
        if session is not None:
            active_session[0] = session
        yield active_session[0]

    def compensate(
        _sid,
        *,
        expected_stream_id,
        restore,
        on_committed=None,
        baseline_persisted=True,
    ):
        session = active_session[0]
        if session.active_stream_id != expected_stream_id:
            return False
        restore(session)
        if baseline_persisted:
            session.save(
                touch_updated_at=False,
                skip_index=True,
                _admission_compensation=True,
            )
        if on_committed is not None:
            on_committed()
        return True

    monkeypatch.setattr(turn_admission, "admission_write_owner", write_owner)
    monkeypatch.setattr(turn_admission, "compensate_failed_admission", compensate)


class _Session:
    def __init__(self, order):
        self.order = order
        self.session_id = "turn-journal-session"
        self.profile = None
        self.title = "Existing title"
        self.workspace = "/previous/workspace"
        self.model = "previous-model"
        self.model_provider = "previous-provider"
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.pending_user_source = None
        self.post_compression_context_tokens_estimate = 42
        self.messages = []
        self.truncation_watermark = 123
        self.updated_at = 456
        self.worktree_path = None

    def save(self, *args, **kwargs):
        self.order.append("persisted")


def _request():
    return turn_admission.LocalTurnRequest(
        message="write the journal first",
        attachments=[],
        workspace="/tmp/workspace",
        model="test-model",
    )


def _written_event(session_id, event):
    return {**event, "session_id": session_id}


def _assert_admission_rolled_back(session):
    assert session.title == "Existing title"
    assert session.workspace == "/previous/workspace"
    assert session.model == "previous-model"
    assert session.model_provider == "previous-provider"
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.pending_attachments == []
    assert session.pending_started_at is None
    assert session.pending_user_source is None
    assert session.post_compression_context_tokens_estimate == 42
    assert session.messages == []
    assert session.truncation_watermark == 123
    assert session.updated_at == 456


def test_local_turn_is_persisted_and_journaled_before_publication_and_worker(monkeypatch):
    order = []
    session = _Session(order)

    def append(session_id, event):
        assert session_id == session.session_id
        assert event["event"] == "submitted"
        assert event["role"] == "user"
        order.append("journaled")
        return _written_event(session_id, event)

    def register(stream_id, session_id, channel, *, goal_related=False):
        assert session_id == session.session_id
        order.append("published")

    def worker(*args, **kwargs):
        order.append("worker")

    class InlineThread:
        def __init__(self, *, target, args, kwargs, daemon):
            self.target = target
            self.args = args
            self.kwargs = kwargs

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "register_runtime_stream", register)
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)
    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=InlineThread),
    )

    result = turn_admission.start_local_turn(
        session,
        _request(),
        worker_target=worker,
        clear_stale_stream=lambda _session: False,
    )

    assert order == ["persisted", "journaled", "published", "worker"]
    assert isinstance(result["turn_id"], str) and result["turn_id"]


def test_journal_failure_rolls_back_pending_state_and_prevents_publication(monkeypatch):
    order = []
    session = _Session(order)

    def fail_journal(*_args, **_kwargs):
        order.append("journal_failed")
        raise OSError("journal unavailable")

    def register(*_args, **_kwargs):
        order.append("published")

    def worker(*_args, **_kwargs):
        order.append("worker")

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", fail_journal)
    monkeypatch.setattr(turn_admission, "register_runtime_stream", register)
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)

    with pytest.raises(OSError, match="journal unavailable"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=worker,
            clear_stale_stream=lambda _session: False,
        )

    assert order == ["persisted", "journal_failed", "persisted"]
    _assert_admission_rolled_back(session)


def test_unknown_submitted_commit_keeps_pending_owner_and_consumed_markers(monkeypatch):
    order = []
    session = _Session(order)
    goal_markers = {session.session_id}
    background_markers = {session.session_id}

    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("append result unavailable")
        ),
    )
    monkeypatch.setattr(
        turn_admission,
        "confirm_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            turn_admission.TurnJournalCommitUnknown("commit state unknown")
        ),
    )
    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )

    with pytest.raises(
        turn_admission.TurnJournalCommitUnknown,
        match="commit state unknown",
    ):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
        )

    assert order == ["persisted"]
    assert session.active_stream_id
    assert session.pending_user_message == _request().message
    assert goal_markers == set()
    assert background_markers == set()


def test_stream_registration_failure_interrupts_journal_and_rolls_back(monkeypatch):
    order = []
    session = _Session(order)
    submitted_turn_id = []

    def append(session_id, event):
        assert session_id == session.session_id
        order.append(event["event"])
        if event["event"] == "submitted":
            submitted_turn_id.append(event["turn_id"])
            return _written_event(session_id, event)
        assert event["event"] == "interrupted"
        assert event["turn_id"] == submitted_turn_id[0]
        return _written_event(session_id, event)

    def register(*_args, **_kwargs):
        order.append("registration_failed")
        raise RuntimeError("stream registration unavailable")

    def worker(*_args, **_kwargs):
        order.append("worker")

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "register_runtime_stream", register)
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)
    monkeypatch.setattr(
        turn_admission,
        "finish_runtime_run",
        lambda _stream_id: order.append("runtime_cleaned"),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="stream registration unavailable"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=worker,
            clear_stale_stream=lambda _session: False,
        )

    assert order == [
        "persisted",
        "submitted",
        "registration_failed",
        "runtime_cleaned",
        "interrupted",
        "persisted",
    ]
    _assert_admission_rolled_back(session)


def test_post_commit_terminal_error_is_confirmed_before_rollback(monkeypatch):
    order = []
    session = _Session(order)

    def append(session_id, event):
        order.append(event["event"])
        if event["event"] == "submitted":
            return _written_event(session_id, event)
        raise OSError("terminal append result unavailable")

    def confirm(session_id, event):
        assert event["event"] == "interrupted"
        order.append("terminal_confirmed")
        return _written_event(session_id, event)

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "confirm_turn_journal_event", confirm)
    monkeypatch.setattr(
        turn_admission,
        "register_runtime_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stream registration unavailable")
        ),
    )
    monkeypatch.setattr(
        turn_admission,
        "finish_runtime_run",
        lambda _stream_id: order.append("runtime_cleaned"),
    )

    with pytest.raises(RuntimeError, match="stream registration unavailable"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
        )

    assert order == [
        "persisted",
        "submitted",
        "runtime_cleaned",
        "interrupted",
        "terminal_confirmed",
        "persisted",
    ]
    _assert_admission_rolled_back(session)


@pytest.mark.parametrize("confirmation", ["absent", "unknown"])
def test_unconfirmed_terminal_append_keeps_pending_owner_and_markers(
    monkeypatch, confirmation
):
    order = []
    session = _Session(order)
    goal_markers = {session.session_id}
    background_markers = {session.session_id}

    def append(session_id, event):
        order.append(event["event"])
        if event["event"] == "submitted":
            return _written_event(session_id, event)
        raise OSError("terminal append result unavailable")

    def confirm(_session_id, event):
        assert event["event"] == "interrupted"
        if confirmation == "unknown":
            raise turn_admission.TurnJournalCommitUnknown("terminal state unknown")
        return None

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "confirm_turn_journal_event", confirm)
    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )
    monkeypatch.setattr(
        turn_admission,
        "register_runtime_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stream registration unavailable")
        ),
    )
    monkeypatch.setattr(turn_admission, "finish_runtime_run", lambda _stream_id: None)

    with pytest.raises(RuntimeError, match="stream registration unavailable"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
        )

    assert order == ["persisted", "submitted", "interrupted"]
    assert session.active_stream_id
    assert session.pending_user_message == _request().message
    assert goal_markers == set()
    assert background_markers == set()


def test_worker_start_failure_restores_consumed_markers_and_rolls_back(monkeypatch):
    order = []
    session = _Session(order)
    goal_markers = {session.session_id}
    background_markers = {session.session_id}
    submitted_turn_id = []

    def append(session_id, event):
        assert session_id == session.session_id
        order.append(event["event"])
        if event["event"] == "submitted":
            submitted_turn_id.append(event["turn_id"])
            return _written_event(session_id, event)
        assert event["event"] == "interrupted"
        assert event["turn_id"] == submitted_turn_id[0]
        return _written_event(session_id, event)

    def register(*_args, goal_related=False, **_kwargs):
        assert goal_related is True
        order.append("published")

    class FailingThread:
        def __init__(self, *args, **kwargs):
            order.append("thread_created")

        def start(self):
            order.append("worker_start_failed")
            raise RuntimeError("worker unavailable")

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "register_runtime_stream", register)
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)
    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=FailingThread),
    )
    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )
    monkeypatch.setattr(
        turn_admission,
        "finish_runtime_run",
        lambda _stream_id: order.append("runtime_cleaned"),
    )

    with pytest.raises(RuntimeError, match="worker unavailable"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=lambda *_args, **_kwargs: order.append("worker"),
            clear_stale_stream=lambda _session: False,
        )

    assert order == [
        "persisted",
        "submitted",
        "thread_created",
        "published",
        "worker_start_failed",
        "runtime_cleaned",
        "interrupted",
        "persisted",
    ]
    assert goal_markers == {session.session_id}
    assert background_markers == {session.session_id}
    _assert_admission_rolled_back(session)


def test_runtime_cleanup_failure_keeps_pending_owner_and_markers_consumed(monkeypatch):
    order = []
    session = _Session(order)
    stream_id = "runtime-cleanup-failure-stream"
    goal_markers = {session.session_id}
    background_markers = {session.session_id}
    journal_events = []

    def append(session_id, event):
        assert session_id == session.session_id
        journal_events.append(event["event"])
        return _written_event(session_id, event)

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("worker unavailable")

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )
    monkeypatch.setattr(
        turn_admission,
        "uuid",
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=stream_id)),
    )
    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=FailingThread),
    )
    monkeypatch.setattr(
        turn_admission,
        "finish_runtime_run",
        lambda _stream_id: (_ for _ in ()).throw(
            RuntimeError("runtime cleanup unavailable")
        ),
    )

    try:
        with pytest.raises(RuntimeError, match="runtime cleanup unavailable"):
            turn_admission.start_local_turn(
                session,
                _request(),
                worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
                clear_stale_stream=lambda _session: False,
            )

        assert config.runtime_stream_alive(stream_id) is True
        assert config.runtime_run_session_id(stream_id) == session.session_id
        assert session.active_stream_id == stream_id
        assert session.pending_user_message == _request().message
        assert goal_markers == set()
        assert background_markers == set()
        assert journal_events == ["submitted"]
    finally:
        config.finish_runtime_run(stream_id)


def test_initial_pending_save_failure_restores_markers_and_session(monkeypatch):
    order = []
    session = _Session(order)
    goal_markers = {session.session_id}
    background_markers = {session.session_id}
    save_calls = 0

    def fail_first_save(*_args, **_kwargs):
        nonlocal save_calls
        save_calls += 1
        order.append("persisted")
        if save_calls == 1:
            raise OSError("index unavailable")

    session.save = fail_first_save
    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )
    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: pytest.fail("journal must not be called"),
    )

    with pytest.raises(OSError, match="index unavailable"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
        )

    assert order == ["persisted", "persisted"]
    assert goal_markers == {session.session_id}
    assert background_markers == {session.session_id}
    _assert_admission_rolled_back(session)


def test_marker_claim_waits_until_pending_checkpoint_setup_succeeds(monkeypatch):
    order = []
    session = _Session(order)
    goal_markers = {session.session_id}
    background_markers = {session.session_id}

    class FailingDiagnostics:
        def stage(self, name):
            if name == "save_pending_state":
                raise RuntimeError("diagnostics unavailable")

    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )
    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: pytest.fail("journal must not be called"),
    )

    with pytest.raises(RuntimeError, match="diagnostics unavailable"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
            diag=FailingDiagnostics(),
        )

    assert goal_markers == {session.session_id}
    assert background_markers == {session.session_id}
    assert order == []
    _assert_admission_rolled_back(session)


def test_hidden_session_is_not_published_when_admission_fails(monkeypatch):
    order = []
    session = _Session(order)
    session.title = "Untitled"
    session.messages = []
    published = []

    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        _written_event,
    )
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(
        turn_admission,
        "register_runtime_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )
    monkeypatch.setattr(turn_admission, "finish_runtime_run", lambda _stream_id: None)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)
    monkeypatch.setattr(
        turn_admission,
        "publish_session_list_changed",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="publish failed"):
        turn_admission.start_local_turn(
            session,
            _request(),
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
        )

    assert published == []
    assert session.title == "Untitled"


def test_local_turn_refuses_a_session_deleted_while_waiting_for_its_lock(monkeypatch):
    order = []
    session = _Session(order)

    @contextmanager
    def rejected_owner(_sid, *, session=None):
        raise turn_admission.SessionWriteRejected(_sid)
        yield session

    monkeypatch.setattr(
        turn_admission,
        "admission_write_owner",
        rejected_owner,
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


def test_local_turn_returns_not_found_when_deletion_wins_before_publication(monkeypatch):
    order = []
    session = _Session(order)
    owner_entries = 0
    original_owner = turn_admission.admission_write_owner

    @contextmanager
    def delete_before_publication(_sid, *, session=None):
        nonlocal owner_entries
        owner_entries += 1
        if owner_entries == 2:
            raise turn_admission.SessionWriteRejected(_sid)
        with original_owner(_sid, session=session) as current:
            yield current

    def append(session_id, event):
        order.append(event["event"])
        return _written_event(session_id, event)

    class DormantThread:
        def __init__(self, *args, **kwargs):
            order.append("thread_created")

        def start(self):
            order.append("worker")

    monkeypatch.setattr(turn_admission, "admission_write_owner", delete_before_publication)
    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append)
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(
        turn_admission,
        "register_runtime_stream",
        lambda *_args, **_kwargs: order.append("registered"),
    )
    monkeypatch.setattr(
        turn_admission,
        "finish_runtime_run",
        lambda _stream_id: order.append("runtime_cleaned"),
    )
    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=DormantThread),
    )

    result = turn_admission.start_local_turn(
        session,
        _request(),
        worker_target=lambda *_args, **_kwargs: order.append("worker_target"),
        clear_stale_stream=lambda _session: False,
    )

    assert result == {"error": "Session not found", "_status": 404}
    assert "worker" not in order
    assert order == [
        "persisted",
        "submitted",
        "thread_created",
        "registered",
        "runtime_cleaned",
    ]
