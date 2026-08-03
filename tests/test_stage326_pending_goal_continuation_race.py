"""Stage-326 integration test for #1951's PENDING_GOAL_CONTINUATION chain.

Opus advisor flagged a critical race during stage-326 review: the original
#1951 PR placed a `PENDING_GOAL_CONTINUATION.discard(session_id)` in the local
run owner's `finally` block. Because `goal_continue` sets the marker inside
the SAME lifecycle that the `finally` then discarded it, the marker would be
erased before the frontend could receive the SSE event, post the next
/chat/start, and trigger the consumer-side continuation check in turn
admission.

The fix leaves the marker owned by local-success publication and relies on
turn admission to consume it atomically after admission succeeds.

These tests exercise the full chain to guard against the regression:
1. The streaming finally must NOT discard the marker
2. Setting the marker survives the streaming finally
3. routes.py consumer discards atomically on read
"""
import logging
from types import SimpleNamespace


def test_streaming_finally_does_not_discard_pending_goal_continuation():
    """Run teardown must preserve the single-use session continuation marker."""
    import api.config as config

    stream_id = "goal-stream-teardown"
    session_id = "goal-session-teardown"
    config.PENDING_GOAL_CONTINUATION.add(session_id)
    config.register_runtime_stream(stream_id, session_id, object(), goal_related=True)
    try:
        config.finish_runtime_run(stream_id)
        assert session_id in config.PENDING_GOAL_CONTINUATION
    finally:
        config.PENDING_GOAL_CONTINUATION.discard(session_id)
        config.finish_runtime_run(stream_id)


def test_turn_admission_consumes_goal_marker_only_after_it_claims_the_session(monkeypatch):
    """A rejected duplicate start must not steal the next goal continuation."""
    import api.config as config
    from api.runs import admission as turn_admission

    class Session:
        session_id = "goal-continuation-admission"
        profile = None
        title = "Goal"
        messages = []
        worktree_path = None

        def __init__(self):
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_started_at = None

        def save(self, *args, **kwargs):
            return None

    session = Session()
    request = turn_admission.LocalTurnRequest(
        message="continue",
        attachments=[],
        workspace="/tmp/workspace",
        model="test-model",
    )
    config.PENDING_GOAL_CONTINUATION.add(session.session_id)
    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda session_id, event: {**event, "session_id": session_id},
    )
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)
    waiting_for_lock = __import__("threading").Event()
    result = {}

    class Diag:
        def stage(self, name):
            if name == "session_lock_wait":
                waiting_for_lock.set()

    def competing_start():
        result["rejected"] = turn_admission.start_local_turn(
            session,
            request,
            worker_target=lambda *_a, **_k: None,
            clear_stale_stream=lambda _session: False,
            diag=Diag(),
        )

    try:
        lock = config._get_session_agent_lock(session.session_id)
        with lock:
            thread = __import__("threading").Thread(target=competing_start)
            thread.start()
            assert waiting_for_lock.wait(2)
            session.active_stream_id = "existing"
            session.pending_user_message = "running"
            session.pending_started_at = 1.0
            config.register_runtime_stream("existing", session.session_id, object())
        thread.join(2)
        assert not thread.is_alive()
        rejected = result["rejected"]
        assert rejected["_status"] == 409
        assert session.session_id in config.PENDING_GOAL_CONTINUATION

        config.finish_runtime_run("existing")
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_started_at = None
        accepted = turn_admission.start_local_turn(
            session,
            request,
            worker_target=lambda *_a, **_k: None,
            clear_stale_stream=lambda _session: False,
        )
        assert session.session_id not in config.PENDING_GOAL_CONTINUATION
        assert config.STREAM_GOAL_RELATED[accepted["stream_id"]] is True
    finally:
        config.PENDING_GOAL_CONTINUATION.discard(session.session_id)
        config.finish_runtime_run("existing")
        if "accepted" in locals():
            config.finish_runtime_run(accepted["stream_id"])
        with config.LOCK:
            config.SESSIONS.pop(session.session_id, None)


def test_pending_goal_continuation_is_a_set():
    """The marker store must be a set so add/discard is GIL-safe single-op
    (mutated from streaming worker thread, read from HTTP threads)."""
    from api.config import PENDING_GOAL_CONTINUATION
    assert isinstance(PENDING_GOAL_CONTINUATION, set), (
        "PENDING_GOAL_CONTINUATION must be a set for thread-safe single-op "
        "add/discard semantics"
    )


def test_stream_goal_related_pop_keyed_by_stream_id():
    """Run teardown removes only the ending stream's goal-related state."""
    import api.config as config

    config.register_runtime_stream("ending-stream", "shared-session", object(), goal_related=True)
    config.register_runtime_stream("other-stream", "shared-session", object(), goal_related=True)
    try:
        config.finish_runtime_run("ending-stream")
        assert "ending-stream" not in config.STREAM_GOAL_RELATED
        assert config.STREAM_GOAL_RELATED["other-stream"] is True
    finally:
        config.finish_runtime_run("ending-stream")
        config.finish_runtime_run("other-stream")


def test_goal_continue_sets_marker_before_publishing_event(monkeypatch):
    """The marker must already be observable when goal_continue is published."""
    from api import goals
    from api.runs import local_success

    session_id = "goal-publication-order"
    marker_was_visible = []
    published = []

    monkeypatch.setattr(goals, "has_active_goal", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        goals,
        "evaluate_goal_after_turn",
        lambda *_args, **_kwargs: {
            "should_continue": True,
            "continuation_prompt": "continue the goal",
        },
    )

    def publish(event, payload):
        published.append((event, payload))
        if event == "goal_continue":
            marker_was_visible.append(
                session_id in local_success.PENDING_GOAL_CONTINUATION
            )

    try:
        local_success._publish_goal_continuation(
            SimpleNamespace(messages=[{"role": "assistant", "content": "working"}]),
            session_id=session_id,
            profile_home="/tmp/profile",
            goal_related=True,
            publish=publish,
            logger=logging.getLogger(__name__),
        )

        assert [event for event, _payload in published] == ["goal", "goal_continue"]
        assert marker_was_visible == [True]
        assert session_id in local_success.PENDING_GOAL_CONTINUATION
    finally:
        local_success.PENDING_GOAL_CONTINUATION.discard(session_id)
