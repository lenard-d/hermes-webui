"""Architecture contract for session import, lineage, and sidebar projections."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import routes
from api.routes_parts import session_projection


REPO = Path(__file__).resolve().parents[1]


def test_session_projection_functions_keep_the_routes_facade_seam():
    for name in session_projection.__routes_exports__:
        value = getattr(routes, name)
        if callable(value):
            assert value.__module__ == "api.routes"
            assert value.__globals__ is vars(routes)

    assert routes._SESSION_DETAIL_TAIL_CACHE is session_projection._SESSION_DETAIL_TAIL_CACHE
    assert routes._SESSION_DETAIL_TAIL_CACHE_LOCK is session_projection._SESSION_DETAIL_TAIL_CACHE_LOCK


def test_session_projection_owner_imports_without_routes():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import api.routes_parts.session_projection as owner; "
                "assert owner.__routes_exports__; assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_session_projection_owner_is_file_backed_and_facade_stays_thin():
    owner_source = Path(session_projection.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "def _claim_or_synthesize_cli_session(" in owner_source
    assert "def _message_window_for_display(" in owner_source
    assert "def _pre_compression_continuation_session_id(" in owner_source
    assert "def _sidebar_session_response_item(" in owner_source
    assert "def _claim_or_synthesize_cli_session(" not in facade_source
    assert "exec(" not in owner_source
