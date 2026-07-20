"""Owner contracts for session import, detail, and sidebar projections."""

from __future__ import annotations

import subprocess
import sys

from api.sessions import (
    foreign_session_access,
    session_detail_projection,
    session_sidebar_projection as sidebar_projection,
)


REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


def test_session_projection_package_exposes_three_cohesive_interfaces():
    assert callable(foreign_session_access.claim)
    assert callable(session_detail_projection.message_window)
    assert callable(sidebar_projection.response_item)
    assert not (REPO / "api" / "routes_parts" / "session_projection.py").exists()


def test_session_projection_owners_import_without_routes():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                    "import sys; "
                    "from api.sessions import foreign_session_access, "
                    "session_detail_projection, session_sidebar_projection; "
                    "assert foreign_session_access.claim; "
                    "assert session_detail_projection.message_window; "
                    "assert session_sidebar_projection.response_item; "
                    "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_branch_resolution_returns_transport_neutral_refusal(monkeypatch):
    from api.sessions import materialization

    monkeypatch.setattr(materialization, "_session_is_subagent_view_only", lambda _sid: True)
    resolution = foreign_session_access.resolve_branch_source("child")
    assert resolution.session is None
    assert resolution.refusal == "subagent_view_only"
