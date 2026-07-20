"""Stage-326 integration test for #1951's PENDING_GOAL_CONTINUATION chain.

Opus advisor flagged a critical race during stage-326 review: the original
#1951 PR placed a `PENDING_GOAL_CONTINUATION.discard(session_id)` in the
streaming worker's `finally` block. Because `goal_continue` sets the marker
inside the SAME function call (line ~3328) that the `finally` then discards
it (line ~3553), the marker would be erased before the frontend could
receive the SSE event, post the next /chat/start, and trigger the
consumer-side `if session_id in PENDING_GOAL_CONTINUATION` check in
routes.py.

The fix removes the discard from streaming.py's finally and relies on the
consumer in routes.py to discard atomically when the marker is read.

These tests exercise the full chain to guard against the regression:
1. The streaming finally must NOT discard the marker
2. Setting the marker survives the streaming finally
3. routes.py consumer discards atomically on read
"""
import re
from pathlib import Path


def _read_streaming():
    return Path(__file__).parents[1].joinpath(
        "api", "runs", "local.py"
    ).read_text(encoding="utf-8")


def _read_routes():
    return Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")


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
    import api.turn_admission as turn_admission

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


def test_goal_continue_set_marker_before_emitting_event():
    """Source-code ordering check: PENDING_GOAL_CONTINUATION.add must
    happen BEFORE the goal_continue SSE event is put on the queue, so the
    marker is observable by the time the frontend reacts."""
    src = _read_streaming()
    add_idx = src.find("PENDING_GOAL_CONTINUATION.add(session_id)")
    if add_idx == -1:
        # Tolerate slight phrasing variations.
        m = re.search(r"PENDING_GOAL_CONTINUATION\.add\([^)]*\)", src)
        assert m is not None, "PENDING_GOAL_CONTINUATION.add not found"
        add_idx = m.start()

    # Find the next goal_continue SSE event AFTER the add.
    after_add = src[add_idx:]
    event_idx = after_add.find("goal_continue")
    assert event_idx != -1, "no goal_continue emission after marker add"
    # Must be within ~500 chars (close to the add).
    assert event_idx < 500, (
        "PENDING_GOAL_CONTINUATION.add must immediately precede the "
        "goal_continue SSE emission"
    )
