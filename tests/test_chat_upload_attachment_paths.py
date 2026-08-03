"""Regression coverage for WebUI chat upload path handoff."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.frontend_asset_contract import family_source


_SESSION_DISPLAY_MODULE = (
    Path(__file__).resolve().parents[1]
    / "static"
    / "modules"
    / "sessions"
    / "session-display.js"
)
_NODE = shutil.which("node")


def test_image_uploads_use_server_path_in_attached_files_context():
    """The agent text context must include real uploaded paths for images.

    /api/upload returns an absolute attachment path. The browser also sends the
    structured attachment payload to /api/chat/start, but text/tool-mode agents
    still rely on the literal ``[Attached files: ...]`` suffix. Images must not
    be downgraded to bare filenames there, otherwise tools like vision_analyze
    cannot open the uploaded file immediately.
    """
    src = family_source("messages")

    assert "uploadedPaths=uploaded.map(u=>u&&u.is_image?" not in src
    assert "uploadedPaths=uploaded.map(u=>u&&u.path?u.path" in src


def test_attached_files_context_is_hidden_from_user_message_display():
    """Persist full attachment paths for the agent without showing them in chat."""
    ui_src = family_source("ui")
    assert "function _stripAttachedFilesMarkerForDisplay" in ui_src
    assert "_stripAttachedFilesMarkerForDisplay(_stripWorkspaceDisplayPrefix(content))" in ui_src
    assert "const newRawText=String(displayContent).trim();" in ui_src
    assert "row.dataset.rawText=newRawText;" in ui_src


def test_attached_files_context_is_hidden_from_sidebar_titles():
    """Sidebar rows should not expose absolute uploaded image paths in titles."""
    if _NODE is None:
        pytest.skip("node is required to execute the sidebar title projection")

    attachment_marker = "\n\n[Attached files: /tmp/private/Screenshot.png]"
    rows = [
        {"title": f"Question{attachment_marker}"},
        {"display_title": f"Display title{attachment_marker}", "title": "fallback"},
        {"_state_db_title": f"State DB title{attachment_marker}", "title": "fallback"},
    ]
    script = f"""
import {{ _sessionDisplayTitle }} from {json.dumps(_SESSION_DISPLAY_MODULE.as_uri())};
const rows = JSON.parse(process.argv[1]);
console.log(JSON.stringify(rows.map(_sessionDisplayTitle)));
"""
    result = subprocess.run(
        [_NODE, "--input-type=module", "-e", script, json.dumps(rows)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["Question", "Display title", "State DB title"]


def test_server_provisional_titles_strip_attached_files_context():
    """Server-generated provisional titles must not include the path suffix."""
    from api.sessions.store import title_from

    title = title_from([
        {
            "role": "user",
            "content": "why is llm wiki not working?\n\n[Attached files: /tmp/private/Screenshot.png]",
        }
    ])

    assert title == "why is llm wiki not working?"
    assert "Attached files" not in title
    assert "/tmp/private" not in title


def test_duplicate_upload_response_reports_actual_stored_filename(tmp_path, monkeypatch):
    """Duplicate upload names should report the suffixed stored basename."""
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path))

    from api.upload import _sanitize_upload_name, _upload_destination

    safe_name = _sanitize_upload_name("photo.png")
    first = _upload_destination("session-a", safe_name)
    first.write_bytes(b"first")
    second = _upload_destination("session-a", safe_name)

    assert first.name == "photo.png"
    assert second.name == "photo-1.png"

    from api.media.uploads import store_chat_attachment_for_session

    stored = store_chat_attachment_for_session("session-a", safe_name, b"second")
    assert stored["filename"] == "photo-1.png"
