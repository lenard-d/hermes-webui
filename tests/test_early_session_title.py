from tests.frontend_asset_contract import family_source

from pathlib import Path
from types import SimpleNamespace


def test_prepare_chat_start_sets_provisional_title_for_default_session(tmp_path, monkeypatch):
    from api.models import Session
    from api.turn_admission import prepare_session_for_turn

    saved = []

    def fake_save(self, *args, **kwargs):
        saved.append(
            {
                "title": self.title,
                "pending_user_message": self.pending_user_message,
                "active_stream_id": self.active_stream_id,
            }
        )

    monkeypatch.setattr(Session, "save", fake_save)

    s = Session(session_id="test-early-title", title="Untitled")
    prepare_session_for_turn(
        s,
        message="Can you conclude whether early WebUI session titles are possible?",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-1",
        started_at=123.0,
    )

    assert s.title != "Untitled"
    assert "early WebUI session titles" in s.title or s.title.startswith("Can you conclude")
    assert saved[-1]["title"] == s.title
    assert saved[-1]["pending_user_message"] == "Can you conclude whether early WebUI session titles are possible?"
    assert saved[-1]["active_stream_id"] == "stream-1"


def test_prepare_chat_start_sets_provisional_title_in_eager_save_mode(tmp_path, monkeypatch):
    from api.models import Session
    saved = []

    def fake_save(self, *args, **kwargs):
        saved.append({"title": self.title, "messages": list(self.messages)})

    monkeypatch.setattr(Session, "save", fake_save)
    import api.turn_admission as turn_admission
    monkeypatch.setattr(turn_admission, "get_webui_session_save_mode", lambda: "eager")

    s = Session(session_id="test-eager-early-title", title="Untitled")
    turn_admission.prepare_session_for_turn(
        s,
        message="Can eager session save also get early titles?",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-1",
        started_at=123.0,
    )

    assert s.title != "Untitled"
    assert s.messages[-1]["role"] == "user"
    assert s.messages[-1]["content"] == "Can eager session save also get early titles?"
    assert saved[-1]["title"] == s.title


def test_prepare_chat_start_does_not_overwrite_manual_title(tmp_path, monkeypatch):
    from api.models import Session
    from api.turn_admission import prepare_session_for_turn

    monkeypatch.setattr(Session, "save", lambda self, *a, **k: None)

    s = Session(session_id="test-manual-title", title="My Manual Title")
    prepare_session_for_turn(
        s,
        message="This prompt should not replace the title",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-1",
        started_at=123.0,
    )

    assert s.title == "My Manual Title"


def test_start_chat_stream_response_includes_provisional_title(tmp_path, monkeypatch):
    import api.config as config
    from api.models import Session
    import api.routes as routes
    import api.turn_admission as turn_admission

    monkeypatch.setattr(Session, "save", lambda self, *a, **k: None)
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda workspace: None)
    monkeypatch.setattr(turn_admission, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: None)

    class ImmediateThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            return None

    monkeypatch.setattr(
        turn_admission,
        "threading",
        SimpleNamespace(Thread=ImmediateThread),
    )

    s = Session(session_id="test-start-response-title", title="Untitled")
    response = routes._start_chat_stream_for_session(
        s,
        msg="Please design early session titles for Hermes WebUI",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
    )

    try:
        assert response["title"] == s.title
        assert response["title"] != "Untitled"
    finally:
        config.finish_runtime_run(response["stream_id"])
        with config.LOCK:
            config.SESSIONS.pop(s.session_id, None)


def test_prompt_provisional_title_still_counts_as_provisional_after_response():
    from api.models import title_from
    from api.streaming import _is_provisional_title

    messages = [
        {"role": "user", "content": "Can you implement early session titles in Hermes WebUI?"},
        {"role": "assistant", "content": "Yes, here is the plan..."},
    ]
    provisional = title_from(messages, "Untitled")
    assert _is_provisional_title(provisional, messages)


def test_prompt_prefix_manual_title_is_not_treated_as_provisional():
    from api.models import title_from
    from api.streaming import _is_provisional_title

    messages = [
        {"role": "user", "content": "Can you implement early session titles in Hermes WebUI?"},
        {"role": "assistant", "content": "Yes, here is the plan..."},
    ]
    provisional = title_from(messages, "Untitled")
    manual_prefix = provisional[: max(3, min(12, len(provisional) - 1))]

    assert manual_prefix
    assert provisional.startswith(manual_prefix)
    assert manual_prefix != provisional
    assert not _is_provisional_title(manual_prefix, messages)


def test_messages_js_applies_chat_start_title():
    src = family_source("messages")
    assert "applySessionTitleUpdate" in src
    assert "startData.title" in src or "provisional_title" in src
    assert "addEventListener('title'" in src
    assert "_sessionTitleLooksDefaultOrProvisional" in src
    assert "options.force" in src
    assert "_sessionTitleProvisionalBySid" in src
    assert "rememberProvisional:true" in src
    assert "provisionalText:displayText.slice(0,64)" in src
