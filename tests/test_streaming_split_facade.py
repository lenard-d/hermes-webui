"""Behavioral compatibility checks for the streaming module split facade."""

from api import streaming
from api.streaming_parts import payloads
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
