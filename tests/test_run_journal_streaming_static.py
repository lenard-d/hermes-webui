from pathlib import Path

from api.streaming import _compact_for_echo_compare


def test_streaming_initializes_one_run_journal_writer_per_stream():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    register_idx = src.index("register_active_run(")
    writer_idx = src.index("RunJournalWriter(session_id, stream_id)", register_idx)
    cancel_idx = src.index("cancel_event = threading.Event()", writer_idx)

    assert "from api.run_journal import RunJournalWriter" in src
    assert register_idx < writer_idx < cancel_idx


def test_streaming_journals_sse_events_before_queue_delivery():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    put_idx = src.index("def put(event, data):")
    sink_idx = src.rindex("event_sink = RunEventSink(", 0, put_idx)
    publish_idx = src.index("event_sink.publish(event, data)", put_idx)

    assert sink_idx < put_idx < publish_idx
    assert "journal=run_journal" in src[sink_idx:put_idx]


def test_visible_process_echo_compare_ignores_all_whitespace():
    token_text = "先把 issue 4249 拉下来\n\n先看正文和评论"
    interim_text = "先把 issue 4249 拉下来先看正文和评论"

    assert _compact_for_echo_compare(token_text) == _compact_for_echo_compare(interim_text)
