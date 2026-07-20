"""Ownership guards for run execution and live stream transport."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

from api import streaming
from api.runs import local, local_entrypoint, payloads


ROOT = Path(__file__).resolve().parents[1]
STREAMING_ROOT = ROOT / "api" / "streaming"


class _Session:
    def __init__(self):
        self.messages = [{"role": "user", "content": "hello"}]

    def compact(self):
        return {"session_id": "session-1", "message_count": 99}


def test_run_payload_contract_keeps_full_transcript():
    result = payloads._session_payload_with_full_messages(
        _Session(),
        tool_calls=[{"id": "tool-1"}],
    )

    assert result == {
        "session_id": "session-1",
        "message_count": 1,
        "messages": [{"role": "user", "content": "hello"}],
        "tool_calls": [{"id": "tool-1"}],
    }


def test_cancel_payload_public_shape_is_unchanged():
    session = {"session_id": "session-1", "messages": []}
    assert payloads._cancel_event_payload("Stopped", session=session) == {
        "message": "Stopped",
        "type": "cancelled",
        "status": "cancelled",
        "session": session,
        "session_id": "session-1",
    }


def test_helpers_have_real_module_owners():
    assert payloads._session_payload_with_full_messages.__module__ == "api.runs.payloads"
    assert local.run_agent_streaming.__module__ == "api.runs.local"


def test_streaming_interface_stays_transport_focused():
    assert streaming.__all__ == ("cancel_stream",)
    assert not hasattr(streaming, "_session_payload_with_full_messages")
    assert not hasattr(streaming, "_sanitize_messages_for_api")


def test_streaming_transport_modules_import_independently():
    module_names = [
        f"api.streaming.{path.stem}"
        for path in STREAMING_ROOT.glob("*.py")
        if path.name != "__init__.py"
    ]
    for module_name in sorted(module_names):
        assert importlib.import_module(module_name).__name__ == module_name


def test_streaming_implementations_do_not_import_route_or_package_facades():
    forbidden = ("api.routes", "api.routes_parts", "api.streaming")
    for path in STREAMING_ROOT.glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = (node.module,)
            assert not any(
                name == blocked or name.startswith(f"{blocked}.")
                for name in names
                for blocked in forbidden
            ), f"{path.name} imports facade ownership: {names}"


def test_no_dynamic_source_or_module_alias_facade_remains():
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in STREAMING_ROOT.glob("*.py")
    )
    assert "sys.modules" not in source
    assert "exec(" not in source
    assert "streaming_parts" not in source


def test_local_run_composition_is_narrow_and_explicit():
    fields = inspect.signature(local.LocalRunDependencies).parameters
    assert set(fields) == {
        "get_session",
        "get_ai_agent",
        "resolve_model_provider",
        "get_session_agent_lock",
        "build_session_db_for_stream",
        "attempt_credential_self_heal",
        "load_webui_prefill_context",
        "prefill_messages_with_webui_context",
        "normalize_prefill_messages_before_user_turn",
        "classify_provider_error",
        "session_payload_with_full_messages",
        "maybe_schedule_title_refresh",
    }


def test_run_entrypoint_delegates_to_local_owner(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return "finished"

    monkeypatch.setattr(local, "run_agent_streaming", fake_run)
    result = local_entrypoint.run_agent_streaming(
        "session-1",
        "hello",
        "model-1",
        "/workspace",
        "stream-1",
        ephemeral=True,
    )

    assert result == "finished"
    assert calls[0][0][:5] == (
        "session-1",
        "hello",
        "model-1",
        "/workspace",
        "stream-1",
    )
    assert isinstance(calls[0][1]["dependencies"], local.LocalRunDependencies)
