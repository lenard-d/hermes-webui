"""Architecture contract for the media domain and thin HTTP adapters."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import routes
from api.media import delivery, preview, uploads
from api.routes_parts import media_files


REPO = Path(__file__).resolve().parents[1]
LEGACY_ROUTE_NAMES = (
    "_serve_file_bytes",
    "_serve_inline_html_preview",
    "_session_media_token_allows_path",
    "_handle_media",
    "_handle_folder_download",
    "_handle_file_raw",
    "_handle_file_read",
)


def test_media_file_functions_keep_thin_legacy_route_adapters():
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    for name in LEGACY_ROUTE_NAMES:
        assert callable(getattr(routes, name))
        start = facade_source.index(f"def {name}(")
        block = facade_source[start : start + 900]
        assert f"_media_http.{name}(" in block


def test_media_domain_and_http_adapter_import_without_routes():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import api.media; "
                "import api.routes_parts.media_files; "
                "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_media_domain_owns_policy_storage_and_delivery():
    delivery_source = Path(delivery.__file__).read_text(encoding="utf-8")
    preview_source = Path(preview.__file__).read_text(encoding="utf-8")
    uploads_source = Path(uploads.__file__).read_text(encoding="utf-8")
    adapter_source = Path(media_files.__file__).read_text(encoding="utf-8")

    assert "def resolve_local_media(" in delivery_source
    assert "def collect_folder_download(" in delivery_source
    assert "def preview_policy(" in preview_source
    assert "def store_chat_attachment(" in uploads_source
    assert "def _handle_media(" in adapter_source
    assert "def _handle_folder_download(" in adapter_source
    assert "def _handle_file_raw(" in adapter_source
    assert "api.routes" not in delivery_source + preview_source + uploads_source
    assert "__routes_exports__" not in adapter_source
    assert "_install_routes_part" not in adapter_source
    assert "exec(" not in adapter_source
