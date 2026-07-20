"""Regression coverage for settled SSE payload message counts."""

import json
import inspect
from pathlib import Path
from types import SimpleNamespace

from api.runs.payloads import _session_payload_with_full_messages
from api.runs.local_failures import LocalFailureOwner
from api.runs.local_success import LocalSuccessProjection


class _FakeSession(SimpleNamespace):
    def compact(self):
        return {
            "session_id": self.session_id,
            "message_count": 45,
            "title": "stale compact metadata",
        }


def test_full_message_payload_overrides_stale_compact_message_count():
    session = _FakeSession(
        session_id="child-session",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ],
    )

    payload = _session_payload_with_full_messages(session, tool_calls=[])

    assert payload["messages"] == session.messages
    assert payload["message_count"] == len(session.messages)
    assert payload["message_count"] != session.compact()["message_count"]


def test_full_message_payload_includes_todo_state_snapshot():
    todo_result = {
        "todos": [
            {"id": "todo-1", "content": "keep workspace todos visible", "status": "in_progress"},
        ],
        "summary": {
            "total": 1,
            "pending": 0,
            "in_progress": 1,
            "completed": 0,
            "cancelled": 0,
        },
    }
    session = _FakeSession(
        session_id="todo-session",
        messages=[
            {"role": "user", "content": "plan the fix", "timestamp": 100},
            {
                "role": "tool",
                "content": json.dumps(todo_result),
                "timestamp": 101,
            },
            {"role": "assistant", "content": "done", "timestamp": 102},
        ],
    )

    payload = _session_payload_with_full_messages(session, tool_calls=[])

    assert payload["todo_state"]["todos"] == todo_result["todos"]
    assert payload["todo_state"]["summary"] == todo_result["summary"]
    assert payload["todo_state"]["version"] == 1
    assert payload["todo_state"]["ts"] == 101


def test_done_payload_uses_full_message_count_helper():
    local_source = Path("api/runs/local.py").read_text(encoding="utf-8")
    block = inspect.getsource(LocalSuccessProjection.publish_terminal)

    assert "payload_builder=_session_payload_with_full_messages" in local_source
    assert "payload_builder(session, tool_calls=self.tool_calls)" in block
    assert block.index("payload_builder(session") < block.index(
        'publish("done", done_payload)'
    )
    assert ".compact()" not in block


def test_apperror_payload_uses_full_message_count_helper():
    local_source = Path("api/runs/local.py").read_text(encoding="utf-8")
    block = inspect.getsource(LocalFailureOwner._persist_error)

    assert "session_payload=_session_payload_with_full_messages" in local_source
    assert "self.ctx.session_payload(session, tool_calls=session.tool_calls)" in block
    assert block.index("self.ctx.session_payload(session") < block.index(
        'self.ctx.publish("apperror", payload)'
    )
    assert ".compact()" not in block


def test_gateway_done_payload_uses_full_message_count_helper():
    """The gateway-routed chat `done` SSE shares the settled-payload path and
    must also report a message_count matching the embedded transcript (sibling
    of the two streaming.py sites)."""
    gateway_source = Path("api/runs/gateway.py").read_text(encoding="utf-8")
    done_idx = gateway_source.index('publish(\n            "done"')
    block_start = gateway_source.rfind("session_payload =", 0, done_idx)
    block = gateway_source[block_start:done_idx]

    assert "_session_payload_with_full_messages(\n            session," in block
    assert 'session.compact() | {"messages": session.messages' not in block
