from __future__ import annotations

import json
from urllib.parse import urlparse

import api.routes as routes
from api.sessions import foreign_session_access
from api.sessions.store import Session

def test_session_route_attaches_todo_state_for_webui_and_foreign_sessions(
    monkeypatch, tmp_path
):
    todo_message = {
        "role": "tool",
        "content": json.dumps(
            {
                "todos": [
                    {"id": "todo-1", "content": "verify route", "status": "pending"}
                ],
                "summary": {
                    "total": 1,
                    "pending": 1,
                    "in_progress": 0,
                    "completed": 0,
                    "cancelled": 0,
                },
            }
        ),
        "timestamp": 123,
    }
    webui_session = Session(
        session_id="todo-webui",
        workspace=tmp_path,
        messages=[todo_message],
        context_length=1,
        session_source="webui",
    )
    foreign_session = Session(
        session_id="todo-foreign",
        workspace=tmp_path,
        messages=[todo_message],
        context_length=1,
        source_tag="cli",
        session_source="cli",
        is_cli_session=True,
    )

    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: False)
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: payload,
    )

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: webui_session)
    webui_payload = routes.handle_get(
        object(),
        urlparse("/api/session?session_id=todo-webui&resolve_model=0"),
    )

    def missing_webui_session(*_args, **_kwargs):
        raise KeyError("foreign session")

    monkeypatch.setattr(routes, "get_session", missing_webui_session)
    monkeypatch.setattr(
        foreign_session_access,
        "metadata",
        lambda _sid: {"source_tag": "cli", "session_source": "cli"},
    )
    monkeypatch.setattr(
        foreign_session_access,
        "claim",
        lambda _sid, _metadata: (foreign_session, None),
    )
    foreign_payload = routes.handle_get(
        object(),
        urlparse("/api/session?session_id=todo-foreign&resolve_model=0"),
    )

    assert webui_payload["session"]["todo_state"]["todos"][0]["id"] == "todo-1"
    assert foreign_payload["session"]["todo_state"]["todos"][0]["id"] == "todo-1"
