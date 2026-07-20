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
