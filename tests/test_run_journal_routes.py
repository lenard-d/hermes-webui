from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import io
import json
import queue

import api.config as config
from api.sessions.anchor_scene import journal_projection as anchor_journal_owner

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_QUERIES_SRC = (
    ROOT / "api" / "http" / "routes" / "workspace_queries.py"
).read_text(encoding="utf-8")
STREAM_TRANSPORT_SRC = (
    ROOT / "api" / "routes_parts" / "stream_transport.py"
).read_text(encoding="utf-8")


def test_stream_status_exposes_replay_summary():
    status_pos = WORKSPACE_QUERIES_SRC.index(
        'parsed.path == "/api/chat/stream/status"'
    )
    block = WORKSPACE_QUERIES_SRC[status_pos : status_pos + 900]

    assert "find_run_summary(stream_id)" in block
    assert '"replay_available"' in block
    assert '"journal"' in block
    assert "_run_journal_status_payload" in block


def test_dead_stream_sse_replays_journal_before_404_fallback():
    handler_pos = STREAM_TRANSPORT_SRC.index("def _handle_sse_stream")
    block = STREAM_TRANSPORT_SRC[handler_pos : handler_pos + 1800]

    assert "find_run_summary(stream_id)" in block
    assert "stream not found" in block
    assert "_replay_run_journal" in block
    assert "_parse_run_journal_after_seq" in block
    assert 'Content-Type", "text/event-stream; charset=utf-8"' in block


def test_active_stream_replay_uses_snapshot_cutoff_and_skips_duplicate_queue_items(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "replayed"}, "run_1:1"))
            self.q.put_nowait(("stream_end", {}, "run_1:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_1:1", "offline_buffered_events": 1}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    stream = FakeStream()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {
            "events": [
                {
                    "event": "token",
                    "payload": {"text": "replayed"},
                    "event_id": f"{run_id}:1",
                }
            ]
        },
    )
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_args, **_kwargs: None)
    config.register_runtime_stream("run_1", "session_1", stream)
    try:
        routes._handle_sse_stream(handler, urlparse("/api/chat/stream?stream_id=run_1&replay=1&after_seq=0"))
    finally:
        config.finish_runtime_run("run_1")

    body = handler.wfile.getvalue().decode("utf-8")
    assert body.count("event: token\n") == 1
    assert "id: run_1:1\n" in body
    assert "id: run_1:2\n" in body
    assert stream.unsubscribed is True


def test_active_stream_snapshot_keeps_items_for_new_run_with_same_seq_range(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "fresh"}, "run_new:1"))
            self.q.put_nowait(("stream_end", {}, "run_new:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {
                "last_event_id": "run_old:3",
                "offline_buffered_events": 2,
            }

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    stream = FakeStream()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_2",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {"events": []},
    )
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_args, **_kwargs: None)
    config.register_runtime_stream("run_new", "session_2", stream)
    try:
        routes._handle_sse_stream(
            handler,
            urlparse("/api/chat/stream?stream_id=run_new&replay=1&after_seq=0"),
        )
    finally:
        config.finish_runtime_run("run_new")

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_new:1\n" in body
    assert "id: run_new:2\n" in body
    assert body.count("id: run_new:1\n") == 1
    assert stream.unsubscribed is True


def test_active_stream_replay_without_journal_keeps_buffered_queue_items(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "buffered"}, "missing_journal_run:1"))
            self.q.put_nowait(("stream_end", {}, "missing_journal_run:2"))

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "missing_journal_run:1", "offline_buffered_events": 1}

        def unsubscribe(self, _q):
            pass

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: None)
    handler = Handler()
    config.register_runtime_stream(
        "missing_journal_run",
        "missing-journal-session",
        FakeStream(),
    )
    try:
        routes._handle_sse_stream(
            handler,
            urlparse("/api/chat/stream?stream_id=missing_journal_run&replay=1&after_seq=0"),
        )
    finally:
        config.finish_runtime_run("missing_journal_run")

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: missing_journal_run:1\n" in body
    assert "event: token\n" in body
    assert "buffered" in body


def test_live_sse_uses_each_queue_items_own_event_id():
    import api.routes as routes
    from api.config import create_stream_channel

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    stream = create_stream_channel()
    stream.put_nowait(("token", {"text": "A"}, "run_own_id:1"))
    stream.put_nowait(("stream_end", {"ok": True}, "run_own_id:2"))
    handler = Handler()
    config.register_runtime_stream("run_own_id", "run-own-session", stream)
    try:
        routes._handle_sse_stream(handler, urlparse("/api/chat/stream?stream_id=run_own_id"))
    finally:
        config.finish_runtime_run("run_own_id")

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_own_id:1\nevent: token\n" in body
    assert "id: run_own_id:2\nevent: stream_end\n" in body
    assert body.count("id: run_own_id:2\n") == 1


def test_replay_emits_event_ids_and_stale_restart_diagnostic():
    replay_pos = STREAM_TRANSPORT_SRC.index("def _replay_run_journal")
    block = STREAM_TRANSPORT_SRC[replay_pos : replay_pos + 1200]

    assert "read_run_events" in block
    assert "_sse_with_id" in block
    assert "stale_interrupted_event" in block


def test_session_payload_exposes_durable_runtime_journal_for_stale_streams(
    monkeypatch, tmp_path
):
    """A stale stream clears its live projection without hiding its journal evidence."""
    import api.routes as routes
    from api.runs import journal as run_journal
    from api.http.routes import session_queries
    from api.sessions.store import Session

    session_id = "stale-session"
    stream_id = "stale-run"
    run_journal.append_run_event(
        session_id,
        stream_id,
        "token",
        {"text": "durable partial output"},
        session_dir=tmp_path,
    )

    session = Session(
        session_id=session_id,
        title="Stale run",
        messages=[],
        context_length=128_000,
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "continue this work"
    session.pending_attachments = []
    session.pending_started_at = 1.0
    session.pending_user_source = "webui"
    cleared_streams = []

    def clear_stale_stream_state(current):
        cleared_streams.append(current.active_stream_id)
        current.active_stream_id = None
        current.pending_user_message = None
        current.pending_attachments = []
        current.pending_started_at = None
        current.pending_user_source = None
        return True

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", clear_stale_stream_state)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda run_id: run_journal.find_run_summary(run_id, session_dir=tmp_path),
    )
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(
        session_queries.session_detail_projection,
        "metadata_summary",
        lambda *_args, **_kwargs: {"message_count": 0, "last_message_at": 0},
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, **_kwargs: payload,
    )

    response = routes.handle_get(
        object(),
        urlparse(f"/api/session?session_id={session_id}&messages=0&resolve_model=0"),
    )
    payload = response["session"]

    assert cleared_streams == [stream_id]
    assert payload["active_stream_id"] is None
    assert payload["runtime_journal"] == {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 1,
        "last_event_id": f"{stream_id}:1",
        "last_event": "token",
        "terminal": False,
        "terminal_state": "lost-worker-bookkeeping",
    }
    assert "runtime_journal_snapshot" not in payload
    assert run_journal.read_run_events(session_id, stream_id, session_dir=tmp_path)[
        "events"
    ][0]["payload"] == {"text": "durable partial output"}


def test_message_tail_can_skip_duplicate_live_journal_snapshot(monkeypatch):
    """The transcript fetch keeps journal status but must not rebuild metadata's snapshot."""
    import api.routes as routes
    from api.http.routes import session_queries
    from api.sessions.store import Session

    session_id = "live-session"
    stream_id = "live-run"
    session = Session(
        session_id=session_id,
        title="Live run",
        messages=[{"role": "user", "content": "hello"}],
        context_length=128_000,
    )
    session.active_stream_id = stream_id
    snapshot_calls = []

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: False)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {stream_id})
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _run_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 7,
            "last_event_id": f"{stream_id}:7",
            "last_event": "token",
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "_run_journal_live_snapshot",
        lambda run_id: snapshot_calls.append(run_id) or {"assistant_text": "duplicate"},
    )
    monkeypatch.setattr(
        routes,
        "_stream_id_visible_to_request_profile",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(
        session_queries.session_detail_projection,
        "metadata_summary",
        lambda *_args, **_kwargs: {"message_count": 1, "last_message_at": 0},
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    response = routes.handle_get(
        object(),
        urlparse(
            f"/api/session?session_id={session_id}&messages=1&resolve_model=0"
            "&runtime_snapshot=0"
        ),
    )
    payload = response["session"]

    assert payload["runtime_journal"]["run_id"] == stream_id
    assert payload["runtime_journal"]["terminal"] is False
    assert snapshot_calls == []
    assert "runtime_journal_snapshot" not in payload


def test_live_journal_snapshot_reconstructs_visible_progress_and_tool_aliases(monkeypatch):
    monkeypatch.setattr(
        anchor_journal_owner,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "last_seq": 4,
            "last_event_id": f"{stream_id}:4",
        },
    )
    monkeypatch.setattr(
        anchor_journal_owner,
        "read_run_events",
        lambda session_id, run_id: {
            "events": [
                {
                    "seq": 1,
                    "event": "token",
                    "payload": {"text": "First segment."},
                    "event_id": f"{run_id}:1",
                    "created_at": 1000.0,
                },
                {
                    "seq": 2,
                    "event": "tool",
                    "payload": {
                        "name": "terminal",
                        "preview": "running tests",
                        "tool_use_id": "toolu_123",
                        "args": {"command": "pytest -q", "extra": "x" * 200},
                    },
                    "event_id": f"{run_id}:2",
                },
                {
                    "seq": 3,
                    "event": "tool_complete",
                    "payload": {
                        "name": "terminal",
                        "preview": "passed",
                        "tool_use_id": "toolu_123",
                        "duration": 1.25,
                    },
                    "event_id": f"{run_id}:3",
                },
                {
                    "seq": 4,
                    "event": "reasoning",
                    "payload": {"text": "Checked result."},
                    "event_id": f"{run_id}:4",
                },
                {
                    "seq": 5,
                    "event": "token",
                    "payload": {"text": " Second segment."},
                    "event_id": f"{run_id}:5",
                    "created_at": 1001.0,
                },
            ]
        },
    )

    snapshot = anchor_journal_owner._run_journal_live_snapshot("run_1")

    assert snapshot["last_seq"] == 5
    assert snapshot["last_event_id"] == "run_1:5"
    assert snapshot["last_assistant_text"] == "First segment. Second segment."
    assert snapshot["last_reasoning_text"] == "Checked result."
    assert snapshot["current_live_segment_seq"] == 2
    assert snapshot["activity_burst_anchors"] == [{"id": 1, "textEnd": len("First segment.")}]
    assert snapshot["messages"] == [
        {
            "role": "assistant",
            "content": "First segment. Second segment.",
            "reasoning": "Checked result.",
            "_live": True,
            "_journal_snapshot": True,
            "_journal_stream_id": "run_1",
            "_ts": 1001.0,
        }
    ]
    tool = snapshot["tool_calls"][0]
    assert tool["name"] == "terminal"
    assert tool["done"] is True
    assert tool["tid"] == "toolu_123"
    assert tool["tool_use_id"] == "toolu_123"
    assert tool["activityBurstId"] == 1
    assert tool["activitySegmentSeq"] == 1
    assert tool["snippet"] == "passed"
    assert tool["duration"] == 1.25
    assert tool["args"]["extra"] == "x" * 200


def test_live_journal_snapshot_bounds_pathological_tool_args(monkeypatch):
    long_command = "python -c " + repr("print('x')\n" * 24)
    huge_args = {
        "command": long_command,
        "items": [{"index": i, "payload": "x" * 100} for i in range(50_000)],
    }
    monkeypatch.setattr(
        anchor_journal_owner,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "last_seq": 1,
            "last_event_id": f"{stream_id}:1",
        },
    )
    monkeypatch.setattr(
        anchor_journal_owner,
        "read_run_events",
        lambda session_id, run_id: {
            "events": [
                {
                    "seq": 1,
                    "event": "tool",
                    "payload": {
                        "name": "terminal",
                        "tool_use_id": "toolu_huge",
                        "args": huge_args,
                    },
                    "event_id": f"{run_id}:1",
                },
            ]
        },
    )

    snapshot = anchor_journal_owner._run_journal_live_snapshot("run_1")
    tool = snapshot["tool_calls"][0]
    assert tool["args"]["command"] == long_command
    assert len(tool["args"]["items"]) <= 64
    assert len(json.dumps(snapshot, sort_keys=True)) < 200_000


def test_status_payload_marks_non_terminal_dead_journal_as_stale():
    payload = anchor_journal_owner._run_journal_status_payload(
        {
            "session_id": "session_1",
            "run_id": "run_1",
            "last_seq": 3,
            "last_event_id": "run_1:3",
            "last_event": "token",
            "terminal": False,
            "terminal_state": "running",
        },
        active=False,
    )

    assert payload["terminal"] is False
    assert payload["terminal_state"] == "lost-worker-bookkeeping"
    assert payload["last_event_id"] == "run_1:3"


def test_status_payload_preserves_terminal_error_state():
    payload = anchor_journal_owner._run_journal_status_payload(
        {
            "session_id": "session_1",
            "run_id": "run_1",
            "terminal": True,
            "terminal_state": "interrupted-by-crash",
            "last_event": "apperror",
        },
        active=False,
    )

    assert payload["terminal"] is True
    assert payload["terminal_state"] == "interrupted-by-crash"


def test_replay_run_journal_writes_replayed_events_and_synthetic_terminal(monkeypatch):
    import api.routes as routes

    handler = SimpleNamespace(wfile=io.BytesIO())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {
            "events": [
                {
                    "event": "token",
                    "payload": {"text": "hello"},
                    "event_id": f"{run_id}:1",
                }
            ]
        },
    )
    monkeypatch.setattr(
        routes,
        "stale_interrupted_event",
        lambda session_id, run_id, after_seq=None, max_seq=None: {
            "event": "apperror",
            "payload": {"type": "interrupted"},
            "event_id": f"{run_id}:2",
        },
    )

    assert routes._replay_run_journal(handler, "run_1", 0) is True
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:1\n" in body
    assert "event: token\n" in body
    assert "id: run_1:2\n" in body
    assert "event: apperror\n" in body


def test_replay_run_journal_honors_after_seq_cursor(monkeypatch):
    import api.routes as routes

    captured = {}
    handler = SimpleNamespace(wfile=io.BytesIO())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": True,
        },
    )

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        captured["after_seq"] = after_seq
        captured["max_seq"] = max_seq
        return {
            "events": [
                {
                    "event": "done",
                    "payload": {"session": {"session_id": session_id}},
                    "event_id": f"{run_id}:4",
                }
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)

    assert routes._replay_run_journal(handler, "run_1", 3) is True
    assert captured["after_seq"] == 3
    assert captured["max_seq"] is None
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:4\n" in body
    assert "event: done\n" in body


def test_active_stream_replay_keeps_items_for_new_run_with_same_seq_range(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "fresh"}, "run_new:1"))
            self.q.put_nowait(("stream_end", {}, "run_new:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {
                "last_event_id": "run_old:3",
                "offline_buffered_events": 2,
            }

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    stream = FakeStream()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_2",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {"events": []},
    )
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_args, **_kwargs: None)
    config.register_runtime_stream("run_new", "session_2", stream)
    try:
        routes._handle_sse_stream(
            handler,
            urlparse("/api/chat/stream?stream_id=run_new&replay=1&after_seq=0"),
        )
    finally:
        config.finish_runtime_run("run_new")

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_new:1\n" in body
    assert "id: run_new:2\n" in body
    assert body.count("id: run_new:1\n") == 1
    assert stream.unsubscribed is True
