"""Behavioral contract for process-local browser-turn runtime ownership."""

from __future__ import annotations

import threading

import pytest

import api.config as config
from api.runtime_state import ProcessRuntimeState


_STREAM_MAP_NAMES = (
    "STREAMS",
    "STREAM_SESSION_OWNERS",
    "CANCEL_FLAGS",
    "AGENT_INSTANCES",
    "STREAM_PARTIAL_TEXT",
    "STREAM_REASONING_TEXT",
    "STREAM_LIVE_TOOL_CALLS",
    "STREAM_GOAL_RELATED",
    "STREAM_LAST_EVENT_ID",
    "ACTIVE_RUNS",
)


def _runtime_state(*, clock):
    stores = {
        "streams": {},
        "stream_owners": {},
        "cancel_flags": {},
        "agent_instances": {},
        "partial_text": {},
        "reasoning_text": {},
        "live_tool_calls": {},
        "goal_related": {},
        "last_event_ids": {},
        "active_runs": {},
    }
    state = ProcessRuntimeState(
        **stores,
        streams_lock=threading.Lock(),
        owners_lock=threading.Lock(),
        active_runs_lock=threading.Lock(),
        clock=clock,
    )
    return state, stores


@pytest.fixture(autouse=True)
def isolated_runtime_state():
    snapshots = {name: dict(getattr(config, name)) for name in _STREAM_MAP_NAMES}
    previous_finished_at = config.LAST_RUN_FINISHED_AT
    for name in _STREAM_MAP_NAMES:
        getattr(config, name).clear()
    config.LAST_RUN_FINISHED_AT = None
    try:
        yield
    finally:
        for name, snapshot in snapshots.items():
            target = getattr(config, name)
            target.clear()
            target.update(snapshot)
        config.LAST_RUN_FINISHED_AT = previous_finished_at


def test_register_runtime_stream_publishes_channel_owner_and_goal_state():
    channel = object()

    config.register_runtime_stream(
        "stream-1",
        "session-1",
        channel,
        goal_related=True,
    )

    assert config.STREAMS["stream-1"] is channel
    assert config.stream_owner_session_id("stream-1") == "session-1"
    assert config.STREAM_GOAL_RELATED["stream-1"] is True


def test_blocking_stream_for_session_covers_transport_worker_and_registration_gap():
    clock = [100.0]
    state, stores = _runtime_state(clock=lambda: clock[0])

    state.register_stream("transport", "session-1", object())
    assert (
        state.blocking_stream_for_session(
            "session-1",
            active_stream_id="transport",
        )
        == "transport"
    )

    state.finish_run("transport")
    state.register_worker("worker", session_id="session-1")
    assert state.blocking_stream_for_session("session-1") == "worker"

    state.unregister_worker("worker")
    assert (
        state.blocking_stream_for_session(
            "session-1",
            active_stream_id="publishing",
            pending_user_message="hello",
            pending_started_at=90.0,
            pending_grace_seconds=30.0,
        )
        == "publishing"
    )


def test_blocking_stream_for_session_releases_dead_stale_worker_and_owner():
    clock = [1_000.0]
    state, stores = _runtime_state(clock=lambda: clock[0])
    state.register_owner("stale", "session-1")
    state.register_worker("stale", session_id="session-1", started_at=100.0)

    assert (
        state.blocking_stream_for_session(
            "session-1",
            worker_unwind_seconds=180.0,
        )
        is None
    )
    assert "stale" not in stores["active_runs"]
    assert "stale" not in stores["stream_owners"]


def test_blocking_stream_for_session_does_not_reap_stale_worker_with_live_transport():
    clock = [1_000.0]
    state, stores = _runtime_state(clock=lambda: clock[0])
    state.register_stream("live", "session-1", object())
    state.register_worker("live", session_id="session-1", started_at=100.0)

    assert (
        state.blocking_stream_for_session(
            "session-1",
            worker_unwind_seconds=180.0,
        )
        is None
    )
    assert "live" in stores["active_runs"]


def test_finish_runtime_run_releases_every_owned_per_run_value():
    channel = object()
    cancel_event = threading.Event()
    config.register_runtime_stream("stream-1", "session-1", channel)
    config.CANCEL_FLAGS["stream-1"] = cancel_event
    config.AGENT_INSTANCES["stream-1"] = object()
    config.STREAM_PARTIAL_TEXT["stream-1"] = "partial"
    config.STREAM_REASONING_TEXT["stream-1"] = "reasoning"
    config.STREAM_LIVE_TOOL_CALLS["stream-1"] = [{"id": "tool-1"}]
    config.STREAM_LAST_EVENT_ID["stream-1"] = "stream-1:7"
    config.register_active_run("stream-1", session_id="session-1", phase="running")

    removed = config.finish_runtime_run("stream-1")

    assert removed is True
    for name in _STREAM_MAP_NAMES:
        assert "stream-1" not in getattr(config, name)
    assert isinstance(config.LAST_RUN_FINISHED_AT, float)


def test_finish_runtime_run_is_idempotent():
    assert config.finish_runtime_run("missing-stream") is False
    assert config.finish_runtime_run("missing-stream") is False
