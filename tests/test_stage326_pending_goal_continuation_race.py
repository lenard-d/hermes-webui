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
    return Path(__file__).parents[1].joinpath("api", "streaming.py").read_text(encoding="utf-8")


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


def test_routes_consumer_discards_atomically_on_read():
    """The routes.py consumer must discard the marker after consuming it,
    so the marker is single-use (one continuation = one auto-flag).
    """
    src = _read_routes()

    # Find the consumption check.
    m = re.search(
        r"if not goal_related and s\.session_id in PENDING_GOAL_CONTINUATION:.*?PENDING_GOAL_CONTINUATION\.discard",
        src,
        re.DOTALL,
    )
    assert m is not None, (
        "routes.py must consume PENDING_GOAL_CONTINUATION atomically: "
        "check + set goal_related + discard in the same block"
    )
    # The discard must be within ~10 lines of the check (atomic block).
    block = m.group(0)
    line_count = block.count("\n")
    assert line_count <= 10, (
        f"PENDING_GOAL_CONTINUATION check + discard span {line_count} lines; "
        "should be tight atomic block"
    )


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
