from tests.frontend_asset_contract import family_source
from tests.test_local_run_modules import _translator
from pathlib import Path


def test_tool_start_callback_emits_existing_tool_sse_event_with_tool_id(monkeypatch):
    api, events, translator = _translator(monkeypatch)
    translator.tool_start("tool-1", "terminal", {"command": "pwd"})
    translator.tool_start("tool-1", "terminal", {"command": "pwd"})
    tool_events = [payload for event, payload in events if event == "tool"]
    assert tool_events == [{
        "event_type": "tool.started",
        "name": "terminal",
        "preview": None,
        "args": {"command": "pwd"},
        "tid": "tool-1",
    }]
    assert api._test_started[0][1]["tool_call_id"] == "tool-1"


def test_tool_complete_callback_emits_existing_tool_complete_sse_event_with_tool_id(
    monkeypatch,
):
    api, events, translator = _translator(monkeypatch)
    translator.tool_complete("tool-1", "terminal", {}, "done")
    translator.tool_complete("tool-1", "terminal", {}, "done")
    complete_events = [payload for event, payload in events if event == "tool_complete"]
    assert complete_events == [{
        "event_type": "tool.completed",
        "name": "terminal",
        "preview": "done",
        "args": {},
        "tid": "tool-1",
        "is_error": False,
    }]
    assert api._test_finished[0][1]["tool_call_id"] == "tool-1"
    assert translator.checkpoint_activity == [1]


def test_legacy_progress_events_are_suppressed_when_structured_callbacks_are_wired(
    monkeypatch,
):
    api, events, translator = _translator(
        monkeypatch,
        parameters={"tool_start_callback", "tool_complete_callback"}
    )
    translator.tool("tool.started", "terminal", None, {})
    translator.tool("tool.completed", "terminal", "done", {})
    assert not [event for event, _payload in events if event in {"tool", "tool_complete"}]
    assert api._test_started == []
    assert api._test_finished == []


def test_tool_callback_events_keep_existing_frontend_event_contract():
    messages = (
        Path(__file__).resolve().parents[1]
        / "static/modules/messages/live-tools.js"
    ).read_text(encoding="utf-8")
    ui = family_source("ui")

    assert "source.addEventListener('tool',event=>{" in messages
    assert "source.addEventListener('tool_complete',event=>{" in messages
    assert "String(d&&d.tid" in messages or "explicitTid=String(d&&d.tid" in messages, (
        "frontend tool handlers must still consume explicit server tid when present"
    )
    assert "upsertLiveToolCall(payload,'start')" in messages
    assert "upsertLiveToolCall(payload,'complete')" in messages
    assert "data-live-tid" in ui
    assert "existing.replaceWith(replacement)" in ui
