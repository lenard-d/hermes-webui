"""Compatibility checks for the run-journal and anchor-scene extraction."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import routes
from api.routes_parts import anchor_scene


REPO = Path(__file__).resolve().parents[1]


def test_anchor_scene_owner_keeps_the_routes_facade_as_compatibility_seam():
    for name in anchor_scene.__routes_exports__:
        value = getattr(routes, name)
        if callable(value):
            assert value.__module__ == "api.routes"
            assert value.__globals__ is vars(routes)


def test_anchor_scene_owner_imports_without_loading_routes_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import api.routes_parts.anchor_scene as owner; "
                "assert owner.__routes_exports__; "
                "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_anchor_scene_implementation_is_file_backed_and_dispatch_stays_in_facade():
    owner_source = Path(anchor_scene.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "def _run_journal_live_snapshot(" in owner_source
    assert "def _complete_hydrated_anchor_scene(" in owner_source
    assert "def _handle_session_anchor_scene(" in owner_source
    assert "def _handle_session_anchor_scene(" not in facade_source
    assert "_handle_session_anchor_scene(handler, body)" in facade_source
    assert "exec(" not in owner_source
