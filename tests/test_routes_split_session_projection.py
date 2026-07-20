"""Owner contracts for session import, detail, and sidebar projections."""

from __future__ import annotations

import subprocess
import sys

from api import routes
from api.routes_parts import session_projection
from api.sessions import detail_projection, materialization, sidebar_projection


REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


def test_session_projection_functions_keep_their_domain_owners():
    assert session_projection.__routes_exports__ == ()
    assert routes._claim_or_synthesize_cli_session is materialization._claim_or_synthesize_cli_session
    assert routes._message_window_for_display is detail_projection._message_window_for_display
    assert routes._sidebar_session_response_item is sidebar_projection._sidebar_session_response_item
    assert routes._SESSION_DETAIL_TAIL_CACHE is detail_projection._SESSION_DETAIL_TAIL_CACHE
    assert routes._SESSION_DETAIL_TAIL_CACHE_LOCK is detail_projection._SESSION_DETAIL_TAIL_CACHE_LOCK


def test_session_projection_owners_import_without_routes():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                    "import sys; "
                    "import api.sessions.materialization as materialization; "
                    "import api.sessions.detail_projection as detail; "
                    "import api.sessions.sidebar_projection as sidebar; "
                    "assert materialization._claim_or_synthesize_cli_session; "
                    "assert detail._message_window_for_display; "
                    "assert sidebar._sidebar_session_response_item; "
                    "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_session_projection_adapter_only_owns_http_input_and_refusal_handling():
    assert session_projection._request_wants_all_profiles_import({"all_profiles": True})
    assert session_projection._normalize_import_profile_value("default") == "default"
    assert session_projection._claim_or_synthesize_cli_session is materialization._claim_or_synthesize_cli_session
    assert session_projection._pre_compression_continuation_session_id is detail_projection._pre_compression_continuation_session_id
    assert session_projection._sidebar_session_response_item is sidebar_projection._sidebar_session_response_item
