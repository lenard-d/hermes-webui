from collections import OrderedDict
from types import SimpleNamespace

import pytest


def _exercise_materialization_route(monkeypatch, endpoint):
    from api import routes

    source = routes.Session(
        session_id=f"source_{endpoint}",
        title="Source",
        workspace=".",
        model="test-model",
        messages=[{"role": "user", "content": "persist this"}],
    )
    sessions = OrderedDict({source.session_id: source})
    failed_sessions = []
    responses = []
    events = []

    def fail_new_session_save(session, *_args, **_kwargs):
        if session is source:
            return None
        failed_sessions.append(session)
        raise OSError("materialized session disk full")

    def cache_session(session_id, session):
        sessions[session_id] = session
        sessions.move_to_end(session_id)

    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "cache_full_session", cache_session)
    monkeypatch.setattr(routes.Session, "save", fail_new_session_save)
    monkeypatch.setattr(
        routes.Session,
        "load",
        classmethod(lambda _cls, _sid: source),
    )
    monkeypatch.setattr(routes, "_load_branch_source_or_refuse", lambda *_args: source)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        routes,
        "_publish_session_list_changed",
        lambda reason, **_kwargs: events.append(reason),
    )
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda reason, **_kwargs: events.append(reason),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: responses.append(
            ("success", status, payload)
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: responses.append(
            ("error", status, message)
        )
        or True,
    )

    if endpoint == "duplicate":
        body = {"session_id": source.session_id}
        path = "/api/session/duplicate"
    elif endpoint == "branch":
        body = {"session_id": source.session_id}
        path = "/api/session/branch"
    else:
        body = {
            "title": "Imported",
            "workspace": ".",
            "messages": [{"role": "user", "content": "import this"}],
        }
        path = "/api/session/import"
        monkeypatch.setattr(
            routes,
            "resolve_trusted_workspace",
            lambda _workspace: ".",
        )

    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    handler = SimpleNamespace(headers={})
    try:
        routes.handle_post(handler, SimpleNamespace(path=path, query=""))
    except OSError:
        # Branch/import currently let the server boundary format persistence
        # errors; duplicate formats its own error response. Neither may emit a
        # success response or retain the failed session.
        pass

    assert len(failed_sessions) == 1
    failed_sid = failed_sessions[0].session_id
    assert failed_sid != source.session_id
    assert failed_sid not in sessions
    assert all(kind != "success" for kind, _status, _payload in responses)
    assert events == []


@pytest.mark.parametrize("endpoint", ["duplicate", "branch", "import"])
def test_required_first_save_failure_does_not_publish_in_memory_ghost(
    monkeypatch,
    endpoint,
):
    _exercise_materialization_route(monkeypatch, endpoint)


def test_empty_branch_remains_intentionally_memory_only(monkeypatch):
    from api import routes

    source = routes.Session(
        session_id="source_empty_branch",
        title="Empty source",
        workspace=".",
        model="test-model",
        messages=[],
    )
    sessions = OrderedDict({source.session_id: source})
    saved_new_sessions = []
    responses = []
    events = []

    def record_new_session_save(session, *_args, **_kwargs):
        if session is not source:
            saved_new_sessions.append(session.session_id)

    def cache_session(session_id, session):
        sessions[session_id] = session
        sessions.move_to_end(session_id)

    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "cache_full_session", cache_session)
    monkeypatch.setattr(routes.Session, "save", record_new_session_save)
    monkeypatch.setattr(routes, "_load_branch_source_or_refuse", lambda *_args: source)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": source.session_id},
    )
    monkeypatch.setattr(
        routes,
        "_publish_session_list_changed",
        lambda reason, **_kwargs: events.append(reason),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: responses.append(
            (payload, status)
        )
        or True,
    )

    assert routes.handle_post(
        SimpleNamespace(headers={}),
        SimpleNamespace(path="/api/session/branch", query=""),
    ) is True
    assert responses[0][1] == 200
    branch_sid = responses[0][0]["session_id"]
    assert branch_sid in sessions
    assert saved_new_sessions == []
    assert events == []


def test_index_projection_failure_does_not_turn_duplicate_commit_into_failure(
    monkeypatch,
):
    from api import routes

    source = routes.Session(
        session_id="source_duplicate_index_failure",
        title="Source",
        workspace=".",
        model="test-model",
        messages=[{"role": "user", "content": "duplicate me"}],
    )
    sessions = OrderedDict({source.session_id: source})
    sidecar_saves = []
    index_updates = []
    responses = []
    events = []

    def save_sidecar(session, *_args, **kwargs):
        sidecar_saves.append((session, kwargs))

    def fail_index(*, updates):
        index_updates.append(updates)
        raise OSError("session index disk full")

    def cache_session(session_id, session):
        sessions[session_id] = session
        sessions.move_to_end(session_id)

    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "cache_full_session", cache_session)
    monkeypatch.setattr(routes.Session, "save", save_sidecar)
    monkeypatch.setattr(
        routes.Session,
        "load",
        classmethod(lambda _cls, _sid: source),
    )
    monkeypatch.setattr(routes, "_write_session_index", fail_index)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": source.session_id},
    )
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda reason, **_kwargs: events.append(reason),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: responses.append(
            ("success", status, payload)
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: responses.append(
            ("error", status, message)
        )
        or True,
    )

    assert routes.handle_post(
        SimpleNamespace(headers={}),
        SimpleNamespace(path="/api/session/duplicate", query=""),
    ) is True
    assert len(sidecar_saves) == 1
    copied_session, save_kwargs = sidecar_saves[0]
    assert save_kwargs == {"skip_index": True}
    assert copied_session.session_id in sessions
    assert index_updates == [[copied_session]]
    assert responses[0][0:2] == ("success", 200)
    assert events == ["session_duplicate"]
