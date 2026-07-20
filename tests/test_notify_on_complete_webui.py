from pathlib import Path


def test_webui_drains_only_matching_background_completion_events():
    from api.config import PROCESS_SESSION_INDEX, PROCESS_SESSION_INDEX_LOCK
    from api.runs.process_notifications import (
        _completion_event_targets_webui_session,
    )

    session_key = "notify-webui-session-key"
    session_id = "notify-webui-session"
    with PROCESS_SESSION_INDEX_LOCK:
        PROCESS_SESSION_INDEX[session_key] = session_id
    try:
        assert _completion_event_targets_webui_session(session_id, session_id)
        assert _completion_event_targets_webui_session(session_key, session_id)
        assert not _completion_event_targets_webui_session(session_key, "other-session")
        assert not _completion_event_targets_webui_session("", session_id)
    finally:
        with PROCESS_SESSION_INDEX_LOCK:
            PROCESS_SESSION_INDEX.pop(session_key, None)


def test_webui_injects_process_notifications_without_persisting_them_as_user_text():
    src = Path("api/runs/local_conversation.py").read_text(encoding="utf-8")

    assert "notifications = _drain_webui_process_notifications(" in src
    assert "pending_async_acceptances=pending_acceptances" in src
    assert "_accept_pending_async_delegations(" in src
    assert "_message_with_notifications(" in src
    assert "_build_native_multimodal_message(" in src
    assert '"persist_user_message": message_text' in src


def test_webui_sets_gateway_session_platform_for_background_watchers():
    from api.runs.turn_identity import _build_agent_thread_env

    run_src = Path("api/runs/local_environment.py").read_text(encoding="utf-8")

    thread_env = _build_agent_thread_env({}, "/workspace", "session-1", "/profile")
    assert thread_env["HERMES_SESSION_PLATFORM"] == "webui"
    assert '"HERMES_SESSION_PLATFORM": "webui"' in run_src
    assert "self._previous = {key: os.environ.get(key) for key in keys}" in run_src
    assert "for key, value in self._previous.items():" in run_src
    assert "os.environ.pop(key, None)" in run_src


def test_webui_stale_completion_age_gate_respects_override(monkeypatch):
    """Issue #4029: operators can configure or disable the stale-event cap."""
    from api.runs.process_notifications import _stale_completion_max_age_seconds

    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "120")
    assert _stale_completion_max_age_seconds() == 120
    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "0")
    assert _stale_completion_max_age_seconds() == 0
