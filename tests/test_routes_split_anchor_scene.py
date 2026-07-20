"""Ownership checks for assistant anchor scenes and their HTTP adapter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from api import routes, sessions
from api.routes_parts import anchor_scene as anchor_scene_http
from api.sessions import anchor_scene as anchor_scene_owner


REPO = Path(__file__).resolve().parents[1]


def test_sessions_package_exposes_the_actual_anchor_scene_owner():
    assert (
        sessions.build_live_anchor_scene_snapshot
        is anchor_scene_owner._run_journal_live_snapshot
    )
    assert (
        sessions.hydrate_anchor_activity_scenes
        is anchor_scene_owner._hydrate_anchor_activity_scenes
    )
    assert (
        sessions.summarize_run_journal_status
        is anchor_scene_owner._run_journal_status_payload
    )
    assert (
        sessions.persist_anchor_activity_scene
        is anchor_scene_owner.persist_anchor_activity_scene
    )

    assert (
        routes.handle_session_anchor_scene
        is anchor_scene_http.handle_session_anchor_scene
    )
    assert not hasattr(routes, "_complete_hydrated_anchor_scene")


def test_session_owner_imports_without_loading_routes_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import api.sessions.anchor_scene as owner; "
                "assert owner.persist_anchor_activity_scene; "
                "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_http_adapter_translates_request_into_the_session_operation(monkeypatch):
    session = SimpleNamespace(session_id="session-1", profile="profile-1")
    captured = {}

    def persist(current, **kwargs):
        captured.update(session=current, **kwargs)
        return {"message_index": 2, "message_ref": "ref-2"}

    monkeypatch.setattr(anchor_scene_http, "persist_anchor_activity_scene", persist)

    response = anchor_scene_http.handle_session_anchor_scene(
        SimpleNamespace(),
        {
            "session_id": "session-1",
            "stream_id": "stream-1",
            "message_index": 2,
            "message_ref": "ref-2",
            "scene": {"version": "activity_scene_v1"},
        },
        get_or_materialize_session=lambda sid: session,
        session_visible_to_active_profile=lambda profile, handler: profile == "profile-1",
        require_fields=lambda body, *fields: None,
        bad_response=lambda handler, message, status=400: {
            "error": message,
            "status": status,
        },
        json_response=lambda handler, payload: payload,
    )

    assert response == {
        "ok": True,
        "message_index": 2,
        "message_ref": "ref-2",
    }
    assert captured == {
        "session": session,
        "scene": {"version": "activity_scene_v1"},
        "message_index": 2,
        "message_ref": "ref-2",
        "stream_id": "stream-1",
    }


def test_http_adapter_does_not_reexport_anchor_scene_implementation_helpers():
    adapter_names = vars(anchor_scene_http)

    assert "handle_session_anchor_scene" in adapter_names
    assert "persist_anchor_activity_scene" in adapter_names
    assert "_complete_hydrated_anchor_scene" not in adapter_names
    assert "_anchor_scene_tool_row" not in adapter_names
    assert "_run_journal_live_snapshot" not in adapter_names
