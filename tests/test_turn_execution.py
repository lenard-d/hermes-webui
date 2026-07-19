"""Behavioral contract for common local/Gateway worker ownership."""

from __future__ import annotations

import threading

import pytest

from api import turn_execution


class _Sink:
    def publish(self, _event, _data):
        return None


_DEFAULT_TRANSPORT = object()


def _wire_runtime(
    monkeypatch,
    calls,
    *,
    transport=_DEFAULT_TRANSPORT,
    initialize=True,
):
    monkeypatch.setattr(
        turn_execution,
        "runtime_transport",
        lambda stream_id: calls.append(("transport", stream_id)) or transport,
    )
    monkeypatch.setattr(
        turn_execution,
        "register_active_run",
        lambda stream_id, **metadata: calls.append(("register", stream_id, metadata)),
    )
    monkeypatch.setattr(
        turn_execution,
        "initialize_runtime_execution",
        lambda stream_id, cancel_event: calls.append(
            ("initialize", stream_id, cancel_event)
        ) or initialize,
    )
    monkeypatch.setattr(
        turn_execution,
        "finish_runtime_run",
        lambda stream_id: calls.append(("finish", stream_id)) or True,
    )
    monkeypatch.setattr(turn_execution.time, "time", lambda: 123.0)


def test_start_claims_transport_worker_buffers_journal_and_sink(monkeypatch):
    calls = []
    transport = object()
    journal = object()
    sink = _Sink()
    _wire_runtime(monkeypatch, calls, transport=transport)
    monkeypatch.setattr(
        turn_execution,
        "RunJournalWriter",
        lambda session_id, stream_id: calls.append(
            ("journal", session_id, stream_id)
        ) or journal,
    )
    monkeypatch.setattr(
        turn_execution,
        "RunEventSink",
        lambda **kwargs: calls.append(("sink", kwargs)) or sink,
    )

    execution = turn_execution.TurnExecution.start(
        stream_id="run-1",
        session_id="session-1",
        phase="starting",
        logger=turn_execution.logger,
        log_label="local run",
        workspace="/tmp/workspace",
        model="test-model",
    )

    assert execution is not None
    assert execution.transport is transport
    assert execution.journal is journal
    assert isinstance(execution.cancel_event, threading.Event)
    assert execution.event_sink is sink
    assert calls[0] == ("transport", "run-1")
    assert calls[1] == (
        "register",
        "run-1",
        {
            "session_id": "session-1",
            "started_at": 123.0,
            "phase": "starting",
            "workspace": "/tmp/workspace",
            "model": "test-model",
        },
    )
    assert calls[2] == ("journal", "session-1", "run-1")
    assert calls[3][0] == "initialize"
    assert calls[4][0] == "sink"


def test_start_releases_owner_when_transport_or_initialization_is_gone(monkeypatch):
    missing_calls = []
    _wire_runtime(monkeypatch, missing_calls, transport=None)

    assert turn_execution.TurnExecution.start(
        stream_id="missing",
        session_id="session-1",
        phase="starting",
    ) is None
    assert missing_calls == [("transport", "missing"), ("finish", "missing")]

    raced_calls = []
    _wire_runtime(monkeypatch, raced_calls, initialize=False)
    monkeypatch.setattr(turn_execution, "RunJournalWriter", lambda *_args: object())

    assert turn_execution.TurnExecution.start(
        stream_id="raced",
        session_id="session-1",
        phase="starting",
    ) is None
    assert raced_calls[-1] == ("finish", "raced")


def test_start_continues_without_optional_journal(monkeypatch):
    calls = []
    _wire_runtime(monkeypatch, calls)
    monkeypatch.setattr(
        turn_execution,
        "RunJournalWriter",
        lambda *_args: (_ for _ in ()).throw(OSError("journal unavailable")),
    )
    captured = {}

    def make_sink(**kwargs):
        captured.update(kwargs)
        return _Sink()

    monkeypatch.setattr(turn_execution, "RunEventSink", make_sink)

    execution = turn_execution.TurnExecution.start(
        stream_id="run-1",
        session_id="session-1",
        phase="starting",
    )

    assert execution is not None
    assert execution.journal is None
    assert captured["journal"] is None


def test_start_compensates_unexpected_setup_failure(monkeypatch):
    calls = []
    _wire_runtime(monkeypatch, calls)
    monkeypatch.setattr(turn_execution, "RunJournalWriter", lambda *_args: object())
    monkeypatch.setattr(
        turn_execution,
        "RunEventSink",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("sink failed")),
    )

    with pytest.raises(RuntimeError, match="sink failed"):
        turn_execution.TurnExecution.start(
            stream_id="run-1",
            session_id="session-1",
            phase="starting",
        )

    assert calls[-1] == ("finish", "run-1")


def test_finish_is_idempotent(monkeypatch):
    calls = []
    _wire_runtime(monkeypatch, calls)
    monkeypatch.setattr(turn_execution, "RunJournalWriter", lambda *_args: object())
    monkeypatch.setattr(turn_execution, "RunEventSink", lambda **_kwargs: _Sink())
    execution = turn_execution.TurnExecution.start(
        stream_id="run-1",
        session_id="session-1",
        phase="starting",
    )
    assert execution is not None

    assert execution.finish() is True
    assert execution.finish() is False
    assert calls.count(("finish", "run-1")) == 1


def test_finish_can_retry_after_cleanup_failure(monkeypatch):
    calls = []
    _wire_runtime(monkeypatch, calls)
    monkeypatch.setattr(turn_execution, "RunJournalWriter", lambda *_args: object())
    monkeypatch.setattr(turn_execution, "RunEventSink", lambda **_kwargs: _Sink())
    execution = turn_execution.TurnExecution.start(
        stream_id="run-1",
        session_id="session-1",
        phase="starting",
    )
    assert execution is not None
    attempts = []

    def flaky_finish(stream_id):
        attempts.append(stream_id)
        if len(attempts) == 1:
            raise RuntimeError("cleanup failed")
        return True

    monkeypatch.setattr(turn_execution, "finish_runtime_run", flaky_finish)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        execution.finish()
    assert execution.finish() is True
    assert execution.finish() is False
    assert attempts == ["run-1", "run-1"]
