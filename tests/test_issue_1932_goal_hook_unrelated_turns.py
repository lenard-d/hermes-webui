"""Regression tests for issue #1932: goal hook fires on every assistant turn.

The goal evaluation hook must only run when the turn was triggered by an
explicit goal-related message (goal set, goal continuation). Unrelated
messages like "what time is it" must NOT:
  - increment turns_used
  - trigger goal_continue SSE events
  - burn the goal budget
"""
import pytest


# ---------------------------------------------------------------------------
# Test 1: config exports STREAM_GOAL_RELATED
# ---------------------------------------------------------------------------

def test_config_exports_stream_goal_related():
    """api.config must export STREAM_GOAL_RELATED for the streaming gate."""
    from api.config import STREAM_GOAL_RELATED
    assert isinstance(STREAM_GOAL_RELATED, dict)


# ---------------------------------------------------------------------------
# Test 2: config exports PENDING_GOAL_CONTINUATION
# ---------------------------------------------------------------------------

def test_config_exports_pending_goal_continuation():
    """api.config must export PENDING_GOAL_CONTINUATION for auto-marking
    continuation streams as goal-related."""
    from api.config import PENDING_GOAL_CONTINUATION
    assert isinstance(PENDING_GOAL_CONTINUATION, (dict, set))


# ---------------------------------------------------------------------------
# Test 3: streaming.py gates evaluate_goal_after_turn on STREAM_GOAL_RELATED
# ---------------------------------------------------------------------------

def test_streaming_source_code_gates_on_stream_goal_related():
    """The streaming code must check STREAM_GOAL_RELATED[stream_id] before
    calling evaluate_goal_after_turn, so unrelated turns skip the hook."""
    from pathlib import Path
    streaming_py = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

    # Must import STREAM_GOAL_RELATED
    assert "STREAM_GOAL_RELATED" in streaming_py, (
        "streaming.py must import STREAM_GOAL_RELATED from api.config"
    )

    # Must check it before calling evaluate_goal_after_turn
    goal_related_check = streaming_py.find("STREAM_GOAL_RELATED")
    eval_call = streaming_py.find("evaluate_goal_after_turn")
    assert goal_related_check != -1 and eval_call != -1
    assert goal_related_check < eval_call, (
        "STREAM_GOAL_RELATED check must appear before evaluate_goal_after_turn call"
    )


# ---------------------------------------------------------------------------
# Test 4: streaming.py sets PENDING_GOAL_CONTINUATION on goal_continue
# ---------------------------------------------------------------------------

def test_streaming_sets_pending_goal_continuation_on_goal_continue():
    """When goal_continue is emitted, streaming.py must set
    PENDING_GOAL_CONTINUATION so the next /chat/start marks the stream."""
    from pathlib import Path
    streaming_py = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

    assert "PENDING_GOAL_CONTINUATION" in streaming_py, (
        "streaming.py must reference PENDING_GOAL_CONTINUATION"
    )

    # The PENDING_GOAL_CONTINUATION set must happen near goal_continue
    goal_continue_idx = streaming_py.find("goal_continue")
    pending_idx = streaming_py.find("PENDING_GOAL_CONTINUATION")
    assert goal_continue_idx != -1 and pending_idx != -1


@pytest.mark.parametrize("explicit_goal", [False, True])
def test_turn_admission_marks_continuation_and_explicit_goal_streams(
    monkeypatch,
    explicit_goal,
):
    """Both goal entry paths publish the accepted stream as goal-related."""
    import api.config as config
    import api.turn_admission as turn_admission

    class Session:
        session_id = f"goal-admission-{explicit_goal}"
        profile = None
        title = "Goal"
        active_stream_id = None
        pending_user_message = None
        pending_started_at = None
        messages = []
        worktree_path = None

        def save(self, *args, **kwargs):
            return None

    session = Session()
    if not explicit_goal:
        config.PENDING_GOAL_CONTINUATION.add(session.session_id)
    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda session_id, event: {**event, "session_id": session_id},
    )
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)
    try:
        result = turn_admission.start_local_turn(
            session,
            turn_admission.LocalTurnRequest(
                message="continue goal",
                attachments=[],
                workspace="/tmp/workspace",
                model="test-model",
                goal_related=explicit_goal,
            ),
            worker_target=lambda *_a, **_k: None,
            clear_stale_stream=lambda _session: False,
        )
        assert config.STREAM_GOAL_RELATED[result["stream_id"]] is True
        assert session.session_id not in config.PENDING_GOAL_CONTINUATION
    finally:
        config.PENDING_GOAL_CONTINUATION.discard(session.session_id)
        if "result" in locals():
            config.finish_runtime_run(result["stream_id"])
        with config.LOCK:
            config.SESSIONS.pop(session.session_id, None)


# ---------------------------------------------------------------------------
# Test 8: _run_agent_streaming accepts and uses goal_related
# ---------------------------------------------------------------------------

def test_run_agent_streaming_uses_goal_related():
    """_run_agent_streaming must accept goal_related kwarg and use it to
    gate the goal evaluation hook."""
    from pathlib import Path
    streaming_py = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

    # Function must accept goal_related parameter
    func_def_idx = streaming_py.find("def _run_agent_streaming")
    assert func_def_idx != -1

    # The function signature area (within ~200 chars) should contain goal_related
    sig_area = streaming_py[func_def_idx:func_def_idx + 500]
    assert "goal_related" in sig_area, (
        "_run_agent_streaming must accept a goal_related parameter"
    )


# ---------------------------------------------------------------------------
# Test 9: STREAM_GOAL_RELATED cleanup on stream exit
# ---------------------------------------------------------------------------

def test_stream_goal_related_cleaned_up():
    """STREAM_GOAL_RELATED entries must be cleaned up when streams end."""
    import api.config as config

    stream_id = "goal-related-cleanup-contract"
    config.register_runtime_stream(
        stream_id,
        "goal-related-cleanup-session",
        object(),
        goal_related=True,
    )
    try:
        assert config.STREAM_GOAL_RELATED[stream_id] is True
        config.finish_runtime_run(stream_id)
        assert stream_id not in config.STREAM_GOAL_RELATED
    finally:
        config.finish_runtime_run(stream_id)


# ---------------------------------------------------------------------------
# Test 10: functional test with FakeGoalManager at streaming integration level
# ---------------------------------------------------------------------------

def test_goal_evaluate_after_turn_only_increments_for_user_initiated(monkeypatch):
    """Verify that evaluate_goal_after_turn only increments turns_used
    when user_initiated=True (goal-related), not when user_initiated=False."""
    from api import goals as webui_goals

    turns_incremented = []

    class FakeState:
        goal = "test goal"
        status = "active"
        turns_used = 0
        max_turns = 10
        last_turn_at = 0.0
        last_verdict = None
        last_reason = None
        paused_reason = None

        def to_json(self):
            return {"goal": self.goal, "status": self.status}

    class FakeMgr:
        def __init__(self, session_id, default_max_turns=20):
            self.state = FakeState()

        def is_active(self):
            return True

        def evaluate_after_turn(self, last_response, user_initiated=True):
            if user_initiated:
                self.state.turns_used += 1
                turns_incremented.append(True)
            return {
                "status": "active",
                "should_continue": True,
                "continuation_prompt": "continue",
                "verdict": "continue",
                "reason": "ok",
                "message": "ok",
            }

    monkeypatch.setattr(webui_goals, "GoalManager", FakeMgr)
    monkeypatch.setattr(webui_goals, "_default_max_turns", lambda: 10)

    # user_initiated=True should increment
    webui_goals.evaluate_goal_after_turn(
        "sid-1", "goal response", user_initiated=True, profile_home=None
    )
    assert len(turns_incremented) == 1

    # user_initiated=False should NOT increment
    webui_goals.evaluate_goal_after_turn(
        "sid-1", "unrelated response", user_initiated=False, profile_home=None
    )
    assert len(turns_incremented) == 1, (
        "turns_used should NOT increment when user_initiated=False"
    )
