"""Behavioral compatibility checks for the streaming module split facade."""

from api import streaming
from api.streaming_parts import payloads
from api.streaming_parts import runtime_resolution
from api.streaming_parts.bindings import streaming_api


class _Session:
    def __init__(self):
        self.messages = [{"role": "user", "content": "hello"}]

    def compact(self):
        return {"session_id": "session-1", "message_count": 99}


def test_payload_facade_preserves_full_transcript_contract(monkeypatch):
    attached = []
    monkeypatch.setattr(
        streaming,
        "attach_todo_state",
        lambda raw, messages: attached.append((raw, messages)),
    )

    result = streaming._session_payload_with_full_messages(
        _Session(),
        tool_calls=[{"id": "tool-1"}],
    )

    assert result == {
        "session_id": "session-1",
        "message_count": 1,
        "messages": [{"role": "user", "content": "hello"}],
        "tool_calls": [{"id": "tool-1"}],
    }
    assert attached == [(result, result["messages"])]


def test_redacted_payload_observes_facade_monkeypatches(monkeypatch):
    session_payload = {"session_id": "patched", "messages": []}
    calls = []
    monkeypatch.setattr(
        streaming,
        "_session_payload_with_full_messages",
        lambda session, *, tool_calls=None: session_payload,
    )
    monkeypatch.setattr(
        streaming,
        "redact_session_data",
        lambda payload: calls.append(payload) or {"redacted": payload["session_id"]},
    )

    result = streaming._redacted_session_payload_with_full_messages(_Session())

    assert result == {"redacted": "patched"}
    assert calls == [session_payload]


def test_echo_suffix_helper_observes_facade_compactor_patch(monkeypatch):
    seen = []

    def compact(value):
        seen.append(value)
        return str(value).replace(" ", "").lower()

    monkeypatch.setattr(streaming, "_compact_for_echo_compare", compact)

    assert streaming._strip_compact_echo_suffix("Prefix ANSWER", "answer") == (
        "Prefix",
        True,
    )
    assert seen[0] == "answer"
    assert any(value.strip() == "ANSWER" for value in seen)


def test_cancel_payload_public_shape_is_unchanged():
    session = {"session_id": "session-1", "messages": []}

    assert streaming._cancel_event_payload("Stopped", session=session) == {
        "message": "Stopped",
        "type": "cancelled",
        "status": "cancelled",
        "session": session,
        "session_id": "session-1",
    }


def test_late_binding_resolves_the_canonical_facade():
    assert streaming_api() is streaming
    assert payloads.session_payload_with_full_messages is not None


def test_provider_classifier_observes_facade_helpers(monkeypatch):
    seen = []
    monkeypatch.setattr(
        streaming,
        "_provider_error_probe_text",
        lambda value: (str(value), None),
    )
    monkeypatch.setattr(
        streaming,
        "_is_quota_error_text",
        lambda value: seen.append(value) or True,
    )

    result = streaming._classify_provider_error("account exhausted")

    assert result["type"] == "quota_exhausted"
    assert seen == ["account exhausted"]


def test_multimodal_builder_observes_facade_mode_patch(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "_resolve_image_input_mode",
        lambda cfg: "text",
    )

    result = streaming._build_native_multimodal_message(
        "[workspace] ",
        "question",
        [{"path": "/must/not/be/read.png", "mime": "image/png"}],
        "/must/not/be/read",
        cfg={"agent": {"image_input_mode": "auto"}},
    )

    assert result == "[workspace] question"


def test_thinking_extractor_observes_facade_merge_patch(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "_merge_inline_thinking_reasoning",
        lambda existing, extracted: f"patched:{'|'.join(extracted)}",
    )

    content, reasoning = streaming._extract_inline_thinking_from_content(
        "<think>inspect</think>Answer",
    )

    assert content == "Answer"
    assert reasoning == "patched:inspect"


def test_title_sanitizer_observes_facade_validation_patch(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "_strip_thinking_markup",
        lambda text: "Candidate title",
    )
    monkeypatch.setattr(
        streaming,
        "_looks_invalid_generated_title",
        lambda text: text == "Candidate title",
    )

    assert streaming._sanitize_generated_title("ignored") == ""


def test_title_analysis_observes_facade_message_text_patch(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "_message_text",
        lambda content: f"visible:{content}",
    )

    assert streaming._first_exchange_snippets([
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]) == ("visible:question", "visible:answer")


def test_message_sanitizer_observes_facade_reasoning_patch(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "_is_reasoning_only_assistant_message",
        lambda message: message.get("content") == "drop-me",
    )

    assert streaming._sanitize_messages_for_api([
        {"role": "assistant", "content": "drop-me"},
        {"role": "assistant", "content": "keep-me"},
    ]) == [{"role": "assistant", "content": "keep-me"}]


def test_context_dedupe_observes_facade_identity_patch(monkeypatch):
    monkeypatch.setattr(streaming, "_is_context_compression_marker", lambda _msg: False)
    monkeypatch.setattr(
        streaming,
        "_is_compressed_context_tool_result_summary_message",
        lambda _msg: False,
    )
    monkeypatch.setattr(streaming, "_message_identity", lambda _msg: "same")

    first = {"role": "user", "content": "first"}
    second = {"role": "assistant", "content": "second"}

    assert streaming._deduplicate_context_messages([first, second]) == [first]


def test_post_compression_pruner_observes_facade_helpers(monkeypatch):
    seen = []
    monkeypatch.setattr(streaming, "_post_compression_tool_result_budget", lambda _compressor: 1)
    monkeypatch.setattr(
        streaming,
        "_rough_text_token_count",
        lambda text: seen.append(text) or 2,
    )
    monkeypatch.setattr(
        streaming,
        "_compressed_context_tool_result_summary",
        lambda text, *, original_tokens, keep_tokens: (
            f"patched:{text}:{original_tokens}:{keep_tokens}"
        ),
    )

    result, pruned_count = streaming._hard_prune_post_compression_tool_results([
        {"role": "tool", "content": "large result"},
    ])

    assert pruned_count == 1
    assert seen == ["large result"]
    assert result[0]["content"] == "patched:large result:2:1"


def test_display_reasoning_restore_observes_facade_merge_patch(monkeypatch):
    restored = [{"role": "assistant", "content": "patched"}]
    monkeypatch.setattr(
        streaming,
        "_restore_reasoning_metadata",
        lambda previous, updated: restored,
    )
    monkeypatch.setattr(streaming, "_api_safe_message_positions", lambda _messages: [])
    monkeypatch.setattr(streaming, "_is_empty_partial_activity_message", lambda _message: True)

    assert streaming._restore_display_reasoning_metadata(
        [{"role": "assistant", "content": "before"}],
        [{"role": "assistant", "content": "after"}],
    ) is restored


def test_replay_prefix_check_observes_facade_identity_patch(monkeypatch):
    seen = []
    monkeypatch.setattr(
        streaming,
        "_message_identity",
        lambda message: seen.append(message) or message["identity"],
    )

    assert streaming._messages_have_prefix(
        [{"identity": "first"}, {"identity": "second"}],
        [{"identity": "first"}],
    )
    assert seen == [{"identity": "first"}, {"identity": "first"}]


def test_active_context_replay_observes_facade_dedupe_patch(monkeypatch):
    sentinel = [{"role": "assistant", "content": "patched"}]
    calls = []
    monkeypatch.setattr(
        streaming,
        "_dedupe_replayed_context_messages",
        lambda previous, result, text=None: calls.append((previous, result, text)) or sentinel,
    )
    previous = [{"role": "user", "content": "before"}]
    result = previous + [{"role": "assistant", "content": "after"}]

    assert streaming._dedupe_replayed_active_context(previous, result, "prompt") is sentinel
    assert calls == [(previous, result, "prompt")]


def test_context_replay_public_helpers_keep_streaming_module_identity():
    helpers = (
        streaming._session_context_messages,
        streaming._message_identity,
        streaming._messages_have_prefix,
        streaming._message_replay_key,
        streaming._strip_replayed_prefix,
        streaming._looks_like_replayed_session_arc_summary,
        streaming._strip_replayed_context_items,
        streaming._dedupe_replayed_context_messages,
        streaming._dedupe_replayed_active_context,
    )

    assert {helper.__module__ for helper in helpers} == {"api.streaming"}


def test_stale_user_tail_observes_facade_normalizers(monkeypatch):
    seen = []
    monkeypatch.setattr(
        streaming,
        "_raw_message_text",
        lambda content: seen.append(("raw", content)) or " raw tail ",
    )
    monkeypatch.setattr(
        streaming,
        "_normalize_user_text",
        lambda text: seen.append(("normalize", text)) or "patched tail",
    )

    assert streaming._stale_user_tail_candidate(
        {"role": "user", "content": "ignored"},
    ) == "patched tail"
    assert seen == [("raw", "ignored"), ("normalize", " raw tail ")]


def test_stale_user_cleaner_observes_facade_detector(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "_detect_stale_user_merge",
        lambda message, text, tail, previous_context=None: message.get("polluted") is True,
    )
    polluted = {"role": "user", "content": "stale", "polluted": True}
    clean = {"role": "assistant", "content": "answer"}

    result = streaming._strip_stale_user_merge_from_messages(
        [polluted, clean],
        "current",
        "prior",
    )

    assert result == [
        {"role": "user", "content": "current", "polluted": True},
        clean,
    ]
    assert result[0] is not polluted


def test_stale_user_public_helpers_keep_streaming_module_identity():
    helpers = (
        streaming._strip_workspace_prefixes_for_compare,
        streaming._normalize_user_text,
        streaming._raw_message_text,
        streaming._stale_user_tail_candidate,
        streaming._last_user_row,
        streaming._stale_prefix_matches_prior_user_context,
        streaming._detect_stale_user_merge,
        streaming._strip_stale_user_merge_from_messages,
    )

    assert {helper.__module__ for helper in helpers} == {"api.streaming"}


def test_compression_marker_observes_facade_classifier(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "is_context_compression_marker",
        lambda message: message == "patched marker",
    )

    assert streaming._is_context_compression_marker("patched marker")
    assert not streaming._is_context_compression_marker("other")


def test_compression_summary_observes_facade_helpers(monkeypatch):
    seen = []
    monkeypatch.setattr(
        streaming,
        "_is_context_compression_marker",
        lambda message: message.get("marker") is True,
    )
    monkeypatch.setattr(
        streaming,
        "_message_text",
        lambda content: seen.append(content) or f"visible:{content}",
    )

    result = streaming._compression_summary_from_messages([
        {"role": "assistant", "content": "ignored"},
        {"role": "assistant", "content": "summary", "marker": True},
    ])

    assert result == "visible:summary"
    assert seen == ["summary"]


def test_drop_checkpointed_user_observes_facade_identity(monkeypatch):
    seen = []
    monkeypatch.setattr(
        streaming,
        "_message_identity",
        lambda message: seen.append(message) or message.get("content"),
    )
    history = [
        {"role": "assistant", "content": "before"},
        {"role": "user", "content": "current"},
    ]

    assert streaming._drop_checkpointed_current_user_from_context(
        history,
        "current",
    ) == history[:-1]
    assert seen == [
        {"role": "user", "content": "current"},
        history[-1],
    ]


def test_compression_anchor_public_helpers_keep_streaming_module_identity():
    helpers = (
        streaming._is_context_compression_marker,
        streaming._compact_summary_text,
        streaming._compression_anchor_message_key,
        streaming._compression_summary_from_messages,
        streaming._find_current_user_turn,
        streaming._drop_checkpointed_current_user_from_context,
    )

    assert {helper.__module__ for helper in helpers} == {"api.streaming"}


def test_new_turn_context_observes_facade_decisions(monkeypatch):
    history = [{"role": "assistant", "content": "compacted task"}]
    monkeypatch.setattr(
        streaming,
        "_drop_checkpointed_current_user_from_context",
        lambda messages, text: history,
    )
    monkeypatch.setattr(streaming, "_is_casual_fresh_chat_message", lambda text: True)
    monkeypatch.setattr(
        streaming,
        "_has_task_resume_compaction_marker",
        lambda messages: messages is history,
    )

    assert streaming._new_turn_context_from_messages([], "hello") == []


def test_session_turn_context_observes_facade_history_selector(monkeypatch):
    selected = [{"role": "user", "content": "selected"}]
    calls = []
    monkeypatch.setattr(streaming, "_session_context_messages", lambda session: selected)
    monkeypatch.setattr(
        streaming,
        "_new_turn_context_from_messages",
        lambda messages, text: calls.append((messages, text)) or messages,
    )

    session = object()
    assert streaming._context_messages_for_new_turn(session, "prompt") is selected
    assert calls == [(selected, "prompt")]


def test_truncation_watermark_fallback_observes_facade_clock(monkeypatch):
    session = type(
        "Session",
        (),
        {
            "truncation_watermark": 1.0,
            "messages": [{"role": "user", "content": "current"}],
        },
    )()
    monkeypatch.setattr(streaming.time, "time", lambda: 321.5)

    streaming._advance_truncation_watermark_after_commit(session)

    assert session.truncation_watermark == 321.5


def test_turn_context_public_helpers_keep_streaming_module_identity():
    helpers = (
        streaming._save_streaming_checkpoint,
        streaming._normalize_fresh_chat_text,
        streaming._is_casual_fresh_chat_message,
        streaming._has_task_resume_compaction_marker,
        streaming._new_turn_context_from_messages,
        streaming._context_messages_for_new_turn,
        streaming._stream_writeback_is_current,
        streaming._stream_writeback_can_supersede_recovery_marker,
        streaming._advance_truncation_watermark_after_commit,
    )

    assert {helper.__module__ for helper in helpers} == {"api.streaming"}


def test_prefill_redactor_observes_facade_pattern(monkeypatch):
    seen = []

    class Pattern:
        def sub(self, replacement, value):
            seen.append((replacement, value))
            return "patched diagnostic"

    monkeypatch.setattr(streaming, "_SECRET_SHAPED_RE", Pattern())

    assert streaming._redact_prefill_status_text("secret=value") == "patched diagnostic"
    assert seen == [("[REDACTED]", "secret=value")]


def test_prefill_script_loader_observes_facade_runtime_helpers(monkeypatch):
    calls = []

    class Process:
        returncode = 0
        stdout = "raw output"
        stderr = ""

    class Subprocess:
        PIPE = object()
        TimeoutExpired = TimeoutError

        @staticmethod
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return Process()

    monkeypatch.setattr(streaming, "subprocess", Subprocess)
    monkeypatch.setattr(streaming, "_prefill_script_command", lambda raw: ["prefill-tool"])
    monkeypatch.setattr(streaming, "_prefill_script_timeout", lambda config: 7.5)
    monkeypatch.setattr(
        streaming,
        "_messages_from_prefill_script_output",
        lambda output: [{"role": "system", "content": f"parsed:{output}"}],
    )

    result = streaming._load_prefill_messages_script(
        {"webui_prefill_messages_script": "ignored"},
    )

    assert result["messages"] == [{"role": "system", "content": "parsed:raw output"}]
    assert calls == [
        (
            ["prefill-tool"],
            {
                "text": True,
                "stdout": Subprocess.PIPE,
                "stderr": Subprocess.PIPE,
                "timeout": 7.5,
                "check": False,
            },
        )
    ]


def test_prefill_context_loader_observes_facade_config(monkeypatch):
    config = {"marker": "patched"}
    sentinel = {
        "status": "not_configured",
        "source": "none",
        "label": "",
        "messages": [],
        "message_count": 0,
    }
    monkeypatch.delenv("HERMES_PREFILL_MESSAGES_FILE", raising=False)
    monkeypatch.setattr(streaming, "get_config", lambda: config)
    monkeypatch.setattr(
        streaming,
        "_load_prefill_messages_script",
        lambda value: sentinel if value is config else None,
    )
    monkeypatch.setattr(streaming, "_prefill_not_configured", lambda: sentinel)

    assert streaming._load_webui_prefill_context() is sentinel


def test_prefill_normalizer_observes_facade_logger(monkeypatch):
    seen = []
    logger = type("Logger", (), {"debug": lambda self, *args: seen.append(args)})()
    monkeypatch.setattr(streaming, "logger", logger)

    result = streaming._normalize_prefill_messages_before_user_turn([
        {"role": "system", "content": "keep"},
        {"role": "user", "content": "drop"},
    ])

    assert result == [{"role": "system", "content": "keep"}]
    assert seen == [("Dropped %d trailing user message(s) from prefill", 1)]


def test_webui_prefill_public_helpers_keep_streaming_module_identity():
    helpers = (
        streaming._redact_prefill_status_text,
        streaming._valid_prefill_messages,
        streaming._resolve_prefill_path,
        streaming._prefill_context_max_chars,
        streaming._prefill_context_char_count,
        streaming._budget_compacted_prefill_context,
        streaming._apply_prefill_context_budget,
        streaming._prefill_not_configured,
        streaming._load_prefill_messages_file,
        streaming._prefill_script_timeout,
        streaming._prefill_script_command,
        streaming._messages_from_prefill_script_output,
        streaming._load_prefill_messages_script,
        streaming._load_webui_prefill_context,
        streaming._public_prefill_context_status,
        streaming._webui_delivery_context_prompt,
        streaming._prefill_messages_with_webui_context,
        streaming._normalize_prefill_messages_before_user_turn,
    )

    assert {helper.__module__ for helper in helpers} == {"api.streaming"}


def test_runtime_snapshot_observes_facade_signature_patch(monkeypatch, tmp_path):
    memory_path = tmp_path / "memories" / "MEMORY.md"
    memory_path.parent.mkdir()
    memory_path.write_text("memory", encoding="utf-8")
    seen = []
    monkeypatch.setattr(
        streaming,
        "_file_signature",
        lambda path: seen.append(path) or (7, 8),
    )

    result = streaming._persistent_state_snapshot(str(tmp_path))

    assert result["memory"] == {"memory": (7, 8), "user": (7, 8), "soul": (7, 8)}
    assert memory_path in seen


def test_profile_home_resolution_observes_facade_provider_helper(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: patched-provider\n  default: patched-model\n",
        encoding="utf-8",
    )
    seen = []
    monkeypatch.setattr(
        streaming,
        "_apply_profile_provider_context_to_streaming_model",
        lambda *args: seen.append(args) or ("resolved", "provider", True),
    )

    assert streaming._apply_profile_home_context_to_streaming_model(
        "old-model",
        None,
        str(tmp_path),
        True,
    ) == ("resolved", "provider", True)
    assert seen == [("old-model", None, "patched-provider", "patched-model")]


def test_custom_runtime_resolution_observes_facade_connection_and_key(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "resolve_custom_provider_connection",
        lambda provider: (None, "http://patched.test/v1"),
    )
    monkeypatch.setattr(streaming, "_KEYLESS_CUSTOM_API_KEY", "patched-key")

    assert streaming._resolve_custom_provider_runtime_overrides(
        "custom:patched",
        None,
        None,
    ) == ("custom", "patched-key", "http://patched.test/v1")


def test_runtime_base_url_resolution_observes_facade_endpoint_matcher(monkeypatch):
    monkeypatch.setattr(streaming, "_same_base_url_endpoint", lambda left, right: True)

    assert streaming._runtime_preferred_base_url(
        {"base_url": "http://runtime.test/v1"},
        "openai",
        "http://configured.test/v1/v1",
    ) == "http://runtime.test/v1"


def test_runtime_resolution_public_helpers_keep_streaming_module_identity():
    helpers = (
        streaming._file_signature,
        streaming._persistent_state_snapshot,
        streaming._persistent_state_changes,
        streaming._apply_profile_provider_context_to_streaming_model,
        streaming._apply_profile_home_context_to_streaming_model,
        streaming._resolve_custom_provider_runtime_overrides,
        streaming._same_base_url_endpoint,
        streaming._runtime_preferred_base_url,
        streaming._is_fallback_lifecycle_message,
        streaming._is_agent_compression_start_status,
    )

    assert runtime_resolution.file_signature is not None
    assert {helper.__module__ for helper in helpers} == {"api.streaming"}
