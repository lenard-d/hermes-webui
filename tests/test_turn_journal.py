import json
import os

import api.turn_journal as turn_journal
import pytest
from api.sessions.recovery import audit_session_recovery
from api.turn_journal import (
    TurnJournalCommitUnknown,
    append_turn_journal_event,
    derive_turn_journal_states,
    iter_turn_journal_session_ids,
    read_turn_journal,
)


def _write_session(session_dir, sid, messages=None):
    payload = {
        "session_id": sid,
        "title": "Turn journal test",
        "messages": messages or [],
    }
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_append_turn_journal_event_fsyncs_jsonl_and_preserves_payload(tmp_path):
    event = append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "role": "user",
            "content": "hello",
            "attachments": [{"name": "a.png", "path": "/tmp/a.png"}],
        },
        session_dir=tmp_path,
    )

    assert event["version"] == 1
    assert event["session_id"] == "sid-1"
    assert event["created_at"] > 0
    journal_dir = tmp_path / "_turn_journal"
    shards = list(journal_dir.glob(f"sid-1~{os.getpid()}.jsonl"))
    assert len(shards) == 1, f"expected one pid-scoped shard, found: {list(journal_dir.iterdir())}"
    lines = shards[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["content"] == "hello"


def test_confirm_turn_journal_event_refsyncs_exact_written_event(tmp_path, monkeypatch):
    expected = append_turn_journal_event(
        "sid-confirm",
        {
            "event": "submitted",
            "turn_id": "turn-confirm",
            "stream_id": "stream-confirm",
            "role": "user",
            "content": "durable prompt",
        },
        session_dir=tmp_path,
    )
    fsync_calls = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(turn_journal.os, "fsync", recording_fsync)

    confirmed = turn_journal.confirm_turn_journal_event(
        "sid-confirm",
        expected,
        session_dir=tmp_path,
    )

    assert confirmed == expected
    assert fsync_calls


def test_confirm_turn_journal_event_proves_missing_process_shard(tmp_path):
    expected = {
        "version": 1,
        "session_id": "sid-missing",
        "event": "submitted",
        "turn_id": "turn-missing",
        "stream_id": "stream-missing",
    }

    confirmed = turn_journal.confirm_turn_journal_event(
        "sid-missing",
        expected,
        session_dir=tmp_path,
    )

    assert confirmed is None


def test_confirm_turn_journal_event_rejects_conflicting_turn_identity(tmp_path):
    expected = append_turn_journal_event(
        "sid-conflict",
        {
            "event": "submitted",
            "turn_id": "turn-conflict",
            "stream_id": "stream-conflict",
            "content": "first",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event(
        "sid-conflict",
        {
            **expected,
            "content": "different durable payload",
        },
        session_dir=tmp_path,
    )

    with pytest.raises(TurnJournalCommitUnknown, match="ambiguous admission state"):
        turn_journal.confirm_turn_journal_event(
            "sid-conflict",
            expected,
            session_dir=tmp_path,
        )


def test_confirm_turn_journal_event_allows_prior_lifecycle_event_for_same_turn(
    tmp_path,
):
    submitted = append_turn_journal_event(
        "sid-lifecycle",
        {
            "event": "submitted",
            "turn_id": "turn-lifecycle",
            "stream_id": "stream-lifecycle",
        },
        session_dir=tmp_path,
    )
    interrupted = append_turn_journal_event(
        "sid-lifecycle",
        {
            "event": "interrupted",
            "turn_id": submitted["turn_id"],
            "stream_id": submitted["stream_id"],
            "reason": "admission_failed",
        },
        session_dir=tmp_path,
    )

    confirmed = turn_journal.confirm_turn_journal_event(
        "sid-lifecycle",
        interrupted,
        session_dir=tmp_path,
    )

    assert confirmed == interrupted


def test_confirm_turn_journal_event_fails_closed_when_refsync_fails(
    tmp_path, monkeypatch
):
    expected = append_turn_journal_event(
        "sid-refsync-failure",
        {
            "event": "submitted",
            "turn_id": "turn-refsync-failure",
            "stream_id": "stream-refsync-failure",
        },
        session_dir=tmp_path,
    )
    monkeypatch.setattr(
        turn_journal.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("fsync unavailable")),
    )

    with pytest.raises(TurnJournalCommitUnknown, match="unable to confirm"):
        turn_journal.confirm_turn_journal_event(
            "sid-refsync-failure",
            expected,
            session_dir=tmp_path,
        )


def test_read_turn_journal_tolerates_malformed_lines(tmp_path):
    journal_dir = tmp_path / "_turn_journal"
    journal_dir.mkdir()
    (journal_dir / "sid-1.jsonl").write_text(
        '{"event":"submitted","turn_id":"turn-1","session_id":"sid-1"}\n'
        'not-json\n'
        '{"event":"completed","turn_id":"turn-1","session_id":"sid-1"}\n',
        encoding="utf-8",
    )

    result = read_turn_journal("sid-1", session_dir=tmp_path)

    assert [event["event"] for event in result["events"]] == ["submitted", "completed"]
    assert len(result["malformed"]) == 1
    assert result["malformed"][0]["line"] == 2
    assert result["malformed"][0]["raw"] == "not-json"


def test_append_turn_journal_event_locks_around_write_and_fsync(tmp_path, monkeypatch):
    calls = []

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(fd, flag):
            calls.append((fd, flag))

    monkeypatch.setattr(turn_journal, "_fcntl", FakeFcntl)

    append_turn_journal_event(
        "sid-1",
        {"event": "submitted", "turn_id": "turn-locked", "content": "x" * 5000},
        session_dir=tmp_path,
    )

    assert [flag for _, flag in calls] == [FakeFcntl.LOCK_EX, FakeFcntl.LOCK_UN]


def test_append_turn_journal_event_still_writes_when_fcntl_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_journal, "_fcntl", None)

    append_turn_journal_event(
        "sid-1",
        {"event": "submitted", "turn_id": "turn-no-fcntl", "content": "hello"},
        session_dir=tmp_path,
    )

    result = read_turn_journal("sid-1", session_dir=tmp_path)
    assert result["events"][0]["turn_id"] == "turn-no-fcntl"


def test_derive_turn_journal_states_keeps_latest_event_per_turn():
    states, _ = derive_turn_journal_states([
        {"event": "submitted", "turn_id": "turn-1", "created_at": 1},
        {"event": "worker_started", "turn_id": "turn-1", "created_at": 2},
        {"event": "submitted", "turn_id": "turn-2", "created_at": 3},
        {"event": "completed", "turn_id": "turn-1", "created_at": 4},
    ])

    assert states["turn-1"]["event"] == "completed"
    assert states["turn-2"]["event"] == "submitted"


def test_derive_turn_journal_states_uses_created_at_not_file_order():
    states, _ = derive_turn_journal_states([
        {"event": "completed", "turn_id": "turn-1", "created_at": 20},
        {"event": "submitted", "turn_id": "turn-1", "created_at": 10},
    ])

    assert states["turn-1"]["event"] == "completed"


def test_audit_reports_pending_turn_journal_entry_when_user_message_absent(tmp_path):
    _write_session(tmp_path, "sid-1", messages=[])
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "stream_id": "stream-1",
            "role": "user",
            "content": "recover me",
            "attachments": [],
        },
        session_dir=tmp_path,
    )

    report = audit_session_recovery(tmp_path)

    assert report["status"] == "warn"
    assert report["summary"]["repairable"] == 1
    assert report["items"] == [
        {
            "session_id": "sid-1",
            "kind": "turn_journal_pending_turn",
            "category": "repairable",
            "recommendation": "audit_only_pending_turn_journal",
            "live_messages": 0,
            "bak_messages": -1,
            "turn_id": "turn-1",
            "event": "submitted",
        }
    ]


def test_audit_ignores_completed_or_already_materialized_turn_journal_entry(tmp_path):
    _write_session(tmp_path, "sid-1", messages=[{"role": "user", "content": "already there"}])
    append_turn_journal_event(
        "sid-1",
        {
            "event": "submitted",
            "turn_id": "turn-1",
            "role": "user",
            "content": "already there",
        },
        session_dir=tmp_path,
    )
    append_turn_journal_event(
        "sid-1",
        {"event": "completed", "turn_id": "turn-1"},
        session_dir=tmp_path,
    )

    report = audit_session_recovery(tmp_path)

    assert report["status"] == "ok"
    assert report["items"] == []


def test_derive_turn_journal_states_reports_terminal_collision_when_both_completed_and_interrupted():
    # A turn that recorded both completed and interrupted terminal events should
    # not silently collapse to one winner — the collision must be reported.
    events = [
        {'event': 'submitted', 'turn_id': 'turn-double-terminal', 'created_at': 1},
        {'event': 'worker_started', 'turn_id': 'turn-double-terminal', 'created_at': 2},
        {'event': 'completed', 'turn_id': 'turn-double-terminal', 'created_at': 3},
        {'event': 'interrupted', 'turn_id': 'turn-double-terminal', 'created_at': 4, 'reason': 'server_restart'},
    ]
    states, collisions = derive_turn_journal_states(events)

    # Derived state still picks the latest by timestamp (interrupted)
    assert states['turn-double-terminal']['event'] == 'interrupted'
    # But the collision is explicitly reported so callers can audit it
    assert len(collisions) == 1
    assert collisions[0]['turn_id'] == 'turn-double-terminal'
    assert [e['event'] for e in collisions[0]['events']] == ['completed', 'interrupted']


def test_derive_turn_journal_states_no_collision_when_single_terminal():
    # A normal turn with only one terminal event must not produce a collision.
    events = [
        {'event': 'submitted', 'turn_id': 'turn-normal', 'created_at': 1},
        {'event': 'worker_started', 'turn_id': 'turn-normal', 'created_at': 2},
        {'event': 'completed', 'turn_id': 'turn-normal', 'created_at': 3},
    ]
    states, collisions = derive_turn_journal_states(events)

    assert states['turn-normal']['event'] == 'completed'
    assert collisions == []


def test_terminal_field_set_on_completed_event(tmp_path):
    event = append_turn_journal_event(
        "sid-term",
        {"event": "completed", "turn_id": "turn-1"},
        session_dir=tmp_path,
    )
    assert event.get("terminal") is True


def test_terminal_field_set_on_interrupted_event(tmp_path):
    event = append_turn_journal_event(
        "sid-term-int",
        {"event": "interrupted", "turn_id": "turn-1"},
        session_dir=tmp_path,
    )
    assert event.get("terminal") is True


def test_terminal_field_not_set_on_non_terminal_event(tmp_path):
    event = append_turn_journal_event(
        "sid-nterm",
        {"event": "submitted", "turn_id": "turn-1", "content": "hi"},
        session_dir=tmp_path,
    )
    assert "terminal" not in event


def test_read_turn_journal_merges_pid_shards(tmp_path, monkeypatch):
    journal_dir = tmp_path / "_turn_journal"
    journal_dir.mkdir()

    # Simulate two worker processes writing separate shards
    monkeypatch.setattr(os, "getpid", lambda: 1001)
    append_turn_journal_event(
        "sid-merge",
        {"event": "submitted", "turn_id": "turn-1", "created_at": 1.0},
        session_dir=tmp_path,
    )
    monkeypatch.setattr(os, "getpid", lambda: 1002)
    append_turn_journal_event(
        "sid-merge",
        {"event": "completed", "turn_id": "turn-1", "created_at": 2.0},
        session_dir=tmp_path,
    )

    result = read_turn_journal("sid-merge", session_dir=tmp_path)

    assert len(result["events"]) == 2
    assert result["events"][0]["event"] == "submitted"
    assert result["events"][1]["event"] == "completed"


def test_iter_turn_journal_session_ids_deduplicates_pid_shards(tmp_path):
    journal_dir = tmp_path / "_turn_journal"
    journal_dir.mkdir()

    # Create two pid-scoped shards for the same session, plus a legacy file for another session
    (journal_dir / "sess-a~1001.jsonl").write_text("", encoding="utf-8")
    (journal_dir / "sess-a~1002.jsonl").write_text("", encoding="utf-8")
    (journal_dir / "sess-b.jsonl").write_text("", encoding="utf-8")

    ids = iter_turn_journal_session_ids(tmp_path)

    assert ids == ["sess-a", "sess-b"]
