"""Regression tests for config-driven first-turn session persistence (#1406)."""
import json
from types import SimpleNamespace

import pytest

import api.config as config
import api.sessions.store as models
import api.sessions.pending_recovery as session_pending_recovery
import api.sessions.records as session_records
import api.sessions.recovery as session_recovery
from api.runs import compression_anchors, transcript, turn_context
from api.runs import admission as turn_admission
import api.turn_journal as turn_journal
from api.sessions.store import Session, new_session


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(session_records, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_records, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(session_pending_recovery, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_pending_recovery, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()
    monkeypatch.setattr(config, "cfg", {})
    monkeypatch.setattr(config, "_cfg_cache", {})
    yield session_dir
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()


def test_session_save_mode_defaults_to_deferred_for_missing_config():
    assert config.get_webui_session_save_mode({}) == "deferred"
    assert config.get_webui_session_save_mode({"webui": {}}) == "deferred"


@pytest.mark.parametrize("raw", ["bogus", "", None, 42, {"mode": "eager"}])
def test_invalid_session_save_mode_falls_back_to_deferred(raw):
    assert config.get_webui_session_save_mode({"webui": {"session_save_mode": raw}}) == "deferred"


def test_eager_session_save_mode_is_accepted():
    assert config.get_webui_session_save_mode({"webui": {"session_save_mode": "eager"}}) == "eager"


def test_eager_mode_still_does_not_save_empty_new_sessions(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    s = new_session()
    assert not s.path.exists(), "eager mode must not recreate empty Untitled session files"


def test_deferred_chat_start_persists_pending_only_before_thread(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "deferred"}})
    s = new_session(workspace=str(_isolate_state.parent))
    turn_admission.prepare_session_for_turn(
        s,
        message="hello deferred",
        attachments=[],
        workspace=str(_isolate_state.parent),
        model=s.model,
        model_provider=s.model_provider,
        stream_id="stream_deferred",
        started_at=123.0,
    )
    on_disk = json.loads(s.path.read_text(encoding="utf-8"))
    assert on_disk["messages"] == []
    assert on_disk["pending_user_message"] == "hello deferred"


def test_eager_chat_start_checkpoints_first_user_message_before_thread(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    s = new_session(workspace=str(_isolate_state.parent))
    turn_admission.prepare_session_for_turn(
        s,
        message="hello eager",
        attachments=[{"name": "note.txt", "path": "", "mime": "text/plain"}],
        workspace=str(_isolate_state.parent),
        model=s.model,
        model_provider=s.model_provider,
        stream_id="stream_eager",
        started_at=456.0,
    )
    on_disk = json.loads(s.path.read_text(encoding="utf-8"))
    assert [m["role"] for m in on_disk["messages"]] == ["user"]
    assert on_disk["messages"][0]["content"] == "hello eager"
    assert on_disk["messages"][0]["attachments"][0]["name"] == "note.txt"
    assert on_disk["pending_user_message"] == "hello eager"


def test_post_commit_submitted_error_keeps_the_confirmed_turn(_isolate_state, monkeypatch):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    session = Session(
        session_id="post_commit_submitted_error",
        title="Existing",
        messages=[{"role": "user", "content": "accepted"}],
    )
    session.save()
    committed = []
    thread_starts = []
    real_append = turn_journal.append_turn_journal_event

    def append_then_raise(session_id, event):
        written = real_append(session_id, event)
        committed.append(written)
        raise OSError("simulated close error after fsync")

    class StartedThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            thread_starts.append(True)

    monkeypatch.setattr(turn_admission, "append_turn_journal_event", append_then_raise)
    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=StartedThread),
    )
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)

    response = turn_admission.start_local_turn(
        session,
        turn_admission.LocalTurnRequest(
            message="durably submitted",
            attachments=[],
            workspace="/workspace",
            model="model",
        ),
        worker_target=lambda *_args, **_kwargs: None,
        clear_stale_stream=lambda _session: False,
    )

    try:
        assert thread_starts == [True]
        assert response["turn_id"] == committed[0]["turn_id"]
        assert response["stream_id"] == committed[0]["stream_id"]
        authoritative = models.get_session(session.session_id)
        assert authoritative.active_stream_id == response["stream_id"]
        assert authoritative.pending_user_message == "durably submitted"
    finally:
        config.finish_runtime_run(response["stream_id"])


def test_worker_starts_after_admission_releases_the_session_owner(_isolate_state, monkeypatch):
    session = Session(
        session_id="worker_starts_after_owner_release",
        title="Existing",
        messages=[{"role": "user", "content": "accepted"}],
    )
    session.save()

    class ReentryDetectingLock:
        def __init__(self):
            self.held = False

        def __enter__(self):
            if self.held:
                raise RuntimeError("session owner was still held")
            self.held = True
            return self

        def __exit__(self, exc_type, exc, tb):
            self.held = False
            return False

    owner_lock = ReentryDetectingLock()
    worker_acquired_owner = []

    def worker(*_args, **_kwargs):
        with config._get_session_agent_lock(session.session_id):
            worker_acquired_owner.append(True)

    class InlineThread:
        def __init__(self, *, target, args, kwargs, daemon):
            self.target = target
            self.args = args
            self.kwargs = kwargs

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(config, "_get_session_agent_lock", lambda _sid: owner_lock)
    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=InlineThread),
    )
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)

    response = turn_admission.start_local_turn(
        session,
        turn_admission.LocalTurnRequest(
            message="start without owner lock",
            attachments=[],
            workspace="/workspace",
            model="model",
        ),
        worker_target=worker,
        clear_stale_stream=lambda _session: False,
    )

    try:
        assert worker_acquired_owner == [True]
    finally:
        config.finish_runtime_run(response["stream_id"])


@pytest.mark.parametrize("failure_stage", ["pending_index", "journal"])
@pytest.mark.parametrize("preexisting_backup", [False, True])
def test_failed_eager_admission_cannot_be_restored_from_shrink_backup(
    _isolate_state,
    monkeypatch,
    failure_stage,
    preexisting_backup,
):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    session = Session(
        session_id=f"eager_rollback_{failure_stage}_{int(preexisting_backup)}",
        title="Existing title",
        workspace="/previous/workspace",
        model="previous-model",
        model_provider="previous-provider",
        messages=[
            {"role": "user", "content": "accepted prompt"},
            {"role": "assistant", "content": "accepted answer"},
        ],
    )
    session.save()
    baseline = json.loads(session.path.read_text(encoding="utf-8"))
    backup_path = session.path.with_suffix(".json.bak")
    expected_backup = None
    if preexisting_backup:
        expected_backup = session.path.read_bytes()
        backup_path.write_bytes(expected_backup)

    if failure_stage == "pending_index":
        real_write_index = session_records._write_session_index
        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("index unavailable")
            return real_write_index(*args, **kwargs)

        monkeypatch.setattr(session_records, "_write_session_index", fail_once)
        monkeypatch.setattr(
            turn_admission,
            "append_turn_journal_event",
            lambda *_args, **_kwargs: pytest.fail("journal must not be reached"),
        )
        expected_error = "index unavailable"
    else:
        monkeypatch.setattr(
            turn_admission,
            "append_turn_journal_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("journal unavailable")
            ),
        )
        expected_error = "journal unavailable"

    request = turn_admission.LocalTurnRequest(
        message="REJECTED MESSAGE",
        attachments=[],
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
    )
    with pytest.raises(OSError, match=expected_error):
        turn_admission.start_local_turn(
            session,
            request,
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
        )

    live = json.loads(session.path.read_text(encoding="utf-8"))
    assert live["messages"] == baseline["messages"]
    assert "REJECTED MESSAGE" not in session.path.read_text(encoding="utf-8")
    assert live["active_stream_id"] is None
    assert live["pending_user_message"] is None
    assert live["workspace"] == baseline["workspace"]
    assert live["model"] == baseline["model"]
    assert live["model_provider"] == baseline["model_provider"]
    if expected_backup is None:
        assert not backup_path.exists()
    else:
        assert backup_path.read_bytes() == expected_backup

    index = json.loads(session_records.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    indexed = next(row for row in index if row["session_id"] == session.session_id)
    assert indexed["message_count"] == 2
    assert indexed["active_stream_id"] is None

    models.SESSIONS.clear()
    status = session_recovery.inspect_session_recovery_status(session.path)
    assert status["recommend"] in {"no_backup", "no_action"}
    recovery = session_recovery.recover_all_sessions_on_startup(_isolate_state)
    assert recovery["restored"] == 0
    reloaded = Session.load(session.session_id)
    assert reloaded is not None
    assert reloaded.messages == baseline["messages"]
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None


def test_eager_admission_uses_authoritative_session_not_stale_caller(
    _isolate_state,
    monkeypatch,
):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    authoritative = Session(
        session_id="authoritative_admission",
        title="Authoritative",
        messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ],
    )
    authoritative.save()
    models.cache_full_session(authoritative.session_id, authoritative)
    stale = Session(
        session_id=authoritative.session_id,
        title="Stale",
        messages=authoritative.messages[:2],
    )

    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda session_id, event: {**event, "session_id": session_id},
    )
    monkeypatch.setattr(turn_admission, "create_stream_channel", object)
    monkeypatch.setattr(turn_admission, "register_runtime_stream", lambda *_a, **_k: None)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)

    class StartedThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=StartedThread),
    )

    result = turn_admission.start_local_turn(
        stale,
        turn_admission.LocalTurnRequest(
            message="q3",
            attachments=[],
            workspace="/workspace",
            model="model",
        ),
        worker_target=lambda *_args, **_kwargs: None,
        clear_stale_stream=lambda _session: False,
    )

    live = json.loads(authoritative.path.read_text(encoding="utf-8"))
    assert [message["content"] for message in live["messages"]] == [
        "q1",
        "a1",
        "q2",
        "a2",
        "q3",
    ]
    assert live["active_stream_id"] == result["stream_id"]
    assert not authoritative.path.with_suffix(".json.bak").exists()


def test_admission_compensation_restores_empty_orphan_pending_baseline(
    _isolate_state,
    monkeypatch,
):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    session = Session(
        session_id="orphan_pending_admission",
        title="Existing",
        messages=[],
        pending_user_message="older orphan intent",
    )
    session.save()
    models.cache_full_session(session.session_id, session)
    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )

    with pytest.raises(OSError, match="journal unavailable"):
        turn_admission.start_local_turn(
            session,
            turn_admission.LocalTurnRequest(
                message="REJECTED",
                attachments=[],
                workspace="/workspace",
                model="model",
            ),
            worker_target=lambda *_args, **_kwargs: None,
            clear_stale_stream=lambda _session: False,
        )

    live = json.loads(session.path.read_text(encoding="utf-8"))
    assert live["messages"] == []
    assert live["pending_user_message"] == "older orphan intent"
    assert live["active_stream_id"] is None
    assert "REJECTED" not in session.path.read_text(encoding="utf-8")
    assert not session.path.with_suffix(".json.bak").exists()


def test_failed_first_turn_does_not_create_an_empty_session_sidecar(
    _isolate_state,
    monkeypatch,
):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    session = new_session(workspace="/workspace")
    assert not session.path.exists()
    goal_markers = {session.session_id}
    background_markers = {session.session_id}
    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )
    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )

    with pytest.raises(OSError, match="journal unavailable"):
        turn_admission.start_local_turn(
            session,
            turn_admission.LocalTurnRequest(
                message="REJECTED FIRST TURN",
                attachments=[],
                workspace="/workspace",
                model="model",
            ),
            worker_target=lambda *_args, **_kwargs: pytest.fail("worker started"),
            clear_stale_stream=lambda _session: False,
        )

    assert not session.path.exists()
    assert not session.path.with_suffix(".json.bak").exists()
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.messages == []
    assert goal_markers == {session.session_id}
    assert background_markers == {session.session_id}
    if session_records.SESSION_INDEX_FILE.exists():
        index = json.loads(session_records.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        assert all(row["session_id"] != session.session_id for row in index)


def test_failed_compensation_keeps_pending_owner_and_markers_consumed(
    _isolate_state,
    monkeypatch,
):
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    session = Session(
        session_id="failed_admission_compensation",
        title="Existing",
        messages=[{"role": "user", "content": "accepted"}],
    )
    session.save()
    models.cache_full_session(session.session_id, session)
    goal_markers = {session.session_id}
    background_markers = {session.session_id}
    monkeypatch.setattr(turn_admission, "PENDING_GOAL_CONTINUATION", goal_markers)
    monkeypatch.setattr(
        turn_admission,
        "PENDING_BG_TASK_COMPLETIONS",
        background_markers,
    )
    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )
    real_replace = session_records._safe_replace
    sidecar_writes = 0

    def fail_compensation_replace(src, dst):
        nonlocal sidecar_writes
        if dst == session.path:
            sidecar_writes += 1
            if sidecar_writes == 2:
                raise OSError("compensation replace unavailable")
        return real_replace(src, dst)

    monkeypatch.setattr(session_records, "_safe_replace", fail_compensation_replace)

    with pytest.raises(OSError, match="journal unavailable"):
        turn_admission.start_local_turn(
            session,
            turn_admission.LocalTurnRequest(
                message="REJECTED",
                attachments=[],
                workspace="/workspace",
                model="model",
            ),
            worker_target=lambda *_args, **_kwargs: None,
            clear_stale_stream=lambda _session: False,
        )

    live = json.loads(session.path.read_text(encoding="utf-8"))
    assert live["active_stream_id"] == session.active_stream_id
    assert live["active_stream_id"] is not None
    assert live["pending_user_message"] == session.pending_user_message == "REJECTED"
    assert [message["content"] for message in live["messages"]] == [
        "accepted",
        "REJECTED",
    ]
    assert goal_markers == set()
    assert background_markers == set()


def test_eager_wal_repair_does_not_duplicate_checkpointed_user_message(_isolate_state, monkeypatch):
    s = Session(session_id="eager_repair", messages=[{"role": "user", "content": "survive"}])
    s.pending_user_message = "survive"
    s.active_stream_id = "dead_stream"
    s.pending_started_at = 789.0
    s.save()

    repaired = models._repair_stale_pending(s)

    assert repaired is True
    user_messages = [m for m in s.messages if m.get("role") == "user" and m.get("content") == "survive"]
    assert len(user_messages) == 1
    assert s.pending_user_message is None
    assert any(m.get("_error") for m in s.messages if m.get("role") == "assistant")


def test_eager_checkpointed_user_is_removed_from_model_context():
    context = compression_anchors._drop_checkpointed_current_user_from_context(
        [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "current"},
        ],
        "current",
    )
    assert [m["content"] for m in context] == ["older", "prior"]


def test_active_pending_current_user_is_removed_from_model_context():
    """Current pending user text must not appear in conversation_history twice.

    The provider receives the active turn as `user_message`; if an eager
    checkpoint/recovery path has already put the same current user text at the
    end of `context_messages`, `_context_messages_for_new_turn` must strip it
    before building the model-facing history.  Turn-journal `submitted` records
    are intentionally not a context source for this helper.
    """
    session = Session(
        session_id="journal_context_current_turn",
        title="journal context",
        messages=[
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "prior"},
        ],
        context_messages=[
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "[Workspace::v1: /tmp/hermes]\ncurrent prompt"},
        ],
        active_stream_id="stream-current",
        pending_user_message="current prompt",
    )

    context = turn_context._context_messages_for_new_turn(session, "current prompt")

    assert [m["content"] for m in context] == ["older", "prior"]


def test_eager_checkpointed_user_is_not_duplicated_after_agent_result():
    merged = transcript._merge_display_messages_after_agent_result(
        previous_display=[{"role": "user", "content": "repeat me"}],
        previous_context=[],
        result_messages=[
            {"role": "user", "content": "repeat me"},
            {"role": "assistant", "content": "ok"},
        ],
        msg_text="repeat me",
    )
    assert [m["role"] for m in merged] == ["user", "assistant"]


def test_deferred_turn_is_materialized_when_agent_returns_assistant_only_delta():
    merged = transcript._merge_display_messages_after_agent_result(
        previous_display=[
            {"role": "user", "content": "older prompt"},
            {"role": "assistant", "content": "older answer"},
        ],
        previous_context=[
            {"role": "user", "content": "older prompt"},
            {"role": "assistant", "content": "older answer"},
        ],
        result_messages=[
            {"role": "user", "content": "older prompt"},
            {"role": "assistant", "content": "older answer"},
            {"role": "assistant", "content": "current answer"},
        ],
        msg_text="latest prompt",
    )

    assert [m["role"] for m in merged] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [m["content"] for m in merged[-2:]] == ["latest prompt", "current answer"]


def test_duplicate_assistant_delta_is_not_persisted_twice():
    """Provider/result merge replay must not duplicate the same assistant bubble."""
    previous_display = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
    ]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "user", "content": "latest prompt"},
        {"role": "assistant", "content": "current answer"},
        {"role": "assistant", "content": "current answer"},
    ]

    merged = transcript._merge_display_messages_after_agent_result(
        previous_display=previous_display,
        previous_context=previous_context,
        result_messages=result_messages,
        msg_text="latest prompt",
    )

    assert [m["role"] for m in merged] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [m["content"] for m in merged[-2:]] == ["latest prompt", "current answer"]
    assert (
        sum(
            1
            for m in merged
            if m.get("role") == "assistant" and m.get("content") == "current answer"
        )
        == 1
    )


def test_same_assistant_text_across_different_turns_is_preserved():
    previous_display = [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "same answer"},
    ]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "user", "content": "second prompt"},
        {"role": "assistant", "content": "same answer"},
    ]

    merged = transcript._merge_display_messages_after_agent_result(
        previous_display=previous_display,
        previous_context=previous_context,
        result_messages=result_messages,
        msg_text="second prompt",
    )

    assert [m["content"] for m in merged] == [
        "first prompt",
        "same answer",
        "second prompt",
        "same answer",
    ]


def test_llm_title_generated_survives_save_and_load(_isolate_state):
    s = Session(
        session_id="generated_title",
        title="Useful generated title",
        messages=[{"role": "user", "content": "first prompt"}],
        llm_title_generated=True,
    )
    s.save()

    loaded = Session.load("generated_title")

    assert loaded.llm_title_generated is True
    on_disk = json.loads(s.path.read_text(encoding="utf-8"))
    assert on_disk["llm_title_generated"] is True


def test_session_constructor_preserves_loaded_llm_title_generated_kwarg():
    s = Session(session_id="loaded_generated_title", llm_title_generated=True)

    assert s.llm_title_generated is True
