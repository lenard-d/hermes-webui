import os

import pytest

from api.turn_journal import (
    append_turn_journal_event,
    append_turn_journal_event_for_stream,
    derive_turn_journal_states,
    read_turn_journal,
)


def test_append_turn_journal_event_for_stream_reuses_submitted_turn_id(tmp_path):
    submitted = append_turn_journal_event(
        "sid-1",
        {"event": "submitted", "turn_id": "turn-1", "stream_id": "stream-1", "content": "hello"},
        session_dir=tmp_path,
    )

    worker = append_turn_journal_event_for_stream(
        "sid-1",
        "stream-1",
        {"event": "worker_started"},
        session_dir=tmp_path,
    )

    assert submitted["turn_id"] == "turn-1"
    assert worker["turn_id"] == "turn-1"
    states, _ = derive_turn_journal_states([submitted, worker])
    assert states["turn-1"]["event"] == "worker_started"


def test_append_turn_journal_event_for_stream_falls_back_to_new_turn_for_missing_stream(tmp_path):
    event = append_turn_journal_event_for_stream(
        "sid-1",
        "stream-missing",
        {"event": "interrupted", "reason": "no submitted event found"},
        session_dir=tmp_path,
    )

    assert event["stream_id"] == "stream-missing"
    assert event["turn_id"]
    assert event["event"] == "interrupted"


def test_append_turn_journal_event_for_stream_can_require_existing_turn(tmp_path):
    with pytest.raises(LookupError, match="stream-missing"):
        append_turn_journal_event_for_stream(
            "sid-1",
            "stream-missing",
            {"event": "worker_started"},
            session_dir=tmp_path,
            require_existing_turn=True,
        )

    assert not (tmp_path / "_turn_journal").exists()


def test_strict_stream_append_rejects_conflicting_turn_identity(tmp_path):
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-authoritative",
            "stream_id": "stream-1",
        },
        session_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="turn identity does not match"):
        append_turn_journal_event_for_stream(
            "sid-1",
            "stream-1",
            {"event": "worker_started", "turn_id": "turn-forged"},
            session_dir=tmp_path,
            require_existing_turn=True,
        )

    events = read_turn_journal("sid-1", session_dir=tmp_path)["events"]
    assert [event["event"] for event in events] == ["submitted"]


def test_append_turn_journal_event_skips_directory_fsync_without_o_directory(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)

    event = append_turn_journal_event(
        "sid-windows",
        {"event": "submitted", "content": "hello"},
        session_dir=tmp_path,
    )

    assert event["event"] == "submitted"
    journal_dir = tmp_path / "_turn_journal"
    shards = list(journal_dir.glob(f"sid-windows~{os.getpid()}.jsonl"))
    assert len(shards) == 1, f"expected one pid-scoped shard, found: {list(journal_dir.iterdir())}"
