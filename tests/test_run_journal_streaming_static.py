from pathlib import Path

from api.streaming import _compact_for_echo_compare


def test_streaming_claims_turn_execution_before_worker_journal_event():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    execution_idx = src.index("execution = TurnExecution.start(")
    sink_idx = src.index("event_sink = execution.event_sink", execution_idx)
    worker_event_idx = src.index('"event": "worker_started"', sink_idx)

    assert "from api.turn_execution import TurnExecution" in src
    assert execution_idx < sink_idx < worker_event_idx


def test_streaming_journals_sse_events_before_queue_delivery():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    put_idx = src.index("def put(event, data):")
    sink_idx = src.rindex("event_sink = execution.event_sink", 0, put_idx)
    publish_idx = src.index("event_sink.publish(event, data)", put_idx)

    assert sink_idx < put_idx < publish_idx


def test_visible_process_echo_compare_ignores_all_whitespace():
    token_text = "先把 issue 4249 拉下来\n\n先看正文和评论"
    interim_text = "先把 issue 4249 拉下来先看正文和评论"

    assert _compact_for_echo_compare(token_text) == _compact_for_echo_compare(interim_text)
