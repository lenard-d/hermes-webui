"""Behavioral contract for journal-backed live run event publication."""

from __future__ import annotations

import queue

from api.run_event_sink import RunEventSink


class _Journal:
    def __init__(self, calls, *, event_id="run-1:7", error=None):
        self.calls = calls
        self.event_id = event_id
        self.error = error

    def append_sse_event(self, event, data):
        self.calls.append(("journal", event, data))
        if self.error is not None:
            raise self.error
        return {"event_id": self.event_id}


class _StreamChannel:
    def __init__(self, calls, *, fail_note=False, fail_put=False):
        self.calls = calls
        self.fail_note = fail_note
        self.fail_put = fail_put

    def subscribe_with_snapshot(self):
        raise AssertionError("capability marker only")

    def note_last_event_id(self, event_id):
        self.calls.append(("transport_cursor", event_id))
        if self.fail_note:
            raise RuntimeError("cursor failed")

    def put_nowait(self, item):
        self.calls.append(("transport", item))
        if self.fail_put:
            raise RuntimeError("put failed")


def test_sink_publishes_durable_cursor_before_exact_live_frame():
    calls = []
    transport = _StreamChannel(calls)
    sink = RunEventSink(
        stream_id="run-1",
        transport=transport,
        journal=_Journal(calls),
        record_runtime_cursor=lambda stream_id, event_id: calls.append(
            ("runtime_cursor", stream_id, event_id)
        ),
    )

    event_id = sink.publish("token", {"text": "hello"})

    assert event_id == "run-1:7"
    assert calls == [
        ("journal", "token", {"text": "hello"}),
        ("runtime_cursor", "run-1", "run-1:7"),
        ("transport_cursor", "run-1:7"),
        ("transport", ("token", {"text": "hello"}, "run-1:7")),
    ]


def test_sink_preserves_two_tuple_contract_for_legacy_queue():
    transport = queue.Queue()
    sink = RunEventSink(
        stream_id="run-1",
        transport=transport,
        journal=_Journal([]),
        record_runtime_cursor=lambda *_args: None,
    )

    sink.publish("token", {"text": "hello"})

    assert transport.get_nowait() == ("token", {"text": "hello"})


def test_sink_keeps_live_delivery_when_journal_fails():
    transport = queue.Queue()
    sink = RunEventSink(
        stream_id="run-1",
        transport=transport,
        journal=_Journal([], error=RuntimeError("disk failed")),
        record_runtime_cursor=lambda *_args: (_ for _ in ()).throw(
            AssertionError("no cursor without a durable event id")
        ),
    )

    assert sink.publish("warning", {"message": "live only"}) is None
    assert transport.get_nowait() == ("warning", {"message": "live only"})


def test_sink_does_not_lose_frame_when_cursor_side_effects_fail():
    calls = []
    transport = _StreamChannel(calls, fail_note=True)

    def fail_runtime_cursor(*_args):
        calls.append(("runtime_cursor_failed",))
        raise RuntimeError("runtime cursor failed")

    sink = RunEventSink(
        stream_id="run-1",
        transport=transport,
        journal=_Journal(calls),
        record_runtime_cursor=fail_runtime_cursor,
    )

    assert sink.publish("done", {"ok": True}) == "run-1:7"
    assert calls[-1] == ("transport", ("done", {"ok": True}, "run-1:7"))


def test_sink_contains_live_transport_failure_after_durable_append():
    calls = []
    sink = RunEventSink(
        stream_id="run-1",
        transport=_StreamChannel(calls, fail_put=True),
        journal=_Journal(calls),
        record_runtime_cursor=lambda *_args: None,
    )

    assert sink.publish("done", {"ok": True}) == "run-1:7"
    assert calls[-1] == ("transport", ("done", {"ok": True}, "run-1:7"))


def test_sink_rejects_malformed_journal_event_id():
    transport = _StreamChannel([])
    sink = RunEventSink(
        stream_id="run-1",
        transport=transport,
        journal=_Journal([], event_id=7),
        record_runtime_cursor=lambda *_args: None,
    )

    assert sink.publish("token", {}) is None
    assert transport.calls == [("transport", ("token", {}))]
