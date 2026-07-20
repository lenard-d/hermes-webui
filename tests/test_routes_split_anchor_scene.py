"""Ownership checks for assistant anchor scenes and their HTTP adapter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from api import routes
from api.routes_parts import anchor_scene as anchor_scene_http
from api.sessions import anchor_scene as anchor_scene_interface
from api.sessions.anchor_scene import hydration as anchor_hydration_owner
from api.sessions.anchor_scene import journal_projection as anchor_journal_owner
from api.sessions.anchor_scene import persistence as anchor_persistence_owner


REPO = Path(__file__).resolve().parents[1]


def test_routes_composition_uses_only_the_anchor_owners_it_needs():
    assert routes._run_journal_live_snapshot is anchor_journal_owner._run_journal_live_snapshot
    assert routes._run_journal_status_payload is anchor_journal_owner._run_journal_status_payload
    assert routes._hydrate_anchor_activity_scenes is anchor_hydration_owner._hydrate_anchor_activity_scenes
    assert routes._handle_session_anchor_scene is anchor_scene_http._handle_session_anchor_scene
    assert not hasattr(routes, "_complete_hydrated_anchor_scene")

    assert routes._run_journal_live_snapshot.__module__.endswith(".journal_projection")
    assert routes._hydrate_anchor_activity_scenes.__module__.endswith(".hydration")


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
    interface_source = Path(anchor_scene_interface.__file__).read_text(encoding="utf-8")
    owner_source = Path(anchor_persistence_owner.__file__).read_text(encoding="utf-8")
    adapter_source = Path(anchor_scene_http.__file__).read_text(encoding="utf-8")

    assert "def persist_anchor_activity_scene(" in owner_source
    assert "persist_anchor_activity_scene" in interface_source
    assert "def _handle_session_anchor_scene(" not in owner_source
    assert "def _handle_session_anchor_scene(" in adapter_source
    for source in (interface_source, owner_source, adapter_source):
        assert "exec(" not in source
        assert "sys.modules" not in source
