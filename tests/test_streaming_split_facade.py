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
