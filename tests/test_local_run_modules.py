from __future__ import annotations

from types import SimpleNamespace

from api import streaming as _streaming_facade  # noqa: F401
import api.runs.local_agent_config as local_agent_config
import api.runs.local_events as local_events
import api.runs.local_usage as local_usage
from api.runs.local_agent_config import build_local_agent_configuration
from api.runs.local_events import LocalEventTranslator
from api.runs.local_usage import LocalUsageTracker


class _Meter:
    def __init__(self):
        self.tokens = []
        self.reasoning = []

    def get_stats(self, _stream_id):
        return {}

    def record_token(self, stream_id, count):
        self.tokens.append((stream_id, count))

    def record_reasoning(self, stream_id, count):
        self.reasoning.append((stream_id, count))


class _Usage:
    def snapshot(self):
        return {"input_tokens": 7}

    def record_tool_start(self, *_args):
        return True

    def record_tool_complete(self, *_args):
        return True


def _event_owner_spies(monkeypatch):
    meter = _Meter()
    partials = []
    reasoning = []
    started = []
    finished = []
    todos = []
    partial_text = {}
    reasoning_text = {}

    monkeypatch.setattr(local_events, "meter", lambda: meter)
    monkeypatch.setattr(local_events, "STREAM_PARTIAL_TEXT", partial_text)
    monkeypatch.setattr(local_events, "STREAM_REASONING_TEXT", reasoning_text)
    monkeypatch.setattr(
        local_events,
        "append_runtime_partial_text",
        lambda stream_id, text: partials.append((stream_id, text)),
    )
    monkeypatch.setattr(
        local_events,
        "append_runtime_reasoning_text",
        lambda stream_id, text: reasoning.append((stream_id, text)),
    )
    monkeypatch.setattr(local_events, "replace_runtime_reasoning_text", lambda *_args: None)
    monkeypatch.setattr(
        local_events,
        "start_runtime_tool_call",
        lambda stream_id, **kwargs: started.append((stream_id, kwargs)),
    )
    monkeypatch.setattr(
        local_events,
        "finish_runtime_tool_call",
        lambda stream_id, **kwargs: finished.append((stream_id, kwargs)),
    )
    monkeypatch.setattr(
        local_events,
        "emit_todo_state",
        lambda *_args, **kwargs: todos.append(kwargs),
    )

    return SimpleNamespace(
        STREAM_PARTIAL_TEXT=partial_text,
        STREAM_REASONING_TEXT=reasoning_text,
        _test_meter=meter,
        _test_partials=partials,
        _test_reasoning=reasoning,
        _test_started=started,
        _test_finished=finished,
        _test_todos=todos,
    )


def _translator(monkeypatch, *, parameters=()):
    spies = _event_owner_spies(monkeypatch)
    events = []
    translator = LocalEventTranslator(
        session_id="session-1",
        stream_id="stream-1",
        publish=lambda event, payload: events.append((event, payload)),
        usage=_Usage(),
        agent_params=lambda: set(parameters),
    )
    return spies, events, translator


def test_event_translator_preserves_reasoning_before_visible_output(monkeypatch):
    api, events, translator = _translator(monkeypatch)

    translator.reasoning("one")
    translator.reasoning(" two")
    translator.token("answer")

    assert api._test_reasoning == [("stream-1", "one"), ("stream-1", " two")]
    assert api._test_partials == [("stream-1", "answer")]
    assert (
        "".join(payload["text"] for event, payload in events if event == "reasoning")
        == "one two"
    )
    assert ("token", {"text": "answer"}) in events
    assert any(event == "metering" for event, _payload in events)
    assert translator.token_sent is True


def test_event_translator_keeps_reasoning_segments_separate_at_real_boundaries(
    monkeypatch,
):
    api, events, translator = _translator(monkeypatch)

    translator.reasoning("before tool")
    translator.tool("tool.started", "terminal", None, {})
    translator.tool("tool.started", "browser", None, {})
    translator.reasoning("after tool")
    translator.interim_assistant("interim")
    translator.reasoning("after interim")
    translator.flush_reasoning()

    assert translator.reasoning_segments == {
        0: "before tool",
        1: "after tool",
        2: "after interim",
    }
    assert translator.current_reasoning_idx == 2
    assert api.STREAM_REASONING_TEXT.get("stream-1", "") == ""
    assert (
        "".join(payload["text"] for event, payload in events if event == "reasoning")
        == "before toolafter toolafter interim"
    )


def test_reasoning_buffer_flushes_on_none_token_tool_and_explicit_finalization(
    monkeypatch,
):
    import time

    _api, events, translator = _translator(monkeypatch)

    translator._reasoning_last_publish = time.monotonic() + 60
    translator.reasoning("a")
    translator.reasoning(None)
    translator._reasoning_last_publish = time.monotonic() + 60
    translator.reasoning("b")
    translator.token("answer")
    translator._reasoning_last_publish = time.monotonic() + 60
    translator.reasoning("c")
    translator.tool("tool.started", "terminal", None, {})
    translator._reasoning_last_publish = time.monotonic() + 60
    translator.reasoning("d")
    translator.flush_reasoning()

    assert [payload["text"] for event, payload in events if event == "reasoning"] == [
        "a",
        "b",
        "c",
        "d",
    ]


def test_event_translator_captures_terminal_status_and_routes_lifecycle_events(
    monkeypatch,
):
    _api, events, translator = _translator(monkeypatch)

    translator.status("lifecycle", "Non-retryable error HTTP 400")
    translator.status("lifecycle", "Preflight compression: context is near limit")
    translator.status("lifecycle", "Rate limited — switching to fallback provider")

    assert translator.captured_terminal_error[0] == "Non-retryable error HTTP 400"
    assert events == [
        ("compressing", {"session_id": "session-1", "message": "Compressing context"}),
        (
            "warning",
            {
                "type": "fallback",
                "message": "Rate limited — switching to fallback provider",
            },
        ),
    ]


def test_structured_tool_callbacks_are_idempotent_and_checkpoint_completion(
    monkeypatch,
):
    api, events, translator = _translator(
        monkeypatch,
        parameters={"tool_start_callback", "tool_complete_callback"}
    )

    translator.tool_start("tool-1", "terminal", {"content": "x" * 800})
    translator.tool_start("tool-1", "terminal", {})
    translator.tool_complete("tool-1", "terminal", {}, "done")
    translator.tool_complete("tool-1", "terminal", {}, "done")

    assert len([event for event, _payload in events if event == "tool"]) == 1
    assert len([event for event, _payload in events if event == "tool_complete"]) == 1
    assert translator.checkpoint_activity == [1]
    assert api._test_started[0][1]["tool_call_id"] == "tool-1"
    assert api._test_finished[0][1]["tool_call_id"] == "tool-1"
    assert api._test_todos == [
        {
            "name": "terminal",
            "function_result": "done",
            "session_id": "session-1",
            "stream_id": "stream-1",
        }
    ]


def test_legacy_tool_progress_is_suppressed_when_structured_callbacks_exist(
    monkeypatch,
):
    api, events, translator = _translator(
        monkeypatch,
        parameters={"tool_start_callback", "tool_complete_callback"}
    )

    translator.tool("tool.started", "terminal", None, {})
    translator.tool("tool.completed", "terminal", "done", {})

    assert not [
        event for event, _payload in events if event in {"tool", "tool_complete"}
    ]
    assert api._test_started == []
    assert api._test_finished == []


def test_legacy_tool_completion_prefers_full_result_for_todo_state(monkeypatch):
    api, _events, translator = _translator(monkeypatch)

    translator.tool(
        "tool.completed",
        "todo",
        "preview",
        {},
        result={"todos": ["complete"]},
    )

    assert api._test_todos[0]["function_result"] == {"todos": ["complete"]}


def test_agent_configuration_adapts_to_installed_constructor_and_config(monkeypatch):
    class Agent:
        def __init__(
            self,
            model,
            max_iterations=None,
            max_tokens=None,
            status_callback=None,
            request_overrides=None,
            reasoning_config=None,
            api_mode=None,
            acp_command=None,
            acp_args=None,
            credential_pool=None,
            gateway_session_key=None,
            **_kwargs,
        ):
            pass

    _spies, _events, translator = _translator(monkeypatch)
    monkeypatch.setattr(
        local_agent_config,
        "coerce_reasoning_effort_for_model",
        lambda value, *_args, **_kwargs: value,
    )
    monkeypatch.setattr(
        local_agent_config,
        "parse_reasoning_effort",
        lambda value: {"effort": value},
    )
    result = build_local_agent_configuration(
        agent_class=Agent,
        config={
            "agent": {"max_turns": "123", "reasoning_effort": "high"},
            "max_tokens": "456",
            "fallback_providers": {"provider": "openai", "model": "gpt-test"},
        },
        model="model",
        provider="provider",
        base_url="https://example.test",
        api_key="secret",
        toolsets=["terminal"],
        session_id="session-1",
        session_db=None,
        prefill_messages=[],
        callbacks=translator,
        clarify_callback=lambda *_args: None,
        runtime={
            "api_mode": "acp",
            "command": "codex",
            "args": ["app-server"],
            "credential_pool": {"pool": "primary"},
        },
        request_overrides={"temperature": 0.2},
    )

    assert result.max_iterations == 123
    assert result.max_tokens == 456
    assert result.fallback_models == [
        {
            "model": "gpt-test",
            "provider": "openai",
            "base_url": None,
            "api_key": None,
            "key_env": None,
        }
    ]
    assert result.kwargs["max_iterations"] == 123
    assert result.kwargs["max_tokens"] == 456
    assert result.kwargs["status_callback"] == translator.status
    assert result.kwargs["request_overrides"] == {"temperature": 0.2}
    assert result.kwargs["platform"] == "webui"
    assert result.kwargs["reasoning_config"] == {"effort": "high"}
    assert result.kwargs["api_mode"] == "acp"
    assert result.kwargs["acp_command"] == "codex"
    assert result.kwargs["acp_args"] == ["app-server"]
    assert result.kwargs["credential_pool"] == {"pool": "primary"}
    assert result.kwargs["gateway_session_key"] == "session-1"


def test_usage_tracker_reanchors_tool_estimate_to_fresh_exact_prompt_tokens(monkeypatch):
    calls = []
    session = SimpleNamespace(last_prompt_tokens=100)
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(last_prompt_tokens=100),
    )
    monkeypatch.setattr(local_usage, "_tool_result_snippet", lambda value: str(value))
    monkeypatch.setattr(
        local_usage,
        "_live_usage_session_snapshot",
        lambda _sid, current, _cache: current,
    )
    monkeypatch.setattr(
        local_usage,
        "live_usage_prompt_estimate_after_tool_delta",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "last_prompt_tokens": kwargs["exact_prompt_tokens"] + 9,
                "turn_tool_prompt_tokens": 9,
            }
        ),
    )
    monkeypatch.setattr(local_usage, "prompt_cache_hit_percent", lambda *_args: None)
    tracker = LocalUsageTracker(
        session_id="session-1",
        session_getter=lambda: session,
        agent_getter=lambda: agent,
    )

    tracker.record_tool_start("tool-1", "terminal", {"command": "pwd"})
    agent.context_compressor.last_prompt_tokens = 250
    tracker.snapshot()
    tracker.record_tool_complete("tool-1", "terminal", "done")

    assert calls[0]["base_prompt_tokens"] == 100
    assert calls[-1]["base_prompt_tokens"] == 250
