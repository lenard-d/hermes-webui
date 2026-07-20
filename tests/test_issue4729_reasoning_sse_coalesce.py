"""Regression test for #4729 — reasoning SSE coalescing throttle.

The bug: during the reasoning/thinking phase of models like DeepSeek the server
emitted one SSE `reasoning` event per token (tens of thousands per turn), each
triggering a full-text scan in the frontend renderer and freezing the JS main thread.

The fix throttles reasoning SSE events to ~10 Hz. The SUBTLE correctness requirement
(the reason the first attempt was bounced): reasoning deltas are INCREMENTAL and the
frontend APPENDS them (`reasoningText += text` in static/messages.js), so the throttle
must COALESCE — accumulate dropped deltas into a buffer and flush the buffer, NOT drop
deltas — otherwise live reasoning text is permanently lost. And the tail (the last
sub-100ms window) must be flushed when the reasoning phase ends, or it's lost too.

These are source-structure assertions on the on_reasoning closure in api/streaming.py
(the closure isn't unit-testable in isolation), pinning the three properties so the
coalescing contract can't silently regress to the drop-based version.
"""
from tests.frontend_asset_contract import family_source

import pathlib
import time

from tests.test_local_run_modules import _translator

REPO = pathlib.Path(__file__).parent.parent
STREAMING = (REPO / "api" / "runs" / "local.py").read_text(encoding="utf-8")
MESSAGES = family_source("messages")


def test_reasoning_uses_coalescing_buffer_not_drop(monkeypatch):
    _api, events, translator = _translator(monkeypatch)
    translator._reasoning_last_publish = time.monotonic() + 60
    translator.reasoning("one")
    translator.reasoning(" two")
    assert not [event for event, _payload in events if event == "reasoning"]
    translator.flush_reasoning()
    assert [payload["text"] for event, payload in events if event == "reasoning"] == [
        "one two"
    ]


def test_reasoning_throttle_is_rate_limited(monkeypatch):
    _api, events, translator = _translator(monkeypatch)
    translator._reasoning_last_publish = time.monotonic()
    translator.reasoning("one")
    translator.reasoning("two")
    assert not [event for event, _payload in events if event == "reasoning"]


def test_reasoning_tail_flushed_on_phase_end(monkeypatch):
    _api, events, translator = _translator(monkeypatch)
    translator._reasoning_last_publish = time.monotonic() + 60
    translator.reasoning("tail")
    translator.reasoning(None)
    assert ("reasoning", {"text": "tail"}) in events


def test_reasoning_buffer_flushed_at_every_boundary(monkeypatch):
    # #4729 (Codex re-gate): on_reasoning(None) is effectively dead — the agent never
    # calls reasoning_callback(None) — so the buffered tail must be flushed at the REAL
    # boundaries that close/reorder the live reasoning stream, or it's silently lost:
    #   - a shared _flush_reasoning_buffer() helper
    #   - on_token (visible output starting)
    #   - on_tool (tool boundary)
    #   - after agent.run_conversation() returns (terminal catch-all: a turn can end on
    #     reasoning with no trailing token/tool)
    _api, events, translator = _translator(monkeypatch)
    translator._reasoning_last_publish = time.monotonic() + 60
    translator.reasoning("before token")
    translator.token("answer")
    translator.reasoning("before tool")
    translator.tool("tool.started", "terminal", None, {})
    assert [payload["text"] for event, payload in events if event == "reasoning"] == [
        "before token",
        "before tool",
    ]
    assert "_flush_reasoning_buffer = event_translator.flush_reasoning" in STREAMING
    assert STREAMING.count("_flush_reasoning_buffer()") >= 2, (
        "the run owner must flush after conversation completion and in guaranteed cleanup"
    )



def test_frontend_appends_reasoning_deltas():
    # The whole coalesce requirement hinges on the frontend APPENDING (not replacing).
    # If this ever changes to assignment, the throttle design must change with it.
    assert "appendReasoning:text=>{ reasoningText+=text;liveReasoningText+=text; }" in MESSAGES, (
        "frontend reasoning handler must append deltas — if this changes, revisit the "
        "server-side coalescing throttle (#4729)"
    )
