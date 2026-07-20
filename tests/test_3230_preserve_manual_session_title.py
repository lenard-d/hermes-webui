"""Regression coverage for preserving manually named sessions (#3230)."""
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import api.config as config
import api.sessions.store as models
import api.profiles as profiles_api
from api.runs.title_generation import lifecycle as title_generation
from api.sessions.store import Session
from api.sessions.operations import apply_session_title_rename, mark_session_title_generated


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield session_dir
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()


def _user_msg(text):
    return {"role": "user", "content": text}


def _assistant_msg(text):
    return {"role": "assistant", "content": text}


def _exchange_messages(count):
    messages = []
    for idx in range(count):
        messages.append(_user_msg(f"question {idx}"))
        messages.append(_assistant_msg(f"answer {idx}"))
    return messages


def _put_event(events):
    def put(name, data):
        events.append((name, data))

    return put


def test_rename_marks_title_manual_and_persists_flag():
    session = Session(
        session_id="manual-title-session",
        title="Old generated title",
        messages=_exchange_messages(1),
        llm_title_generated=True,
    )

    apply_session_title_rename(session, "Long-lived Work Plan")
    session.save()

    loaded = Session.load("manual-title-session")
    assert loaded.title == "Long-lived Work Plan"
    assert loaded.manual_title is True
    assert loaded.llm_title_generated is False
    assert loaded.compact()["manual_title"] is True

    payload = json.loads(session.path.read_text(encoding="utf-8"))
    assert payload["manual_title"] is True
    assert payload["llm_title_generated"] is False


def test_blank_or_auto_title_clears_manual_lock():
    session = Session(
        session_id="clear-manual-title",
        title="Pinned investigation",
        manual_title=True,
        llm_title_generated=True,
    )

    apply_session_title_rename(session, "  ")
    assert session.title == "Untitled"
    assert session.manual_title is False
    assert session.llm_title_generated is False

    apply_session_title_rename(session, "New Chat")
    assert session.title == "New Chat"
    assert session.manual_title is False
    assert session.llm_title_generated is False


def test_adaptive_refresh_skips_manual_title_even_at_configured_interval():
    session = Session(
        session_id="manual-refresh-skip",
        title="Release Review Notes",
        messages=_exchange_messages(5),
        llm_title_generated=True,
        manual_title=True,
    )

    with patch("api.runs.title_generation.lifecycle._get_title_refresh_interval", return_value=5), \
         patch("threading.Thread") as thread_cls:
        title_generation._maybe_schedule_title_refresh(session, lambda *_args: None, agent=None)

    assert thread_cls.called is False


def test_adaptive_refresh_still_schedules_for_generated_titles_without_manual_lock():
    session = Session(
        session_id="generated-refresh",
        title="Generated Session Title",
        messages=_exchange_messages(5),
        llm_title_generated=True,
        manual_title=False,
    )

    with patch("api.runs.title_generation.lifecycle._get_title_refresh_interval", return_value=5), \
         patch("threading.Thread") as thread_cls:
        thread = MagicMock()
        thread_cls.return_value = thread
        title_generation._maybe_schedule_title_refresh(session, lambda *_args: None, agent=None)

    assert thread_cls.called is True
    assert thread.start.called is True


def test_generated_title_clears_manual_lock():
    session = Session(
        session_id="generated-clears-manual",
        title="Manual Before Regenerate",
        manual_title=True,
        llm_title_generated=False,
    )

    mark_session_title_generated(session)

    assert session.manual_title is False
    assert session.llm_title_generated is True


def test_cleared_title_allows_initial_auto_generation(monkeypatch):
    session = Session(
        session_id="cleared-title-auto-generation",
        title="Untitled",
        messages=_exchange_messages(1),
        manual_title=False,
        llm_title_generated=False,
    )
    session.save = MagicMock()
    events = []

    monkeypatch.setattr(title_generation, "get_session", lambda _sid: session)

    @contextlib.contextmanager
    def edit_loaded(_sid, *, touch_updated_at=True, save_when=None, **_kwargs):
        yield session
        if save_when is None or save_when(session):
            kwargs = {} if touch_updated_at else {"touch_updated_at": False}
            session.save(**kwargs)

    monkeypatch.setattr(title_generation, "edit_session", edit_loaded)
    monkeypatch.setattr(title_generation, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(
        title_generation,
        "_generate_llm_session_title_via_aux",
        lambda *_args, **_kwargs: ("Generated Follow-up Title", "llm_ok", "raw"),
    )
    monkeypatch.setattr(
        profiles_api,
        "profile_env_for_background_worker",
        lambda *_args, **_kwargs: contextlib.nullcontext(),
    )

    title_generation._run_background_title_update(
        session.session_id,
        "question",
        "answer",
        "Untitled",
        _put_event(events),
        agent=None,
    )

    assert session.title == "Generated Follow-up Title"
    assert session.manual_title is False
    assert session.llm_title_generated is True
    assert any(name == "title" and data["title"] == "Generated Follow-up Title" for name, data in events)


def test_clear_endpoint_resets_manual_title_lock():
    """Clearing a manually-named session must drop the manual_title lock so the
    reused session can auto-name again (#3542 gap: /api/session/clear reset the
    title directly, leaving manual_title=True). The endpoint now routes the
    title reset through apply_session_title_rename — assert that helper clears
    the lock for the auto-label the clear path uses."""
    session = Session(
        session_id="clear-endpoint-manual-title",
        title="My Deliberate Name",
        messages=_exchange_messages(2),
        manual_title=True,
        llm_title_generated=False,
    )

    # This mirrors exactly what /api/session/clear now does for the title.
    apply_session_title_rename(session, "Untitled")

    assert session.title == "Untitled"
    assert session.manual_title is False, (
        "clearing a manually-named session must drop the manual_title lock"
    )
    assert session.llm_title_generated is False


def test_clear_route_resets_manual_title_lock(monkeypatch):
    """Exercise the extracted HTTP owner instead of inspecting its source."""
    import api.routes as routes
    from api.http.routes import session_mutations

    session = Session(
        session_id="clear-route-manual-title",
        title="My Deliberate Name",
        messages=_exchange_messages(1),
        manual_title=True,
        llm_title_generated=True,
    )
    responses = []

    @contextlib.contextmanager
    def edit_session(_sid, *, session=None, **_kwargs):
        yield session

    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "edit_session", edit_session)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, **_kwargs: responses.append(payload) or True,
    )

    assert session_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/session/clear"),
        {"session_id": session.session_id},
        None,
        routes.__dict__,
    )

    assert session.title == "Untitled"
    assert session.manual_title is False
    assert session.llm_title_generated is False
    assert responses[-1]["session"]["manual_title"] is False
