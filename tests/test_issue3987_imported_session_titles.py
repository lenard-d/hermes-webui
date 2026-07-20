"""Regression coverage for imported-session title generation after CLI import (#3987)."""

from __future__ import annotations

from tests.frontend_asset_contract import family_source

import io
import json
from collections import OrderedDict
from types import SimpleNamespace
from urllib.parse import urlparse

import api.sessions.store as models
import api.routes as routes
from api.http.routes import session_mutations


SESSIONS_JS = family_source("sessions")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_import_cli_handler_queues_default_titles_after_persisting_import(monkeypatch):
    events = []
    queued = []

    class ImportedSession:
        session_id = "cli_import_title"
        profile = "default"

        def compact(self):
            return {"session_id": self.session_id, "title": "CLI Session"}

    imported = ImportedSession()
    cli_meta = {
        "title": "CLI Session",
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "external_agent",
        "source_label": "CLI",
        "read_only": False,
    }
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: None))
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda *_args, **_kwargs: cli_meta)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda *_args, **_kwargs: [{"role": "user", "content": "name this"}],
    )
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "is_cron_session", lambda *_args: False)
    monkeypatch.setattr(
        routes,
        "import_cli_session",
        lambda *_args, **_kwargs: events.append("persist") or imported,
    )
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda *_args, **_kwargs: events.append("publish"),
    )

    def queue_title(session, metadata):
        events.append("queue")
        queued.append((session, metadata))

    monkeypatch.setattr(routes, "_queue_generated_title_for_imported_session", queue_title)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, **_kwargs: events.append("respond") or payload,
    )

    result = routes._handle_session_import_cli(object(), {"session_id": imported.session_id})

    assert events == ["persist", "publish", "queue", "respond"]
    assert result["imported"] is True
    assert queued == [(imported, cli_meta)]


def test_import_cli_queue_helper_skips_read_only_sources(monkeypatch):
    thread_starts = []
    monkeypatch.setattr(
        routes.threading,
        "Thread",
        lambda **_kwargs: thread_starts.append(True),
    )

    routes._queue_generated_title_for_imported_session(
        SimpleNamespace(session_id="readonly-import"),
        {"title": "CLI Session", "source_tag": "cli", "read_only": True},
    )

    assert thread_starts == []


def test_import_cli_queue_helper_generates_title_once_for_placeholder_session(monkeypatch):
    persisted = []
    generated = []

    class FakeSession:
        def __init__(self, title):
            self.session_id = "cli_queued_title"
            self.title = title
            self.source_tag = "cli"
            self.raw_source = "cli"
            self.session_source = "external_agent"
            self.source_label = "CLI"
            self.read_only = False

    current = FakeSession("CLI Session")

    class InlineThread:
        def __init__(self, *, target, daemon, name):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(routes.threading, "Thread", InlineThread)
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: current if sid == current.session_id else None))
    monkeypatch.setattr(routes, "get_full_session", lambda sid, session=None: session)
    monkeypatch.setattr(routes, "generate_session_title_for_session", lambda session: (generated.append(session.session_id) or "Better imported title", "llm", "raw"))
    monkeypatch.setattr(
        routes,
        "_persist_generated_session_title",
        lambda session, title, *, event_reason, require_default_title=False: persisted.append(
            (session.session_id, title, event_reason, require_default_title)
        ),
    )

    routes._queue_generated_title_for_imported_session(current, {"title": "CLI Session", "source_tag": "cli"})

    assert generated == [current.session_id]
    assert persisted == [(current.session_id, "Better imported title", "session_title_regenerate", True)]


def test_import_cli_queue_helper_skips_sessions_that_already_have_real_titles(monkeypatch):
    generated = []
    persisted = []

    class FakeSession:
        def __init__(self):
            self.session_id = "cli_real_title"
            self.title = "Useful imported title"
            self.source_tag = "cli"
            self.raw_source = "cli"
            self.session_source = "external_agent"
            self.source_label = "CLI"
            self.read_only = False

    current = FakeSession()

    class InlineThread:
        def __init__(self, *, target, daemon, name):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(routes.threading, "Thread", InlineThread)
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: current if sid == current.session_id else None))
    monkeypatch.setattr(routes, "get_full_session", lambda sid, session=None: session)
    monkeypatch.setattr(
        routes,
        "generate_session_title_for_session",
        lambda session: generated.append(session.session_id) or ("Unexpected generated title", "llm", "raw"),
    )
    monkeypatch.setattr(
        routes,
        "_persist_generated_session_title",
        lambda session, title, *, event_reason, require_default_title=False: persisted.append(
            (session.session_id, title, event_reason, require_default_title)
        ),
    )

    routes._queue_generated_title_for_imported_session(
        current,
        {"title": "CLI Session", "source_tag": "cli"},
    )

    assert generated == []
    assert persisted == []


def test_generated_title_persist_reloads_latest_session_before_saving(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    cache = OrderedDict()

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", cache, raising=False)
    monkeypatch.setattr(routes, "SESSIONS", cache, raising=False)
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda session: None)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *args, **kwargs: None)

    stale = models.Session(
        session_id="cli_import_race",
        title="CLI Session",
        workspace=".",
        model="test-model",
        messages=[{"role": "user", "content": "first"}],
        source_tag="cli",
        raw_source="cli",
        session_source="external_agent",
        source_label="CLI",
    )
    stale.save(skip_index=True)
    stale_snapshot = models.Session.load(stale.session_id)

    latest = models.Session(
        session_id=stale.session_id,
        title="CLI Session",
        workspace=".",
        model="test-model",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ],
        source_tag="cli",
        raw_source="cli",
        session_source="external_agent",
        source_label="CLI",
    )
    latest.save(skip_index=True)
    cache[latest.session_id] = latest

    saved_title = routes._persist_generated_session_title(
        stale_snapshot,
        "Better imported title",
        event_reason="session_title_regenerate",
        require_default_title=True,
    )

    reloaded = models.Session.load(stale.session_id)
    assert saved_title == "Better imported title"
    assert reloaded.title == "Better imported title"
    assert len(reloaded.messages) == 3
    assert reloaded.llm_title_generated is True
    assert reloaded.manual_title is False
    assert len(cache[stale.session_id].messages) == 3
    assert stale_snapshot.title == "Better imported title"


def test_regenerate_endpoint_accepts_writable_imported_sessions():
    persisted = []
    responses = []

    class WritableImport:
        session_id = "writable-import"
        title = "CLI Session"
        source_tag = "cli"
        session_source = "external_agent"
        read_only = False

        def compact(self):
            return {"session_id": self.session_id, "title": self.title}

    session = WritableImport()
    context = dict(routes.__dict__)
    context.update(
        {
            "_get_or_materialize_session": lambda sid: session,
            "generate_session_title_for_session": lambda *_args, **_kwargs: (
                "Useful imported title",
                "llm",
                "raw",
            ),
            "_persist_generated_session_title": lambda current, title, **_kwargs: (
                persisted.append((current.session_id, title)),
                setattr(current, "title", title),
            )[-1],
            "bad": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("writable imported session was rejected")
            ),
            "j": lambda _handler, payload, **_kwargs: responses.append(payload) or True,
        }
    )

    result = session_mutations.handle_post(
        object(),
        urlparse("/api/session/title/regenerate"),
        {"session_id": session.session_id},
        None,
        context,
    )

    assert result is True
    assert persisted == [(session.session_id, "Useful imported title")]
    assert responses[0]["title"] == "Useful imported title"


def test_sessions_ui_keeps_regenerate_action_for_writable_imports():
    regen_idx = SESSIONS_JS.index("api('/api/session/title/regenerate'")
    window = SESSIONS_JS[regen_idx - 500:regen_idx]
    assert "session.is_imported" not in window
    assert "_isReadOnlySession(session)" in SESSIONS_JS
