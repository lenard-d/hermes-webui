from collections import OrderedDict
import json

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming


_RUNTIME_STREAM_MAPS = (
    config.STREAMS,
    config.CANCEL_FLAGS,
    config.AGENT_INSTANCES,
    config.STREAM_PARTIAL_TEXT,
    config.STREAM_REASONING_TEXT,
    config.STREAM_LIVE_TOOL_CALLS,
    config.STREAM_GOAL_RELATED,
    config.STREAM_LAST_EVENT_ID,
)


def _clear_runtime_maps():
    with config.STREAMS_LOCK:
        for mapping in _RUNTIME_STREAM_MAPS:
            mapping.clear()
    with config.STREAM_SESSION_OWNERS_LOCK:
        config.STREAM_SESSION_OWNERS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    config.PENDING_GOAL_CONTINUATION.clear()
    gateway_chat._STREAM_RUN_IDS.clear()


@pytest.fixture(autouse=True)
def isolated_gateway_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", raising=False)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(
        streaming,
        "_load_webui_prefill_context",
        lambda _cfg: {
            "status": "not_configured",
            "source": "none",
            "label": "",
            "message_count": 0,
            "messages": [],
        },
    )
    monkeypatch.setattr(
        streaming,
        "_prefill_messages_with_webui_context",
        lambda _context, _cfg: [],
    )
    monkeypatch.setattr(
        gateway_chat,
        "gateway_approval_unavailable_reason",
        lambda _base_url, _api_key: None,
    )
    _clear_runtime_maps()
    yield
    _clear_runtime_maps()


def _pending_session(stream_id, prompt):
    session = models.new_session(workspace=models.SESSION_DIR.parent, model="test-model")
    session.active_stream_id = stream_id
    session.pending_user_message = prompt
    session.pending_attachments = []
    session.pending_started_at = 123
    session.save()
    return session


def _registered_events(stream_id, session_id):
    channel = config.create_stream_channel()
    subscriber = channel.subscribe()
    config.register_runtime_stream(stream_id, session_id, channel)
    return subscriber


def _drain_events(subscriber):
    events = []
    while not subscriber.empty():
        item = subscriber.get_nowait()
        events.append((item[0], item[1]))
    return events


def test_first_terminal_write_failure_reports_persistence_error_without_snapshot(
    tmp_path,
    monkeypatch,
):
    class ProviderErrorResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"error":"provider failed"}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda _request, timeout=0: ProviderErrorResponse(),
    )
    stream_id = "gateway-writeback-first-save-failure"
    prompt = "keep this pending turn recoverable"
    session = _pending_session(stream_id, prompt)
    subscriber = _registered_events(stream_id, session.session_id)
    durable_before = json.loads(session.path.read_text(encoding="utf-8"))
    original_save = session.save
    save_attempts = 0

    def fail_first_settlement_save(**kwargs):
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise OSError("sidecar unavailable")
        return original_save(**kwargs)

    monkeypatch.setattr(session, "save", fail_first_settlement_save)

    gateway_chat._run_gateway_chat_streaming(
        session.session_id,
        prompt,
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    events = _drain_events(subscriber)
    persistence_errors = [
        payload
        for event, payload in events
        if event == "apperror" and payload.get("type") == "gateway_persistence_error"
    ]
    assert len(persistence_errors) == 1
    assert "session" not in persistence_errors[0]
    assert save_attempts == 1

    durable_after = json.loads(session.path.read_text(encoding="utf-8"))
    assert durable_after == durable_before
    recovered = models.Session.load(session.session_id)
    assert recovered.active_stream_id == stream_id
    assert recovered.pending_user_message == prompt
    assert recovered.messages == []


def test_provider_cancel_persists_pending_user_turn_and_cancellation_marker(
    tmp_path,
    monkeypatch,
):
    class Response:
        def __init__(self, *, body=None, lines=()):
            self._body = body
            self._lines = tuple(lines)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size=65536):
            return self._body

        def __iter__(self):
            return iter(self._lines)

    responses = iter(
        [
            Response(body=b'{"run_id":"run-provider-cancelled"}'),
            Response(
                lines=(
                    b"event: run.cancelled\n",
                    b'data: {"event":"run.cancelled"}\n',
                    b"\n",
                )
            ),
        ]
    )
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1")
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *_args: True)
    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda _request, timeout=0: next(responses),
    )
    stream_id = "gateway-provider-cancelled"
    prompt = "preserve this submitted request"
    session = _pending_session(stream_id, prompt)
    subscriber = _registered_events(stream_id, session.session_id)

    gateway_chat._run_gateway_chat_streaming(
        session.session_id,
        prompt,
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    events = _drain_events(subscriber)
    assert any(
        event == "cancel" and payload.get("message") == "Cancelled by gateway"
        for event, payload in events
    )
    recovered = models.Session.load(session.session_id)
    assert [message.get("role") for message in recovered.messages[-2:]] == [
        "user",
        "assistant",
    ]
    assert recovered.messages[-2]["content"] == prompt
    assert recovered.messages[-1]["_error"] is True
    assert recovered.messages[-1]["provider_details"] == "Cancelled by gateway"
    assert recovered.active_stream_id is None
    assert recovered.pending_user_message is None


def test_stale_worker_transport_failure_does_not_publish_application_error(
    tmp_path,
    monkeypatch,
):
    def fail_request(_request, timeout=0):
        raise OSError("old worker lost its gateway connection")

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fail_request)
    old_stream_id = "gateway-stale-worker"
    current_stream_id = "gateway-current-worker"
    current_prompt = "newer pending request"
    session = _pending_session(current_stream_id, current_prompt)
    subscriber = _registered_events(old_stream_id, session.session_id)

    gateway_chat._run_gateway_chat_streaming(
        session.session_id,
        "stale request",
        "test-model",
        str(tmp_path),
        old_stream_id,
        [],
    )

    events = _drain_events(subscriber)
    assert not any(event == "apperror" for event, _payload in events)
    recovered = models.Session.load(session.session_id)
    assert recovered.active_stream_id == current_stream_id
    assert recovered.pending_user_message == current_prompt
    assert recovered.messages == []


def test_gateway_run_id_is_released_when_runtime_cleanup_fails(tmp_path, monkeypatch):
    stream_id = "gateway-finish-cleanup-failure"
    session = _pending_session(stream_id, "trigger gateway teardown")
    _registered_events(stream_id, session.session_id)
    gateway_chat._STREAM_RUN_IDS[stream_id] = "run-awaiting-approval"

    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda _request, timeout=0: (_ for _ in ()).throw(
            OSError("gateway connection failed")
        ),
    )

    cleanup_error = RuntimeError("runtime cleanup failed")

    def fail_runtime_cleanup(_execution):
        raise cleanup_error

    monkeypatch.setattr(gateway_chat.TurnExecution, "finish", fail_runtime_cleanup)

    with pytest.raises(RuntimeError) as exc_info:
        gateway_chat._run_gateway_chat_streaming(
            session.session_id,
            "trigger gateway teardown",
            "test-model",
            str(tmp_path),
            stream_id,
            [],
        )

    assert exc_info.value is cleanup_error
    assert stream_id not in gateway_chat._STREAM_RUN_IDS
