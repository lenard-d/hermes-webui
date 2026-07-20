"""Architecture contract for chat/run request lifecycle ownership."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import routes
from api.routes_parts import chat_runs


REPO = Path(__file__).resolve().parents[1]


def test_chat_run_functions_keep_the_routes_facade_seam():
    for name in chat_runs.__routes_exports__:
        value = getattr(routes, name)
        assert value.__module__ == "api.routes"
        assert value.__globals__ is vars(routes)


def test_chat_runs_owner_imports_without_routes():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import api.routes_parts.chat_runs as o; assert o.__routes_exports__; assert 'api.routes' not in sys.modules"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_chat_runs_owner_is_file_backed_and_http_owner_dispatches_to_it():
    owner_source = Path(chat_runs.__file__).read_text(encoding="utf-8")
    turn_source = (REPO / "api" / "routes_parts" / "chat_turns.py").read_text(
        encoding="utf-8"
    )
    control_source = (
        REPO / "api" / "routes_parts" / "chat_controls.py"
    ).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")
    http_owner_source = (
        REPO / "api" / "http" / "routes" / "session_mutations.py"
    ).read_text(encoding="utf-8")

    assert "def start_session_turn(" in owner_source
    assert "def _handle_chat_start(" in turn_source
    assert "def _handle_chat_sync(" in turn_source
    assert "def _handle_goal_command(" in control_source
    assert "def _handle_chat_start(" not in facade_source
    assert "_handle_chat_start(handler, body, diag=diag)" in http_owner_source
    assert "exec(" not in owner_source
