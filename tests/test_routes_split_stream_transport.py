"""Architecture contract for SSE replay, reattach, and event transport ownership."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import routes
from api.routes_parts import stream_transport


REPO = Path(__file__).resolve().parents[1]


def test_stream_transport_functions_keep_the_routes_facade_seam():
    for name in stream_transport.__routes_exports__:
        value = getattr(routes, name)
        if callable(value):
            assert value.__module__ == "api.routes"
            assert value.__globals__ is vars(routes)


def test_stream_transport_owner_imports_without_routes():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import api.routes_parts.stream_transport as o; assert o.__routes_exports__; assert 'api.routes' not in sys.modules"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_stream_transport_owner_is_file_backed_and_facade_dispatches_to_it():
    owner_source = Path(stream_transport.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "def _replay_run_journal(" in owner_source
    assert "def _handle_sse_stream(" in owner_source
    assert "def _handle_gateway_sse_stream(" in owner_source
    assert "def _handle_session_events_stream(" in owner_source
    assert "def _handle_sse_stream(" not in facade_source
    assert "_handle_sse_stream(handler, parsed)" in facade_source
    assert "exec(" not in owner_source
