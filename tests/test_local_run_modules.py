from __future__ import annotations

import json
from types import SimpleNamespace

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


def _event_api():
    meter = _Meter()
    partials = []
    reasoning = []
    started = []
    finished = []
    todos = []
    return SimpleNamespace(
        meter=lambda: meter,
        logger=SimpleNamespace(
            debug=lambda *_a, **_k: None, warning=lambda *_a, **_k: None
        ),
        STREAM_PARTIAL_TEXT={},
        STREAM_REASONING_TEXT={},
        _TOOL_ARG_CONTENT_CAP=500,
        _TOOL_ARG_CONTENT_KEYS={"content"},
        _compact_for_echo_compare=lambda value: " ".join(str(value).split()),
        _strip_compact_echo_suffix=lambda value, _suffix: (value, False),
        _is_agent_compression_start_status=lambda _kind, text: text == "compress",
        _is_fallback_lifecycle_message=lambda _kind, text: text == "fallback",
        _tool_result_snippet=lambda value: str(value)[:120],
        append_runtime_partial_text=lambda stream_id, text: partials.append(
            (stream_id, text)
        ),
        append_runtime_reasoning_text=lambda stream_id, text: reasoning.append(
            (stream_id, text)
        ),
        replace_runtime_reasoning_text=lambda *_args: None,
        start_runtime_tool_call=lambda stream_id, **kwargs: started.append(
            (stream_id, kwargs)
        ),
        finish_runtime_tool_call=lambda stream_id, **kwargs: finished.append(
            (stream_id, kwargs)
        ),
        emit_todo_state=lambda *_args, **kwargs: todos.append(kwargs),
        _test_meter=meter,
        _test_partials=partials,
        _test_reasoning=reasoning,
        _test_started=started,
        _test_finished=finished,
        _test_todos=todos,
    )


def _translator(*, parameters=()):
    api = _event_api()
    events = []
    translator = LocalEventTranslator(
        api,
        session_id="session-1",
        stream_id="stream-1",
        publish=lambda event, payload: events.append((event, payload)),
        usage=_Usage(),
        agent_params=lambda: set(parameters),
    )
    return api, events, translator


def test_event_translator_preserves_reasoning_before_visible_output():
    api, events, translator = _translator()

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


def test_event_translator_keeps_reasoning_segments_separate_at_real_boundaries():
    api, events, translator = _translator()

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


def test_reasoning_buffer_flushes_on_none_token_tool_and_explicit_finalization():
    import time

    _api, events, translator = _translator()

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


def test_event_translator_captures_terminal_status_and_routes_lifecycle_events():
    _api, events, translator = _translator()

    translator.status("lifecycle", "Non-retryable error HTTP 400")
    translator.status("lifecycle", "compress")
    translator.status("lifecycle", "fallback")

    assert translator.captured_terminal_error[0] == "Non-retryable error HTTP 400"
    assert events == [
        ("compressing", {"session_id": "session-1", "message": "Compressing context"}),
        ("warning", {"type": "fallback", "message": "fallback"}),
    ]


def test_structured_tool_callbacks_are_idempotent_and_checkpoint_completion():
    api, events, translator = _translator(
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


def test_legacy_tool_progress_is_suppressed_when_structured_callbacks_exist():
    api, events, translator = _translator(
        parameters={"tool_start_callback", "tool_complete_callback"}
    )

    translator.tool("tool.started", "terminal", None, {})
    translator.tool("tool.completed", "terminal", "done", {})

    assert not [
        event for event, _payload in events if event in {"tool", "tool_complete"}
    ]
    assert api._test_started == []
    assert api._test_finished == []


def test_legacy_tool_completion_prefers_full_result_for_todo_state():
    api, _events, translator = _translator()

    translator.tool(
        "tool.completed",
        "todo",
        "preview",
        {},
        result={"todos": ["complete"]},
    )

    assert api._test_todos[0]["function_result"] == {"todos": ["complete"]}


def test_agent_configuration_adapts_to_installed_constructor_and_config():
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

    api, _events, translator = _translator()
    api.coerce_reasoning_effort_for_model = lambda value, *_args, **_kwargs: value
    api.parse_reasoning_effort = lambda value: {"effort": value}
    result = build_local_agent_configuration(
        api,
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


def test_usage_tracker_reanchors_tool_estimate_to_fresh_exact_prompt_tokens():
    calls = []
    session = SimpleNamespace(last_prompt_tokens=100)
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(last_prompt_tokens=100),
    )
    api = SimpleNamespace(
        json=json,
        _tool_result_snippet=lambda value: str(value),
        _live_usage_session_snapshot=lambda _sid, current, _cache: current,
        live_usage_prompt_estimate_after_tool_delta=lambda **kwargs: (
            calls.append(kwargs)
            or {
                "last_prompt_tokens": kwargs["exact_prompt_tokens"] + 9,
                "turn_tool_prompt_tokens": 9,
            }
        ),
        prompt_cache_hit_percent=lambda *_args: None,
    )
    tracker = LocalUsageTracker(
        api,
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
