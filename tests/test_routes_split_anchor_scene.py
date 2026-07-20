"""Ownership checks for assistant anchor scenes and their HTTP adapter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from api import routes
from api.routes_parts import anchor_scene as anchor_scene_http
from api.sessions import anchor_scene as anchor_scene_owner


REPO = Path(__file__).resolve().parents[1]


def test_routes_facade_exposes_the_actual_session_owner():
    owned_operations = (
        "_run_journal_live_snapshot",
        "_complete_hydrated_anchor_scene",
        "_hydrate_anchor_activity_scenes",
    )

    for name in owned_operations:
        value = getattr(routes, name)
        assert value is getattr(anchor_scene_owner, name)
        assert value.__module__ == "api.sessions.anchor_scene"
        assert value.__globals__ is vars(anchor_scene_owner)

    assert routes._handle_session_anchor_scene is anchor_scene_http._handle_session_anchor_scene
    assert anchor_scene_http.__routes_exports__ == ()


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

    response = anchor_scene_http._handle_session_anchor_scene(
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


def test_domain_owner_and_http_adapter_are_plain_file_backed_modules():
    owner_source = Path(anchor_scene_owner.__file__).read_text(encoding="utf-8")
    adapter_source = Path(anchor_scene_http.__file__).read_text(encoding="utf-8")

    assert "def persist_anchor_activity_scene(" in owner_source
    assert "def _handle_session_anchor_scene(" not in owner_source
    assert "def _handle_session_anchor_scene(" in adapter_source
    assert "exec(" not in owner_source
    assert "sys.modules" not in owner_source
    assert "exec(" not in adapter_source
    assert "sys.modules" not in adapter_source
