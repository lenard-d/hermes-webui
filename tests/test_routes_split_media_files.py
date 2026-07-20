"""Architecture contract for guarded file and media delivery ownership."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import routes
from api.routes_parts import media_files


REPO = Path(__file__).resolve().parents[1]


def test_media_file_functions_keep_the_routes_facade_seam():
    for name in media_files.__routes_exports__:
        value = getattr(routes, name)
        if callable(value):
            assert value.__module__ == "api.routes"
            assert value.__globals__ is vars(routes)


def test_media_files_owner_imports_without_routes():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import api.routes_parts.media_files as o; assert o.__routes_exports__; assert 'api.routes' not in sys.modules"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_media_files_owner_is_file_backed_and_facade_dispatches_to_it():
    owner_source = Path(media_files.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "def _serve_file_bytes(" in owner_source
    assert "def _handle_media(" in owner_source
    assert "def _handle_folder_download(" in owner_source
    assert "def _handle_file_raw(" in owner_source
    assert "def _handle_media(" not in facade_source
    assert "_handle_media(handler, parsed)" in facade_source
    assert "exec(" not in owner_source
