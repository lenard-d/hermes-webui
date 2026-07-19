"""Regression test for stage-364 Opus-caught SHOULD-FIX (per-frame cursor):

When the live SSE stream errors mid-stream and the frontend falls back to
journal replay, live frames must carry an `id:` field so the frontend's
`_lastRunJournalSeq` cursor advances during the live phase. Otherwise replay
arrives with `after_seq=0` and the server replays every journaled event from
seq 1, double-rendering tokens against the live-phase `assistantText`
accumulator.

Implementation:

  - `RunEventSink` captures `journaled["event_id"]` from the run journal and
    records it through the runtime owner.
  - StreamChannel queue items carry `(event, data, event_id)` so active
    subscribers emit each frame with its own id instead of the latest global id.
  - Legacy plain queues keep `(event, data)` and use the runtime cursor view as
    a compatibility fallback.
  - Runtime terminal cleanup releases the cursor with the rest of the run.
"""

import queue
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
GATEWAY_CHAT_PY = (REPO_ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")


def test_local_producer_delegates_journal_and_cursor_publication_to_sink():
    """The local policy wrapper must delegate publication to RunEventSink."""
    put_def_idx = STREAMING_PY.find("def put(event, data):")
    assert put_def_idx != -1, "put(event, data) not found in api/streaming.py"
    setup = STREAMING_PY[max(0, put_def_idx - 700):put_def_idx]
    put_body = STREAMING_PY[put_def_idx:put_def_idx + 700]
    assert "event_sink = RunEventSink(" in setup
    assert "record_runtime_cursor=note_runtime_last_event_id" in setup
    assert "event_sink.publish(event, data)" in put_body


def test_gateway_producer_delegates_journal_and_cursor_publication_to_sink():
    """Gateway policy must use the same publication owner as local runs."""
    put_def_idx = GATEWAY_CHAT_PY.find("def put_gateway_event(event, data):")
    assert put_def_idx != -1, "put_gateway_event(event, data) not found"
    setup = GATEWAY_CHAT_PY[max(0, put_def_idx - 700):put_def_idx]
    put_body = GATEWAY_CHAT_PY[put_def_idx:put_def_idx + 700]
    assert "event_sink = RunEventSink(" in setup
    assert "record_runtime_cursor=note_runtime_last_event_id" in setup
    assert "event_sink.publish(event, data)" in put_body


def test_sse_handler_emits_runtime_cursor_for_legacy_queue(monkeypatch):
    """A legacy 2-tuple transport still emits the runtime-owned journal cursor."""
    from api import config, routes

    class Handler:
        def __init__(self):
            self.wfile = object()

        def send_response(self, _status):
            pass

        def send_header(self, _name, _value):
            pass

    stream_id = "stage364-live-cursor"
    transport = queue.Queue()
    transport.put_nowait(("token", {"text": "hello"}))
    transport.put_nowait(("stream_end", {"ok": True}))
    emitted = []
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "end_sse_headers", lambda *_args: None)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda *_args: None)
    monkeypatch.setattr(
        routes,
        "_sse_replay_run_journal_gap_checked",
        lambda *_args: (False, None),
    )
    monkeypatch.setattr(
        routes,
        "_sse_with_id",
        lambda _handler, event, data, event_id: emitted.append((event, data, event_id)),
    )
    config.register_runtime_stream(stream_id, "stage364-session", transport)
    config.note_runtime_last_event_id(stream_id, f"{stream_id}:3")
    try:
        assert routes._handle_sse_stream(
            Handler(),
            urlparse(f"/api/chat/stream?stream_id={stream_id}"),
        ) is True
    finally:
        config.finish_runtime_run(stream_id)

    assert emitted[0] == ("token", {"text": "hello"}, f"{stream_id}:3")


def test_runtime_cleanup_releases_last_event_id():
    """Terminal runtime cleanup releases the cursor with the rest of the run."""
    import api.config as config

    stream_id = "last-event-id-cleanup-contract"
    config.register_runtime_stream(
        stream_id,
        "last-event-id-cleanup-session",
        object(),
    )
    config.note_runtime_last_event_id(stream_id, f"{stream_id}:3")
    try:
        config.finish_runtime_run(stream_id)
        assert config.runtime_last_event_id(stream_id) is None
    finally:
        config.finish_runtime_run(stream_id)


def test_runtime_cursor_view_hides_registry_from_consumer():
    import api.config as config

    stream_id = "stage364-runtime-cursor"
    config.note_runtime_last_event_id(stream_id, f"{stream_id}:4")
    try:
        assert config.runtime_last_event_id(stream_id) == f"{stream_id}:4"
    finally:
        config.finish_runtime_run(stream_id)
